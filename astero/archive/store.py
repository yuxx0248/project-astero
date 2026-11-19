"""Versioned streaming audio and diagnostic archives.

The writer persists each raw frame as it arrives and appends a small JSONL
index row. It does not keep a recording in memory. A manifest starts in the
``recording`` state and is marked complete only after an explicit clean close;
after process termination an untouched manifest therefore remains visibly
incomplete.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import re
import threading
from typing import Any, Iterator, Mapping
import uuid

from astero.audio.capture import AudioFrame
from astero.audio.pcm import MonoFrame


SCHEMA_VERSION = 1
DEFAULT_ARCHIVE_ROOT = Path.home() / ".local" / "share" / "astero" / "archives"
_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_FORMAT_WIDTHS = {"s24_3le": 3, "pcm_s16le": 2, "float32_le": 4}
_SECRET_KEYS = {
    "apikey", "authorization", "accesstoken", "refreshtoken", "password",
    "passwd", "secret", "clientsecret", "credential", "credentials",
}


class ArchiveError(RuntimeError):
    """Base class for archive errors."""


class ArchiveValidationError(ArchiveError, ValueError):
    """An archive identifier, event, or frame is invalid."""


class ArchiveSessionExistsError(ArchiveError):
    """The capture identifier already exists; archives are never overwritten."""


class ArchiveSessionNotFoundError(ArchiveError, FileNotFoundError):
    """No archive exists for the requested capture identifier."""


class ArchiveRecordError(ArchiveError):
    """A frame or diagnostic record could not be persisted completely."""


class ArchiveIncompleteError(ArchiveError):
    """A caller requested a complete archive, but the archive is incomplete."""


@dataclass(slots=True)
class _OpenSession:
    path: Path
    manifest: dict[str, Any]
    raw: Any
    frame_index: Any
    event_index: Any
    payload_index: Any
    lock: threading.RLock
    mono_handles: dict[str, tuple[Any, Any]]
    mono_checkpoint_counts: dict[str, int]
    closed: bool = False


class ArchiveWriter:
    """Append raw audio and diagnostic records to local versioned archives.

    Args:
        root: Persistent archive directory. Defaults to
            ``~/.local/share/astero/archives``. It is created with mode 0700.

    No TTL, quota eviction, or automatic deletion is performed.
    """

    def __init__(self, root: str | os.PathLike[str] | None = None) -> None:
        self.root = Path(root).expanduser() if root is not None else DEFAULT_ARCHIVE_ROOT
        self._sessions: dict[str, _OpenSession] = {}
        self._lock = threading.RLock()
        self._ensure_root()

    def begin_session(
        self,
        metadata: Mapping[str, Any] | None = None,
        capture_id: str | None = None,
    ) -> str:
        """Create an archive and return its stable capture ID.

        The initial manifest is durably visible as ``recording`` before any
        frame is accepted, which makes abrupt termination distinguishable.
        """
        capture_id = capture_id or uuid.uuid4().hex
        _validate_id(capture_id, "capture_id")
        with self._lock:
            path = self.root / capture_id
            try:
                path.mkdir(mode=0o700)
            except FileExistsError as exc:
                raise ArchiveSessionExistsError(f"archive already exists for capture {capture_id}") from exc
            except OSError as exc:
                raise ArchiveRecordError("could not create archive session directory") from exc
            _secure_directory(path)
            created = _utc_now()
            safe_metadata = _redact_mapping(metadata or {})
            manifest: dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "capture_id": capture_id,
                "status": "recording",
                "complete": False,
                "started_at_utc": created,
                "ended_at_utc": None,
                "outcome": None,
                "metadata": safe_metadata,
                "raw_audio": {
                    "path": "raw.pcm",
                    "sample_format": None,
                    "sample_rate": None,
                    "channels": None,
                    "frame_count": 0,
                    "sample_count": 0,
                    "bytes": 0,
                },
                "derived_audio": [],
                "files": {
                    "frame_index": "frames.jsonl",
                    "events": "events.jsonl",
                    "debug_payloads": "debug_payloads.jsonl",
                },
            }
            try:
                raw = _open_new(path / "raw.pcm")
                frame_index = _open_new(path / "frames.jsonl")
                event_index = _open_new(path / "events.jsonl")
                payload_index = _open_new(path / "debug_payloads.jsonl")
                _write_manifest(path, manifest)
            except OSError as exc:
                # Keep the newly created directory as evidence of a failed
                # start. Never recursively delete archive data on failure.
                raise ArchiveRecordError("could not initialize archive files") from exc
            self._sessions[capture_id] = _OpenSession(
                path, manifest, raw, frame_index, event_index, payload_index,
                threading.RLock(), {}, {},
            )
        return capture_id

    def append_frame(self, capture_id: str, frame: AudioFrame) -> None:
        """Append one original interleaved PCM frame without transformation."""
        session = self._session(capture_id)
        _validate_audio_frame(frame)
        with session.lock:
            self._assert_open(session)
            raw_meta = session.manifest["raw_audio"]
            expected = (frame.sample_format, frame.sample_rate, frame.channels)
            actual = (raw_meta["sample_format"], raw_meta["sample_rate"], raw_meta["channels"])
            if raw_meta["frame_count"] and expected != actual:
                raise ArchiveValidationError("frame format changed within one capture session")
            offset = session.raw.tell()
            try:
                _write_all(session.raw, frame.pcm)
                row = {
                    "sequence": frame.sequence,
                    "timestamp_ns": frame.timestamp_ns,
                    "sample_rate": frame.sample_rate,
                    "channels": frame.channels,
                    "sample_format": frame.sample_format,
                    "sample_count": frame.sample_count,
                    "offset": offset,
                    "bytes": len(frame.pcm),
                }
                _write_jsonl(session.frame_index, row)
            except OSError as exc:
                self._mark_write_error(session, "raw_frame_write_failed")
                raise ArchiveRecordError("could not persist raw audio frame") from exc

            raw_meta.update({
                "sample_format": frame.sample_format,
                "sample_rate": frame.sample_rate,
                "channels": frame.channels,
                "frame_count": raw_meta["frame_count"] + 1,
                "sample_count": raw_meta["sample_count"] + frame.sample_count,
                "bytes": raw_meta["bytes"] + len(frame.pcm),
            })
            # Raw bytes and the per-frame index are written for every frame.
            # Checkpoint the summary manifest at 1 s intervals (100 x 10 ms
            # frames) to avoid rewriting/fsyncing it on every microphone tick.
            if raw_meta["frame_count"] % 100 == 0:
                try:
                    _write_manifest(session.path, session.manifest)
                except OSError as exc:
                    self._mark_write_error(session, "manifest_write_failed")
                    raise ArchiveRecordError("could not update archive manifest after audio frame") from exc

    def append_mono(
        self,
        capture_id: str,
        turn_id: str,
        frame: MonoFrame,
        *,
        channel: int | None = None,
        gain: float | None = None,
        source_sequence: int | None = None,
    ) -> None:
        """Persist a derived mono model input and its source mapping."""
        session = self._session(capture_id)
        _validate_id(turn_id, "turn_id")
        _validate_mono_frame(frame)
        if channel is not None and not 0 <= channel <= 6:
            raise ArchiveValidationError("channel must select a microphone channel from 0 through 6")
        if gain is not None and (not math.isfinite(gain) or gain <= 0):
            raise ArchiveValidationError("gain must be finite and positive")
        if source_sequence is not None and source_sequence < 0:
            raise ArchiveValidationError("source_sequence must be non-negative")

        with session.lock:
            self._assert_open(session)
            try:
                data_handle, index_handle = self._mono_handles(session, turn_id)
                self._validate_derived_consistency(session, turn_id, frame, channel, gain)
                offset = data_handle.tell()
                _write_all(data_handle, frame.data)
                _write_jsonl(index_handle, {
                    "turn_id": turn_id,
                    "timestamp": frame.timestamp,
                    "sequence": frame.sequence,
                    "source_sequence": source_sequence if source_sequence is not None else frame.sequence,
                    "sample_rate": frame.sample_rate,
                    "sample_format": frame.sample_format,
                    "sample_count": frame.sample_count,
                    "channel": channel,
                    "gain": gain,
                    "offset": offset,
                    "bytes": len(frame.data),
                })
                self._update_derived_manifest(session, turn_id, frame, channel, gain)
                session.mono_checkpoint_counts[turn_id] = session.mono_checkpoint_counts.get(turn_id, 0) + 1
                if session.mono_checkpoint_counts[turn_id] % 100 == 0:
                    _write_manifest(session.path, session.manifest)
            except OSError as exc:
                self._mark_write_error(session, "derived_audio_write_failed")
                raise ArchiveRecordError("could not persist derived mono audio") from exc

    def append_event(self, capture_id: str, event: Mapping[str, Any]) -> None:
        """Append a structured stage event, recursively redacting credentials."""
        if not isinstance(event, Mapping):
            raise ArchiveValidationError("event must be a mapping")
        session = self._session(capture_id)
        with session.lock:
            self._assert_open(session)
            record = _redact_mapping(event)
            record.setdefault("capture_id", capture_id)
            record.setdefault("recorded_at_utc", _utc_now())
            try:
                _write_jsonl(session.event_index, record)
            except (OSError, TypeError, ValueError) as exc:
                self._mark_write_error(session, "event_write_failed")
                raise ArchiveRecordError("could not persist archive event") from exc

    def append_debug_payload(
        self,
        capture_id: str,
        turn_id: str,
        kind: str,
        payload: Any,
    ) -> None:
        """Append debug-only text/JSON payload after stripping credential fields.

        Payloads are written to a mode-0600 archive file, not to ordinary
        application logs. Large payloads are serialized one at a time.
        """
        session = self._session(capture_id)
        _validate_id(turn_id, "turn_id")
        if not isinstance(kind, str) or not kind.strip() or len(kind) > 80:
            raise ArchiveValidationError("kind must be non-empty text of at most 80 characters")
        with session.lock:
            self._assert_open(session)
            row = {
                "capture_id": capture_id,
                "turn_id": turn_id,
                "kind": kind.strip(),
                "recorded_at_utc": _utc_now(),
                "payload": _redact_value(payload),
            }
            try:
                _write_jsonl(session.payload_index, row)
            except (OSError, TypeError, ValueError) as exc:
                self._mark_write_error(session, "debug_payload_write_failed")
                raise ArchiveRecordError("could not persist debug payload") from exc

    def close_session(self, capture_id: str, outcome: str | None = None) -> None:
        """Close a capture session and mark it complete only for clean outcome."""
        session = self._session(capture_id)
        with session.lock:
            if session.closed:
                return
            close_error: OSError | None = None
            handles = [session.raw, session.frame_index, session.event_index, session.payload_index]
            handles.extend(handle for pair in session.mono_handles.values() for handle in pair)
            for handle in handles:
                try:
                    handle.flush()
                    os.fsync(handle.fileno())
                    handle.close()
                except OSError as exc:
                    close_error = close_error or exc
            session.manifest["ended_at_utc"] = _utc_now()
            clean_outcome = outcome is None or outcome in {"completed", "clean", "stopped"}
            complete = clean_outcome and close_error is None and not session.manifest.get("write_error")
            session.manifest["complete"] = complete
            session.manifest["status"] = "complete" if complete else "incomplete"
            session.manifest["outcome"] = outcome or ("completed" if complete else "write_error")
            try:
                _write_manifest(session.path, session.manifest)
            except OSError as exc:
                # The old on-disk manifest remains ``recording`` and therefore
                # incomplete, which is safer than claiming a false success.
                session.closed = True
                raise ArchiveRecordError("could not finalize archive manifest") from exc
            session.closed = True
            if close_error is not None:
                raise ArchiveRecordError("could not flush and close archive files") from close_error

    def _session(self, capture_id: str) -> _OpenSession:
        _validate_id(capture_id, "capture_id")
        with self._lock:
            try:
                return self._sessions[capture_id]
            except KeyError as exc:
                raise ArchiveSessionNotFoundError(f"capture session is not open: {capture_id}") from exc

    def _mono_handles(self, session: _OpenSession, turn_id: str) -> tuple[Any, Any]:
        handles = session.mono_handles.get(turn_id)
        if handles is not None:
            return handles
        derived = session.path / "derived"
        if not derived.exists():
            derived.mkdir(mode=0o700)
            _secure_directory(derived)
        data_path = derived / f"{turn_id}.pcm"
        index_path = derived / f"{turn_id}.jsonl"
        handles = (_open_new_or_append(data_path), _open_new_or_append(index_path))
        session.mono_handles[turn_id] = handles
        session.mono_checkpoint_counts.setdefault(turn_id, 0)
        if not any(item["turn_id"] == turn_id for item in session.manifest["derived_audio"]):
            session.manifest["derived_audio"].append({
                "turn_id": turn_id,
                "path": f"derived/{turn_id}.pcm",
                "index_path": f"derived/{turn_id}.jsonl",
                "sample_format": None,
                "sample_rate": None,
                "frame_count": 0,
                "sample_count": 0,
                "bytes": 0,
                "channel": None,
                "gain": None,
            })
        return handles

    @staticmethod
    def _validate_derived_consistency(
        session: _OpenSession,
        turn_id: str,
        frame: MonoFrame,
        channel: int | None,
        gain: float | None,
    ) -> None:
        entry = next(item for item in session.manifest["derived_audio"] if item["turn_id"] == turn_id)
        if entry["frame_count"] and (entry["sample_format"], entry["sample_rate"]) != (
            frame.sample_format, frame.sample_rate
        ):
            raise ArchiveValidationError("mono format changed within one turn")
        if entry["frame_count"] and (channel != entry["channel"] or gain != entry["gain"]):
            raise ArchiveValidationError("channel/gain changed within one turn")

    @staticmethod
    def _update_derived_manifest(
        session: _OpenSession,
        turn_id: str,
        frame: MonoFrame,
        channel: int | None,
        gain: float | None,
    ) -> None:
        entry = next(item for item in session.manifest["derived_audio"] if item["turn_id"] == turn_id)
        entry.update({
            "sample_format": frame.sample_format,
            "sample_rate": frame.sample_rate,
            "frame_count": entry["frame_count"] + 1,
            "sample_count": entry["sample_count"] + frame.sample_count,
            "bytes": entry["bytes"] + len(frame.data),
            "channel": channel,
            "gain": gain,
        })

    @staticmethod
    def _assert_open(session: _OpenSession) -> None:
        if session.closed:
            raise ArchiveRecordError("archive session is already closed")

    @staticmethod
    def _mark_write_error(session: _OpenSession, code: str) -> None:
        session.manifest["write_error"] = code
        session.manifest["complete"] = False
        session.manifest["status"] = "incomplete"
        try:
            _write_manifest(session.path, session.manifest)
        except OSError:
            pass

    def _ensure_root(self) -> None:
        try:
            if self.root.exists() and self.root.is_symlink():
                raise ArchiveValidationError("archive root must not be a symlink")
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            _secure_directory(self.root)
        except ArchiveError:
            raise
        except OSError as exc:
            raise ArchiveRecordError("could not create secure archive root") from exc


class ArchiveReader:
    """Read raw frames, derived frames, events, and debug payloads."""

    def __init__(self, root: str | os.PathLike[str] | None = None) -> None:
        self.root = Path(root).expanduser() if root is not None else DEFAULT_ARCHIVE_ROOT

    def read_manifest(self, capture_id: str) -> dict[str, Any]:
        path = self._session_path(capture_id) / "manifest.json"
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ArchiveSessionNotFoundError(f"manifest not found for capture {capture_id}") from exc
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArchiveRecordError(f"could not read archive manifest for capture {capture_id}") from exc
        if manifest.get("schema_version") != SCHEMA_VERSION:
            raise ArchiveRecordError("unsupported archive manifest schema version")
        return manifest

    def iter_frames(self, capture_id: str) -> Iterator[AudioFrame]:
        """Reconstruct all complete raw frames from the persisted index."""
        session_path = self._session_path(capture_id)
        manifest = self.read_manifest(capture_id)
        raw_path = session_path / manifest["raw_audio"]["path"]
        index_path = session_path / manifest["files"]["frame_index"]
        try:
            with raw_path.open("rb") as raw, index_path.open("rb") as index:
                for row in _iter_jsonl(index):
                    offset, length = row.get("offset"), row.get("bytes")
                    if not isinstance(offset, int) or not isinstance(length, int) or offset < 0 or length < 0:
                        raise ArchiveRecordError("invalid raw frame index entry")
                    raw.seek(offset)
                    pcm = raw.read(length)
                    if len(pcm) != length:
                        raise ArchiveIncompleteError("raw frame data is truncated")
                    yield AudioFrame(
                        timestamp_ns=int(row["timestamp_ns"]),
                        sample_rate=int(row["sample_rate"]),
                        channels=int(row["channels"]),
                        sample_format=str(row["sample_format"]),
                        sequence=int(row["sequence"]),
                        pcm=pcm,
                    )
        except FileNotFoundError as exc:
            raise ArchiveRecordError("archive raw data or frame index is missing") from exc

    def iter_mono(self, capture_id: str, turn_id: str) -> Iterator[MonoFrame]:
        """Reconstruct persisted model-input mono frames for a turn."""
        _validate_id(turn_id, "turn_id")
        session_path = self._session_path(capture_id)
        data_path = session_path / "derived" / f"{turn_id}.pcm"
        index_path = session_path / "derived" / f"{turn_id}.jsonl"
        try:
            with data_path.open("rb") as data, index_path.open("rb") as index:
                for row in _iter_jsonl(index):
                    offset, length = row.get("offset"), row.get("bytes")
                    if not isinstance(offset, int) or not isinstance(length, int) or offset < 0 or length < 0:
                        raise ArchiveRecordError("invalid mono frame index entry")
                    data.seek(offset)
                    pcm = data.read(length)
                    if len(pcm) != length:
                        raise ArchiveIncompleteError("derived mono frame data is truncated")
                    yield MonoFrame(
                        timestamp=float(row["timestamp"]),
                        sequence=int(row["sequence"]),
                        sample_rate=int(row["sample_rate"]),
                        sample_format=str(row["sample_format"]),
                        sample_count=int(row["sample_count"]),
                        data=pcm,
                    )
        except FileNotFoundError as exc:
            raise ArchiveSessionNotFoundError(f"derived mono archive not found for turn {turn_id}") from exc

    def iter_events(self, capture_id: str) -> Iterator[dict[str, Any]]:
        session_path = self._session_path(capture_id)
        try:
            with (session_path / "events.jsonl").open("rb") as handle:
                yield from _iter_jsonl(handle)
        except FileNotFoundError as exc:
            raise ArchiveRecordError("archive event index is missing") from exc

    def iter_debug_payloads(
        self,
        capture_id: str,
        turn_id: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        session_path = self._session_path(capture_id)
        if turn_id is not None:
            _validate_id(turn_id, "turn_id")
        try:
            with (session_path / "debug_payloads.jsonl").open("rb") as handle:
                for row in _iter_jsonl(handle):
                    if turn_id is None or row.get("turn_id") == turn_id:
                        yield row
        except FileNotFoundError as exc:
            raise ArchiveRecordError("archive debug payload index is missing") from exc

    def _session_path(self, capture_id: str) -> Path:
        _validate_id(capture_id, "capture_id")
        path = self.root / capture_id
        if not path.is_dir() or path.is_symlink():
            raise ArchiveSessionNotFoundError(f"archive not found for capture {capture_id}")
        return path


def _validate_id(value: str, name: str) -> None:
    if not isinstance(value, str) or not _ID_PATTERN.fullmatch(value):
        raise ArchiveValidationError(f"{name} must contain only ASCII letters, digits, '_' or '-' (max 128)")


def _validate_audio_frame(frame: AudioFrame) -> None:
    if not isinstance(frame, AudioFrame):
        raise ArchiveValidationError("frame must be an AudioFrame")
    if not isinstance(frame.timestamp_ns, int) or frame.timestamp_ns < 0:
        raise ArchiveValidationError("timestamp_ns must be a non-negative integer")
    if not isinstance(frame.sequence, int) or frame.sequence < 0:
        raise ArchiveValidationError("sequence must be a non-negative integer")
    if not isinstance(frame.sample_rate, int) or frame.sample_rate <= 0:
        raise ArchiveValidationError("sample_rate must be a positive integer")
    if not isinstance(frame.channels, int) or frame.channels <= 0:
        raise ArchiveValidationError("channels must be a positive integer")
    if frame.sample_format not in _FORMAT_WIDTHS:
        raise ArchiveValidationError(f"unsupported sample format: {frame.sample_format!r}")
    if not isinstance(frame.pcm, bytes):
        raise ArchiveValidationError("pcm must be bytes")
    frame_width = frame.channels * _FORMAT_WIDTHS[frame.sample_format]
    if len(frame.pcm) == 0 or len(frame.pcm) % frame_width:
        raise ArchiveValidationError("PCM bytes must contain complete non-empty interleaved samples")


def _validate_mono_frame(frame: MonoFrame) -> None:
    if not isinstance(frame, MonoFrame):
        raise ArchiveValidationError("frame must be a MonoFrame")
    if not math.isfinite(frame.timestamp) or frame.timestamp < 0:
        raise ArchiveValidationError("timestamp must be finite and non-negative")
    if not isinstance(frame.sequence, int) or frame.sequence < 0:
        raise ArchiveValidationError("sequence must be a non-negative integer")
    if frame.sample_rate <= 0 or frame.sample_format not in ("pcm_s16le", "float32_le"):
        raise ArchiveValidationError("unsupported mono sample rate or format")
    if not isinstance(frame.sample_count, int) or frame.sample_count <= 0:
        raise ArchiveValidationError("sample_count must be a positive integer")
    width = _FORMAT_WIDTHS[frame.sample_format]
    if not isinstance(frame.data, bytes) or len(frame.data) != frame.sample_count * width:
        raise ArchiveValidationError("mono data length does not match sample_count and format")


def _redact_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _redact_value(item, key_hint=str(key)) for key, item in value.items()}


def _redact_value(value: Any, key_hint: str | None = None) -> Any:
    if key_hint is not None and _is_secret_key(key_hint):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(key): _redact_value(item, key_hint=str(key)) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ArchiveValidationError("event values must not contain NaN or infinity")
        return value
    raise ArchiveValidationError(f"archive value is not JSON serializable: {type(value).__name__}")


def _is_secret_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
    return normalized in _SECRET_KEYS or normalized.endswith("apikey") or normalized.endswith("token") \
        or normalized.endswith("secret") or normalized.endswith("password") or normalized.endswith("credential")


def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _open_new(path: Path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(fd, "wb", buffering=0)


def _open_new_or_append(path: Path):
    if path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0))
    else:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(fd, "ab", buffering=0)


def _write_all(handle: Any, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = handle.write(view)
        if written is None or written <= 0:
            raise OSError("short write while persisting archive data")
        view = view[written:]


def _write_jsonl(handle: Any, row: Mapping[str, Any]) -> None:
    encoded = (json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    _write_all(handle, encoded)


def _write_manifest(session_path: Path, manifest: Mapping[str, Any]) -> None:
    temp_path = session_path / ".manifest.tmp"
    encoded = (json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    try:
        fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as handle:
            _write_all(handle, encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, session_path / "manifest.json")
        os.chmod(session_path / "manifest.json", 0o600)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _secure_directory(path: Path) -> None:
    if path.is_symlink():
        raise ArchiveValidationError("archive directories must not be symlinks")
    if hasattr(os, "getuid") and path.stat().st_uid != os.getuid():
        raise ArchiveValidationError("archive directory must be owned by the current user")
    os.chmod(path, 0o700)


def _iter_jsonl(handle: Any) -> Iterator[dict[str, Any]]:
    line_number = 0
    for raw_line in handle:
        line_number += 1
        if not raw_line.endswith(b"\n"):
            # A killed process may leave one partial tail row. Earlier frames
            # remain readable; the manifest stays incomplete.
            return
        try:
            row = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArchiveRecordError(f"invalid JSONL record at line {line_number}") from exc
        if not isinstance(row, dict):
            raise ArchiveRecordError(f"JSONL record at line {line_number} is not an object")
        yield row
