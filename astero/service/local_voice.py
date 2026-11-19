"""Real local Echo/ASR/agent/TTS executor used by the resident service."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import math
from pathlib import Path
from queue import Empty, Queue
import struct
import threading
import time
from typing import Iterator, Mapping

from astero.agent import AgentService, AgentServiceError
from astero.archive import ArchiveReader, ArchiveWriter
from astero.audio.capture import AdbCapture, AudioFrame
from astero.audio.pcm import MonoFrame, PCMFrame, convert_frame
from astero.speech.live_wake import with_alternate
from astero.observability import TraceContext, TraceRecorder
from astero.orchestrator import Orchestrator, OrchestratorConfig, TurnResult, TurnStatus
from astero.persistence import AsteroStore
from astero.speech.asr import SherpaOnnxASR, ZipformerConfig
from astero.speech.endpoint import EndpointCollector, EndpointConfig, EndpointKind
from astero.speech.voice_bridge import VoiceBridgeClient, VoiceBridgeConfig

from astero.live import CodexConversation, SenseVoice, SherpaKeyword
from astero.speech.live_vad import SileroVAD


@dataclass(frozen=True, slots=True)
class LocalVoiceConfig:
    data_root: Path = Path.home() / ".local" / "share" / "astero"
    helper_path: Path = Path.home() / ".local" / "share" / "astero" / "bin" / "capture-bionic"
    model_root: Path = Path.home() / ".local" / "share" / "astero" / "models"
    channel: int = 3
    wake_secondary_channel: int = 5
    gain: float = 16.0
    max_listen_seconds: float = 120.0
    playback_timeout_seconds: float = 180.0
    end_silence_seconds: float = 0.8
    voice_bridge_url: str = "http://127.0.0.1:8765"

    def __post_init__(self) -> None:
        if not 0 <= self.channel <= 6:
            raise ValueError("channel must select microphone channel 0 through 6")
        if not 0 <= self.wake_secondary_channel <= 6:
            raise ValueError("wake secondary channel must select microphone channel 0 through 6")
        if not math.isfinite(self.gain) or not 0 < self.gain <= 100:
            raise ValueError("gain must be greater than 0 and at most 100")
        if not math.isfinite(self.end_silence_seconds) or self.end_silence_seconds <= 0:
            raise ValueError("end silence must be finite and positive")
        if self.max_listen_seconds <= 0 or self.playback_timeout_seconds <= 0:
            raise ValueError("listen and playback timeouts must be positive")


@dataclass(frozen=True, slots=True)
class LocalVoiceResult:
    status: str
    capture_id: str | None
    archive_complete: bool
    frame_count: int
    transcript: str | None = None
    reply: str | None = None
    error_code: str | None = None
    bridge_task_id: str | None = None
    total_elapsed_ms: float = 0.0


class LocalVoiceTurnExecutor:
    """Open a capture session per requested voice turn and run the full chain."""

    def __init__(self, store: AsteroStore, config: LocalVoiceConfig | None = None,
                 *, bridge: VoiceBridgeClient | None = None) -> None:
        self.store = store
        self.config = config or LocalVoiceConfig()
        self.bridge = bridge or VoiceBridgeClient(VoiceBridgeConfig(base_url=self.config.voice_bridge_url))
        self._voice_lock = threading.Lock()
        self._cancel = threading.Event()
        self._active_pump = None
        self._activity = {"state": "idle", "listening": False, "active_turn_id": None}
        self._ready_components: dict[str, str] = {}
        self._startup_error: str | None = None
        self.wake = None
        self.asr = None
        try:
            self._load_models()
        except Exception as exc:
            self._startup_error = type(exc).__name__
            self._ready_components = {"capture": "not_ready", "wake": "not_ready",
                                      "vad": "not_ready", "asr": "not_ready",
                                      "agent": "configured", "tts": "unknown"}

    def _load_models(self) -> None:
        model_root = self.config.model_root
        kws = model_root / "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20"
        keywords = model_root / "astero-keywords.txt"
        asr_dir = model_root / "asr"
        sensevoice = model_root / "sensevoice"
        helper = self.config.helper_path
        missing = [str(path) for path in (helper, keywords,
                   kws / "tokens.txt", asr_dir / "tokens.txt",
                   sensevoice / "model.int8.onnx", model_root / "silero_vad.onnx")
                   if not path.is_file()]
        if missing:
            raise RuntimeError("local voice resources are missing: " + ", ".join(missing))
        self.helper_sha256 = hashlib.sha256(helper.read_bytes()).hexdigest()
        self.wake = SherpaKeyword(kws, keywords)
        zipformer = ZipformerConfig(
            encoder=asr_dir / "encoder-epoch-99-avg-1.int8.onnx",
            decoder=asr_dir / "decoder-epoch-99-avg-1.int8.onnx",
            joiner=asr_dir / "joiner-epoch-99-avg-1.int8.onnx",
            tokens=asr_dir / "tokens.txt",
        )
        self.asr = SherpaOnnxASR(zipformer, recognizer=SenseVoice(sensevoice))
        self._ready_components = {"capture": "ready", "wake": "ready", "vad": "ready",
                                  "asr": "ready", "agent": "configured", "tts": "unknown"}

    def readiness(self) -> Mapping[str, object]:
        dependencies = dict(self._ready_components)
        ready = self._startup_error is None
        if self._startup_error:
            dependencies["startup_error"] = self._startup_error
        try:
            self.bridge.health()
            dependencies["tts"] = "ready"
        except Exception as exc:
            dependencies["tts"] = type(exc).__name__
            ready = False
        if self.wake is None or self.asr is None:
            ready = False
        return {"ready": ready, "dependencies": dependencies}

    def activity(self) -> dict[str, object]:
        return dict(self._activity)

    def request_stop(self) -> None:
        self._cancel.set()
        pump = self._active_pump
        if pump is not None:
            pump.close()

    def _observe(self, event) -> None:
        if event.stage == "wake_detection" and event.event == "started":
            self._activity = {**self._activity, "state": "listening", "listening": True}
        elif event.stage == "wake_detection" and event.status == "detected":
            self._activity = {**self._activity, "state": "capturing_command", "listening": False}
        elif event.stage in {"asr", "llm_request", "tts_enqueue", "playback"}:
            self._activity = {**self._activity, "state": event.stage, "listening": False}

    def __call__(self, request) -> object:
        return self.run_turn(request)

    def run_turn(self, request) -> TurnResult:
        if self._cancel.is_set():
            return TurnResult(TurnStatus.CANCELLED)
        if self.wake is None or self.asr is None:
            return TurnResult(TurnStatus.FAILED, error="voice_runtime_not_ready")
        if not self._voice_lock.acquire(blocking=False):
            return TurnResult(TurnStatus.FAILED, error="voice_capture_busy")
        try:
            return self._run_locked(request)
        finally:
            self._active_pump = None
            self._activity = {"state": "idle", "listening": False, "active_turn_id": None}
            self._voice_lock.release()

    def _run_locked(self, request):
        self._activity = {"state": "starting_capture", "listening": False, "active_turn_id": request.turn_id}
        writer = ArchiveWriter(self.config.data_root / "archives")
        started_ns = time.monotonic_ns()
        started_utc = _utc_now()
        capture_id = writer.begin_session({
            "turn_id": request.turn_id,
            "trace_id": request.trace_id,
            "session_id": request.session_id,
            "sample_rate": 16000,
            "channels": 9,
            "sample_format": "s24_3le",
            "microphone_channel": self.config.channel,
            "wake_channels": list(dict.fromkeys((self.config.channel, self.config.wake_secondary_channel))),
            "gain": self.config.gain,
            "input_kind": "live_echo_microphone",
        })
        context = TraceContext(request.trace_id, request.turn_id,
                               request.session_id, capture_id)
        recorder = TraceRecorder(sink=_CompositeTraceSink(self.store, writer, self._observe))
        frame_source = None
        try:
            self.store.put_archive_index(capture_id, str(writer.root / capture_id / "manifest.json"),
                                         started_utc, turn_id=request.turn_id)
            with recorder.stage(context, "capture", attributes={"source": "echo_usb_adb"}):
                capture = AdbCapture(self.config.helper_path, self.helper_sha256)
                with capture:
                    recorder.emit(context, "capture_ready", "completed", status="ready")
                    frame_source = _ArchivedCapturePump(
                        capture, writer, capture_id, request.turn_id, self.config,
                        context, recorder,
                    )
                    self._active_pump = frame_source
                    if self._cancel.is_set():
                        frame_source.close()
                    endpoint = _TraceEndpoint(
                        EndpointCollector(SileroVAD(self.config.model_root / "silero_vad.onnx"),
                                          EndpointConfig(pre_roll_seconds=0,
                                                         pre_roll_buffer_seconds=0,
                                                         end_silence_seconds=self.config.end_silence_seconds)),
                        recorder, context,
                    )
                    agent = AgentService(
                        self.store, CodexConversation(), trace_recorder=recorder,
                        archive_payload=lambda cap, turn, kind, payload:
                            writer.append_debug_payload(cap or capture_id, turn, kind, payload),
                        manage_turn_state=False,
                    )
                    conversation = _TurnConversation(agent, request, capture_id)
                    asr = _TraceASR(self.asr, recorder, context,
                                    lambda kind, payload: writer.append_debug_payload(
                                        capture_id, request.turn_id, kind, payload),
                                    capture_pump=frame_source)
                    playback = _TracePlayback(self.bridge, recorder, context,
                                              lambda text: writer.append_debug_payload(
                                                  capture_id, request.turn_id, "tts_request",
                                                  {"text": text}), frame_source)
                    orchestrator = Orchestrator(
                        _TraceWake(self.wake, recorder, context), endpoint, asr,
                        conversation, playback,
                        config=OrchestratorConfig(
                            session_id=request.session_id,
                            max_listen_seconds=self.config.max_listen_seconds,
                            playback_timeout_seconds=self.config.playback_timeout_seconds,
                        ),
                    )
                    result = orchestrator.run_once(frame_source, cancel=self._cancel)
                    if frame_source._close_error is not None:
                        raise RuntimeError("Echo capture cleanup failed") from frame_source._close_error
                    if frame_source._error is not None:
                        # Playback success cannot make a partial capture valid.
                        raise frame_source._error
            writer.append_event(capture_id, {
                "trace_id": request.trace_id, "turn_id": request.turn_id,
                "session_id": request.session_id, "stage": "turn_result",
                "event": "completed" if result.status is TurnStatus.COMPLETED else "failed",
                "status": result.status.value, "error_code": _error_code(result.error),
                "transcript": result.transcript,
            })
            capture_complete = frame_source is None or frame_source._error is None
            writer.close_session(capture_id, outcome="clean" if capture_complete else "failed")
            manifest = ArchiveReader(writer.root).read_manifest(capture_id)
            if not manifest.get("complete"):
                raise RuntimeError("capture archive incomplete")
            self.store.put_archive_index(
                capture_id, str(writer.root / capture_id / "manifest.json"), started_utc,
                turn_id=request.turn_id, ended_at_utc=manifest.get("ended_at_utc"),
                frame_count=manifest.get("raw_audio", {}).get("frame_count", 0),
                complete=bool(manifest.get("complete")),
            )
            return LocalVoiceResult(
                status="cancelled" if self._cancel.is_set() else result.status.value, capture_id=capture_id,
                archive_complete=bool(manifest.get("complete")),
                frame_count=int(manifest.get("raw_audio", {}).get("frame_count", 0)),
                transcript=result.transcript, reply=result.reply,
                error_code=_error_code(result.error),
                bridge_task_id=(str(result.bridge_task_id)
                                if result.bridge_task_id is not None else None),
                total_elapsed_ms=max(0.0, (time.monotonic_ns() - started_ns) / 1_000_000),
            )
        except Exception as exc:
            try:
                recorder.emit(context, "capture_archive", "failed", status="failed",
                              error_code=_error_code(exc))
            except Exception:
                pass
            try:
                if frame_source is not None:
                    frame_source.close()
            except Exception:
                pass
            try:
                writer.close_session(capture_id, outcome="failed")
            except Exception:
                pass
            frame_count = 0
            complete = False
            ended_utc = None
            try:
                manifest = ArchiveReader(writer.root).read_manifest(capture_id)
                frame_count = int(manifest.get("raw_audio", {}).get("frame_count", 0))
                complete = bool(manifest.get("complete"))
                ended_utc = manifest.get("ended_at_utc")
            except Exception:
                pass
            try:
                self.store.put_archive_index(
                    capture_id, str(writer.root / capture_id / "manifest.json"), started_utc,
                    turn_id=request.turn_id, ended_at_utc=ended_utc,
                    frame_count=frame_count, complete=complete,
                    error_code=_error_code(exc),
                )
            except Exception:
                pass
            return LocalVoiceResult(
                status="failed", capture_id=capture_id, archive_complete=complete,
                frame_count=frame_count, error_code=_error_code(exc),
                total_elapsed_ms=max(0.0, (time.monotonic_ns() - started_ns) / 1_000_000),
            )

    def close(self) -> None:
        if self.asr is not None:
            self.asr.close()


class _ArchivedCapturePump:
    """Continuously archive raw and derived frames, yielding only live input.

    The producer keeps draining and archiving the microphone while Astero is
    speaking. Playback pauses delivery to wake/VAD and clears stale queued
    frames so the reply cannot trigger the next recognition turn.
    """

    def __init__(self, capture, writer, capture_id, turn_id, config, context, recorder):
        self.capture = capture
        self.writer = writer
        self.capture_id = capture_id
        self.turn_id = turn_id
        self.config = config
        self.context = context
        self.recorder = recorder
        self._iterator = None
        self._closed = False
        self._close_lock = threading.Lock()
        self._started = False
        self._done = threading.Event()
        self._stop = threading.Event()
        self._closing = threading.Event()
        self._processing = threading.Event()
        self._processing.set()
        self._queue: Queue[MonoFrame] = Queue(maxsize=200)
        self._thread = threading.Thread(target=self._produce, name="astero-audio-archive", daemon=True)
        self._error: BaseException | None = None
        self._close_error: BaseException | None = None
        self._batch_count = 0
        self._batch_bytes = 0
        self._first_sequence = None
        self._last_sequence = None
        self._batch_start_ns = None

    def __iter__(self) -> Iterator[MonoFrame]:
        if not self._started:
            self._started = True
            self._thread.start()
        while not self._closing.is_set() and not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.05)
            except Empty:
                if self._done.is_set():
                    if self._error is not None:
                        raise self._error
                    return
                continue
            yield item

    def _produce(self) -> None:
        self._iterator = iter(self.capture.frames())
        try:
            while not self._stop.is_set():
                try:
                    raw = next(self._iterator)
                except StopIteration:
                    break
                except (OSError, ValueError):
                    # capture.close may close an in-flight pipe read after the
                    # helper exits. Only source I/O errors are expected here;
                    # archive/conversion failures below must remain visible.
                    if self._closing.is_set():
                        break
                    raise
                self.writer.append_frame(self.capture_id, raw)
                mono = convert_frame(
                    PCMFrame(raw.timestamp_ns / 1e9, raw.sequence, raw.sample_rate,
                             raw.channels, raw.sample_format, raw.pcm),
                    channel=self.config.channel,
                )
                if self.config.gain != 1:
                    mono = _apply_gain(mono, self.config.gain)
                self.writer.append_mono(
                    self.capture_id, self.turn_id, mono,
                    channel=self.config.channel, gain=self.config.gain,
                    source_sequence=raw.sequence,
                )
                if self.config.wake_secondary_channel != self.config.channel:
                    alternate = convert_frame(
                        PCMFrame(raw.timestamp_ns / 1e9, raw.sequence, raw.sample_rate,
                                 raw.channels, raw.sample_format, raw.pcm),
                        channel=self.config.wake_secondary_channel,
                    )
                    if self.config.gain != 1:
                        alternate = _apply_gain(alternate, self.config.gain)
                    mono = with_alternate(mono, alternate)
                self._record_batch(raw, len(raw.pcm))
                while self._processing.is_set() and not self._stop.is_set():
                    try:
                        self._queue.put(mono, timeout=0.05)
                        break
                    except Exception:
                        continue
        except BaseException as exc:
            self._error = exc
        finally:
            try:
                close = getattr(self._iterator, "close", None)
                if callable(close):
                    close()
            except Exception:
                pass
            try:
                self._flush_batch()
            except BaseException as exc:
                self._error = self._error or exc
            self._done.set()

    def pause_processing(self) -> None:
        self._processing.clear()
        while True:
            try:
                self._queue.get_nowait()
            except Empty:
                return

    def _record_batch(self, frame: AudioFrame, size: int) -> None:
        if self._first_sequence is None:
            self._first_sequence = frame.sequence
            self._batch_start_ns = time.monotonic_ns()
        self._last_sequence = frame.sequence
        self._batch_count += 1
        self._batch_bytes += size
        if self._batch_count >= 100:
            self._flush_batch()

    def _flush_batch(self) -> None:
        if not self._batch_count:
            return
        now = time.monotonic_ns()
        start = self._batch_start_ns or now
        attrs = {"frame_count": self._batch_count, "byte_count": self._batch_bytes,
                 "first_sequence": self._first_sequence,
                 "last_sequence": self._last_sequence}
        self.recorder.emit(self.context, "capture_frame_batch", "started",
                           monotonic_ns=start, status="receiving", attributes=attrs)
        self.recorder.emit(self.context, "capture_frame_batch", "completed",
                           monotonic_ns=now,
                           elapsed_ms=max(0.0, (now - start) / 1_000_000),
                           status="archived", attributes=attrs)
        self._batch_count = self._batch_bytes = 0
        self._first_sequence = self._last_sequence = self._batch_start_ns = None

    def close(self) -> None:
        # The supervisor and orchestrator can stop concurrently. Every caller
        # waits for cleanup, so no caller finalizes the archive mid-write.
        with self._close_lock:
            self._close_locked()

    def _close_locked(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._closing.set()
        self.pause_processing()
        if self._started:
            # Keep draining and archiving stdout while close sends q and waits
            # for the helper. Stopping the reader first can fill the pipe and
            # prevent the helper from reaching its control-input read.
            try:
                self.capture.close()
            except Exception as exc:
                self._close_error = exc
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                self._close_error = RuntimeError("capture archive producer did not stop")
                self._error = self._error or self._close_error
        self._stop.set()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class _TraceWake:
    def __init__(self, detector, recorder, context):
        self.detector = detector
        self.recorder = recorder
        self.context = context
        self._timer = None

    def feed(self, frame):
        if self._timer is None:
            self._timer = self.recorder.stage(self.context, "wake_detection")
            self._timer.__enter__()
        events = tuple(self.detector.feed(frame))
        if events and self._timer is not None:
            self._timer.succeed(status="detected")
            self._timer = None
        return events

    def reset(self):
        if self._timer is not None:
            self._timer.succeed(status="no_wake")
            self._timer = None
        reset = getattr(self.detector, "reset", None)
        if callable(reset):
            reset()


class _TraceEndpoint:
    _TERMINAL = {EndpointKind.ENDPOINT, EndpointKind.NO_SPEECH_TIMEOUT,
                 EndpointKind.MAX_DURATION, EndpointKind.STREAM_LOST,
                 EndpointKind.CANCELLED}

    def __init__(self, endpoint, recorder, context):
        self.endpoint = endpoint
        self.recorder = recorder
        self.context = context
        self._timer = None

    @property
    def active(self):
        return self.endpoint.active

    def wake(self, event):
        self._timer = self.recorder.stage(self.context, "endpoint")
        self._timer.__enter__()
        events = tuple(self.endpoint.wake(event))
        self._finish(events)
        return events

    def feed(self, frame):
        events = tuple(self.endpoint.feed(frame))
        self._finish(events)
        return events

    def cancel(self, timestamp=None):
        result = self.endpoint.cancel(timestamp)
        self._finish((result,))
        return result

    def stream_lost(self, timestamp=None):
        result = self.endpoint.stream_lost(timestamp)
        self._finish((result,))
        return result

    def _finish(self, events):
        if self._timer is None:
            return
        terminal = next((event for event in events if event.kind in self._TERMINAL), None)
        if terminal is None:
            return
        if terminal.kind is EndpointKind.STREAM_LOST:
            self._timer.fail("audio_stream_lost")
        elif terminal.kind is EndpointKind.CANCELLED:
            self._timer.fail("cancelled")
        else:
            self._timer.succeed(status=terminal.kind.value)
        self._timer = None


class _TraceASR:
    def __init__(self, asr, recorder, context, archive_payload, *, capture_pump=None):
        self.capture_pump = capture_pump
        self.asr = asr
        self.recorder = recorder
        self.context = context
        self.archive_payload = archive_payload

    def recognize(self, frame, **kwargs):
        # Endpoint is final; no more frames are needed by wake/VAD. Keep raw
        # draining independent of slow ASR/LLM instead of filling the queue.
        if self.capture_pump is not None:
            self.capture_pump.pause_processing()
        with self.recorder.stage(self.context, "asr"):
            result = self.asr.recognize(frame, **kwargs)
        self.archive_payload("asr_result", {
            "text": getattr(result, "text", ""),
            "language": getattr(result, "language", None),
            "elapsed_seconds": getattr(result, "elapsed_seconds", None),
            "audio_duration_seconds": getattr(result, "audio_duration_seconds", None),
        })
        return result


class _TracePlayback:
    def __init__(self, playback, recorder, context, archive_payload, capture_pump):
        self.playback = playback
        self.recorder = recorder
        self.context = context
        self.archive_payload = archive_payload
        self.capture_pump = capture_pump
        self._playback_timer = None
        self._announce_ns = None
        self._first_playing_recorded = False

    def announce(self, text):
        self.capture_pump.pause_processing()
        self.archive_payload(text)
        self._announce_ns = time.monotonic_ns()
        self._first_playing_recorded = False
        with self.recorder.stage(self.context, "tts_enqueue"):
            return self.playback.announce(text)

    def get_status(self, task_id):
        if self._playback_timer is None:
            self._playback_timer = self.recorder.stage(self.context, "playback")
            self._playback_timer.__enter__()
        try:
            result = self.playback.get_status(task_id)
        except Exception as exc:
            self._playback_timer.fail(_error_code(exc))
            self._playback_timer = None
            raise
        status = result.get("status") if isinstance(result, Mapping) else getattr(result, "status", result)
        if not self._first_playing_recorded and status in {"playing", "completed", "failed"}:
            now = time.monotonic_ns()
            observed = status == "playing"
            self.recorder.emit(
                self.context, "first_playing_observed", "completed" if observed else "skipped",
                monotonic_ns=now,
                elapsed_ms=(max(0.0, (now - self._announce_ns) / 1_000_000)
                            if observed and self._announce_ns is not None else None),
                status="playing" if observed else "not_observed",
            )
            self._first_playing_recorded = True
        if status in {"completed", "failed"}:
            if status == "completed":
                self._playback_timer.succeed(status="completed")
            else:
                self._playback_timer.fail("tts_playback_failed")
            self._playback_timer = None
        return result


class _TurnConversation:
    def __init__(self, service: AgentService, request, capture_id: str):
        self.service = service
        self.request = request
        self.capture_id = capture_id

    def respond(self, text: str, session_id: str) -> str:
        result = self.service.submit_turn(
            text, session_id, trace_id=self.request.trace_id,
            turn_id=self.request.turn_id, capture_id=self.capture_id,
        )
        if result.status != "completed" or not result.reply:
            raise AgentServiceError(result.error_code or "agent_turn_failed")
        return result.reply


class _CompositeTraceSink:
    def __init__(self, store: AsteroStore, archive: ArchiveWriter, observer=None):
        self.store = store
        self.archive = archive
        self.observer = observer

    def __call__(self, event) -> None:
        self.store.append_stage_event(
            event.event_id, event.trace_id, event.turn_id, event.session_id,
            event.stage, event.event, event.timestamp_utc, event.elapsed_ms,
            event.status, event.attempt, event.attributes,
            monotonic_ns=event.monotonic_ns,
        )
        if event.capture_id:
            self.archive.append_event(event.capture_id, event.as_dict())
        if self.observer is not None:
            self.observer(event)


def _apply_gain(frame: MonoFrame, gain: float) -> MonoFrame:
    if frame.sample_format != "pcm_s16le":
        raise ValueError("gain processing currently requires pcm_s16le")
    values = (max(-32768, min(32767, round(sample * gain)))
              for (sample,) in struct.iter_unpack("<h", frame.data))
    data = b"".join(struct.pack("<h", value) for value in values)
    return MonoFrame(frame.timestamp, frame.sequence, frame.sample_rate,
                     frame.sample_format, frame.sample_count, data)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _error_code(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        name = value.split(":", 1)[0].strip() or "turn_failed"
        if not all(char.isalnum() or char in "_-" for char in name):
            name = "turn_failed"
    else:
        name = type(value).__name__
    return "".join(("_" + char.lower()) if char.isupper() else char for char in name).lstrip("_")
