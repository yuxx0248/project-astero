"""USB capture from Echo Dot 2 via a short-lived, source-built device helper.

The helper writes unframed interleaved PCM to stdout. This module frames that
byte stream on the host and owns the temporary helper and ``mixer`` lifecycle.
No audio is written to a file by this module.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import subprocess
import threading
import time
from typing import BinaryIO, Iterator, Protocol
import uuid


SAMPLE_RATE = 16_000
CHANNELS = 9
SAMPLE_FORMAT = "s24_3le"
BYTES_PER_SAMPLE = 3


class CaptureError(RuntimeError):
    """Capture could not start, continue, or stop cleanly."""


class CaptureDisconnected(CaptureError):
    """The USB/ADB stream ended before a clean stop was requested."""


@dataclass(frozen=True)
class AudioFrame:
    timestamp_ns: int
    sample_rate: int
    channels: int
    sample_format: str
    sequence: int
    pcm: bytes

    @property
    def sample_count(self) -> int:
        return len(self.pcm) // (self.channels * BYTES_PER_SAMPLE)


class FrameSource(Protocol):
    """Minimal replaceable source contract for downstream components/tests."""

    def frames(self) -> Iterator[AudioFrame]: ...


class AdbCapture:
    """Run a verified temporary helper on a root ADB-connected Dot.

    ``helper`` must be built from the checked-in ``capture_helper.c`` and its
    SHA-256 must be supplied explicitly. The executable is pushed to a unique
    path under ``/data/local/tmp`` and removed on exit. The helper emits raw
    PCM only; frame timestamps use the host monotonic clock.
    """

    def __init__(
        self,
        helper: str | os.PathLike[str],
        expected_sha256: str,
        *,
        adb: str = "adb",
        serial: str | None = None,
        device: int = 0,
        pcm: int = 24,
        frame_samples: int = 160,
        command_timeout: float = 5.0,
        runner=subprocess.run,
        popen=subprocess.Popen,
    ) -> None:
        if frame_samples <= 0:
            raise ValueError("frame_samples must be positive")
        if len(expected_sha256) != 64:
            raise ValueError("expected_sha256 must be a 64-character SHA-256")
        self.helper = Path(helper)
        self.expected_sha256 = expected_sha256.lower()
        self.adb = adb
        self.serial = serial
        self.device = device
        self.pcm = pcm
        self.frame_samples = frame_samples
        self.command_timeout = command_timeout
        self._run = runner
        self._popen = popen
        self._remote: str | None = None
        self._mixer_before: str | None = None
        self._proc: subprocess.Popen[bytes] | None = None
        self._sequence = 0
        self._closed = False
        self._reader_thread_id: int | None = None
        self._reader_done = threading.Event()
        self._reader_done.set()

    def _adb(self, *args: str, timeout: float | None = None, check: bool = True):
        command = [self.adb]
        if self.serial:
            command += ["-s", self.serial]
        command += list(args)
        result = self._run(
            command, check=False, capture_output=True, text=True,
            timeout=timeout or self.command_timeout,
        )
        if check and result.returncode:
            raise CaptureError(f"ADB command failed ({result.returncode}): {result.stderr.strip()}")
        return result

    def _shell(self, command: str, *, check: bool = True) -> str:
        return self._adb("shell", command, check=check).stdout.strip()

    def _wait_mixer(self, expected: str) -> None:
        deadline = time.monotonic() + self.command_timeout
        while time.monotonic() < deadline:
            state = self._shell("getprop init.svc.mixer")
            if state == expected:
                return
            time.sleep(0.1)
        raise CaptureError(f"mixer did not reach {expected!r}; last state was {state!r}")

    def __enter__(self) -> "AdbCapture":
        if not self.helper.is_file():
            raise CaptureError(f"device helper not found: {self.helper}")
        digest = hashlib.sha256(self.helper.read_bytes()).hexdigest()
        if digest != self.expected_sha256:
            raise CaptureError("device helper SHA-256 does not match the supplied build hash")
        uid = self._shell("id -u")
        if uid != "0":
            raise CaptureError(f"root ADB is required to take the PCM device; device uid is {uid!r}")
        state = self._shell("getprop init.svc.mixer")
        if state not in {"running", "stopped"}:
            raise CaptureError(f"unexpected init.svc.mixer state: {state!r}")
        self._mixer_before = state
        self._remote = f"/data/local/tmp/astero-capture-{uuid.uuid4().hex}"
        try:
            self._adb("push", str(self.helper), self._remote)
            self._shell(f"chmod 700 {self._remote}")
            if state == "running":
                self._shell("setprop ctl.stop mixer")
                self._wait_mixer("stopped")
            self._proc = self._popen(
                # -T disables PTY byte transformations and forwards stdin so
                # q reaches the helper. exec-out on this device loses stdin.
                # Suppress remote linker stderr to protect PCM alignment.
                self._adb_args("shell", "-T", f"exec {self._remote} {self.device} {self.pcm} 2>/dev/null"),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
            )
            return self
        except BaseException:
            self.close()
            raise

    def _adb_args(self, *args: str) -> list[str]:
        command = [self.adb]
        if self.serial:
            command += ["-s", self.serial]
        return command + list(args)

    def frames(self) -> Iterator[AudioFrame]:
        # A running producer must finish reading buffered stdout before close
        # closes the pipe. An unstarted generator never marks a reader active.
        if self._proc is None or self._proc.stdout is None or self._closed:
            raise CaptureError("capture is not active")
        self._reader_thread_id = threading.get_ident()
        self._reader_done.clear()
        try:
            yield from self._read_frames()
        finally:
            self._reader_done.set()
            self._reader_thread_id = None

    def _read_frames(self) -> Iterator[AudioFrame]:
        size = self.frame_samples * CHANNELS * BYTES_PER_SAMPLE
        stream: BinaryIO = self._proc.stdout
        while True:
            chunk = bytearray()
            while len(chunk) < size:
                part = stream.read(size - len(chunk))
                if not part:
                    if self._closed and self._proc.poll() in (None, 0):
                        if chunk and len(chunk) % (CHANNELS * BYTES_PER_SAMPLE) == 0:
                            # Device writes 256-sample blocks, while host frames
                            # default to 160. A clean stop can leave a short but
                            # complete group of interleaved samples: retain it.
                            self._sequence += 1
                            yield AudioFrame(time.monotonic_ns(), SAMPLE_RATE, CHANNELS,
                                             SAMPLE_FORMAT, self._sequence, bytes(chunk))
                            return
                        if not chunk:
                            return
                    if chunk:
                        raise CaptureError(
                            f"capture ended mid-frame ({len(chunk)} of {size} bytes); frame dropped"
                        )
                    code = self._proc.poll()
                    err = b""
                    if self._proc.stderr:
                        err = self._proc.stderr.read()
                    if self._closed:
                        return
                    if code == 0:
                        raise CaptureDisconnected("device capture helper ended unexpectedly")
                    detail = err.decode("utf-8", "replace").strip()
                    raise CaptureError(f"device capture failed (exit {code}): {detail}")
                chunk.extend(part)
            self._sequence += 1
            yield AudioFrame(
                timestamp_ns=time.monotonic_ns(), sample_rate=SAMPLE_RATE,
                channels=CHANNELS, sample_format=SAMPLE_FORMAT,
                sequence=self._sequence, pcm=bytes(chunk),
            )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        errors: list[str] = []
        if self._proc is not None:
            if getattr(self._proc, "stdin", None):
                try:
                    self._proc.stdin.write(b"q")
                    self._proc.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
            if self._proc.poll() is None:
                try:
                    self._proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    errors.append("capture helper did not acknowledge stop")
                    self._proc.terminate()
                    try:
                        self._proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self._proc.kill()
                        self._proc.wait(timeout=2)
            reader = self._reader_thread_id
            if reader is not None and reader != threading.get_ident():
                if not self._reader_done.wait(timeout=2):
                    errors.append("capture reader did not finish draining stdout")
            if self._proc.stdout:
                self._proc.stdout.close()
            if self._proc.stderr:
                self._proc.stderr.close()
        if self._remote is not None:
            try:
                self._shell(f"rm -f {self._remote}")
            except Exception as exc:
                errors.append(f"temporary helper cleanup failed: {exc}")
        if self._mixer_before == "running":
            try:
                self._shell("setprop ctl.start mixer")
                self._wait_mixer("running")
            except Exception as exc:
                errors.append(f"mixer restoration failed: {exc}")
        if errors:
            raise CaptureError("; ".join(errors))

    def __exit__(self, exc_type, exc, traceback) -> bool:
        try:
            self.close()
        except CaptureError as cleanup_error:
            if exc is None:
                raise
            exc.add_note(f"capture cleanup also failed: {cleanup_error}")
        return False


class SyntheticCapture:
    """Deterministic in-memory frame source used by unit tests and adapters."""

    def __init__(self, payloads: Iterator[bytes], *, frame_samples: int) -> None:
        self._payloads = payloads
        self._frame_samples = frame_samples

    def frames(self) -> Iterator[AudioFrame]:
        expected = self._frame_samples * CHANNELS * BYTES_PER_SAMPLE
        for sequence, payload in enumerate(self._payloads, start=1):
            if len(payload) != expected:
                raise CaptureError(f"synthetic frame {sequence} has {len(payload)} bytes; expected {expected}")
            yield AudioFrame(time.monotonic_ns(), SAMPLE_RATE, CHANNELS, SAMPLE_FORMAT, sequence, payload)
