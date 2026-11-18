"""One-turn orchestration for Astero's half-duplex voice path.

The controller is intentionally an adapter boundary: audio input is an
iterable of mono frames, while wake, endpoint, ASR, conversation, and playback
objects are injected. Playback must expose ``announce(text)`` returning a
task identifier and ``get_status(task_id)`` returning one of queued,
generating, playing, completed, or failed. A 202/queued response is never
treated as playback completion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import threading
import time
from typing import Callable, Iterable, Protocol

from astero.audio.pcm import MonoFrame
from astero.speech.endpoint import EndpointKind, EndpointEvent, WakeEvent as EndpointWakeEvent


class State(str, Enum):
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    PROCESSING = "PROCESSING"
    SPEAKING = "SPEAKING"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class TurnStatus(str, Enum):
    COMPLETED = "completed"
    EMPTY_INPUT = "empty_input"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NO_WAKE = "no_wake"
    NO_SPEECH = "no_speech"


class WakeDetector(Protocol):
    def feed(self, frame: MonoFrame) -> Iterable[object]: ...


class EndpointCollector(Protocol):
    def wake(self, event: EndpointWakeEvent) -> Iterable[EndpointEvent]: ...
    def feed(self, frame: MonoFrame) -> Iterable[EndpointEvent]: ...
    def cancel(self, timestamp: float | None = None) -> EndpointEvent: ...
    def stream_lost(self, timestamp: float | None = None) -> EndpointEvent: ...


class Recognizer(Protocol):
    def recognize(self, frame: MonoFrame, **kwargs: object) -> object: ...


class Conversation(Protocol):
    def respond(self, text: str, session_id: str) -> str: ...


class Playback(Protocol):
    def announce(self, text: str) -> object: ...
    def get_status(self, task_id: object) -> object: ...


@dataclass(frozen=True, slots=True)
class OrchestratorConfig:
    session_id: str = "astero-local"
    max_listen_seconds: float = 120.0
    playback_timeout_seconds: float = 120.0
    playback_poll_seconds: float = 0.1


@dataclass(frozen=True, slots=True)
class TurnResult:
    status: TurnStatus
    transcript: str | None = None
    reply: str | None = None
    error: str | None = None
    bridge_task_id: object | None = None
    bridge_may_still_play: bool = False
    transitions: tuple[State, ...] = ()


@dataclass(slots=True)
class _Turn:
    state: State = State.IDLE
    transitions: list[State] = field(default_factory=lambda: [State.IDLE])


class Orchestrator:
    """Run one wake-to-playback turn and return to IDLE on every outcome.

    The source is not consumed during ``SPEAKING``. This pauses wake detection
    and ASR while the computer's speakers may be audible to the microphone.
    Cancellation is checked between frames and while polling the bridge. Since
    the bridge currently has no task cancellation API, cancelling after
    enqueueing is explicitly marked as possibly still playing.
    """

    _BRIDGE_PENDING = {"queued", "generating", "playing"}

    def __init__(
        self,
        wake_detector: WakeDetector,
        endpoint: EndpointCollector,
        asr: Recognizer,
        conversation: Conversation,
        playback: Playback,
        *,
        config: OrchestratorConfig | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.wake_detector = wake_detector
        self.endpoint = endpoint
        self.asr = asr
        self.conversation = conversation
        self.playback = playback
        self.config = config or OrchestratorConfig()
        self._clock = clock
        self._sleep = sleep
        self.state = State.IDLE

    def run_once(
        self,
        frames: Iterable[MonoFrame],
        *,
        cancel: threading.Event | None = None,
    ) -> TurnResult:
        """Consume one wake-command cycle. ``frames`` should already be mono."""
        if self.state is not State.IDLE:
            raise RuntimeError("orchestrator is already running")
        cancel = cancel or threading.Event()
        turn = _Turn()
        task_id: object | None = None
        transcript: str | None = None
        reply: str | None = None
        may_still_play = False
        wake_seen = False
        started = self._clock()
        self._transition(turn, State.LISTENING)
        try:
            for frame in frames:
                if cancel.is_set():
                    self._cancel_endpoint(frame.timestamp)
                    self._transition(turn, State.CANCELLED)
                    return self._result(turn, TurnStatus.CANCELLED)
                if self._clock() - started > self.config.max_listen_seconds:
                    self._cancel_endpoint(frame.timestamp)
                    status = TurnStatus.NO_WAKE if not wake_seen else TurnStatus.NO_SPEECH
                    return self._result(turn, status)

                endpoint_events: list[EndpointEvent] = []
                if not wake_seen:
                    # Keep the endpoint's bounded pre-roll warm before wake.
                    # The frame that triggers wake is fed once here, then
                    # included by endpoint.wake() from that pre-roll buffer.
                    endpoint_events.extend(self.endpoint.feed(frame))
                    wake_events = tuple(self.wake_detector.feed(frame))
                    if wake_events:
                        wake_seen = True
                        # One turn starts at the first event. Multiple events
                        # in a detector batch must not restart an active VAD.
                        endpoint_events.extend(self.endpoint.wake(_endpoint_wake(wake_events[0])))
                else:
                    endpoint_events.extend(self.endpoint.feed(frame))

                terminal = next((event for event in endpoint_events if event.kind in {
                    EndpointKind.ENDPOINT, EndpointKind.NO_SPEECH_TIMEOUT,
                    EndpointKind.MAX_DURATION, EndpointKind.STREAM_LOST,
                }), None)
                if terminal is None:
                    continue
                if terminal.kind is EndpointKind.STREAM_LOST:
                    raise ConnectionError("audio input disconnected during command capture")
                if terminal.segment is None:
                    return self._result(turn, TurnStatus.NO_SPEECH)

                self._transition(turn, State.PROCESSING)
                segment = terminal.segment
                segment_frame = MonoFrame(
                    timestamp=segment.start_timestamp,
                    sequence=segment.first_sequence,
                    sample_rate=segment.sample_rate,
                    sample_format=segment.sample_format,
                    sample_count=len(segment.data) // (2 if segment.sample_format == "pcm_s16le" else 4),
                    data=segment.data,
                )
                recognized = self.asr.recognize(segment_frame)
                transcript = _recognized_text(recognized)
                if not transcript:
                    return self._result(turn, TurnStatus.EMPTY_INPUT, transcript="")
                if cancel.is_set():
                    self._transition(turn, State.CANCELLED)
                    return self._result(turn, TurnStatus.CANCELLED, transcript)
                reply = self.conversation.respond(transcript, self.config.session_id)
                if not isinstance(reply, str) or not reply.strip():
                    raise ValueError("conversation backend returned empty reply text")
                reply = reply.strip()

                if cancel.is_set():
                    self._transition(turn, State.CANCELLED)
                    return self._result(turn, TurnStatus.CANCELLED, transcript, reply)
                self._transition(turn, State.SPEAKING)
                task_id = self.playback.announce(reply)
                if task_id is None or task_id == "":
                    raise RuntimeError("playback returned no task identifier")
                playback_started = self._clock()
                while True:
                    if cancel.is_set():
                        may_still_play = True
                        self._transition(turn, State.CANCELLED)
                        return self._result(turn, TurnStatus.CANCELLED, transcript, reply,
                                            task_id, may_still_play)
                    bridge_status = _status_text(self.playback.get_status(task_id))
                    if bridge_status == "completed":
                        return self._result(turn, TurnStatus.COMPLETED, transcript, reply, task_id)
                    if bridge_status == "failed":
                        raise RuntimeError("Voice Bridge reported playback failure")
                    if bridge_status not in self._BRIDGE_PENDING:
                        raise RuntimeError(f"Voice Bridge returned unknown status {bridge_status!r}")
                    if self._clock() - playback_started >= self.config.playback_timeout_seconds:
                        may_still_play = True
                        raise TimeoutError("Voice Bridge playback timed out; task may still be queued or playing")
                    self._sleep(min(self.config.playback_poll_seconds,
                                    max(0.0, self.config.playback_timeout_seconds - (self._clock() - playback_started))))
            if wake_seen:
                try:
                    self.endpoint.stream_lost()
                except Exception:
                    pass
                self._transition(turn, State.FAILED)
                return self._result(turn, TurnStatus.FAILED,
                                    error="ConnectionError: audio input ended before the command endpoint")
            # Clear the endpoint's bounded idle pre-roll so audio cannot carry
            # across a completed no-wake invocation.
            try:
                self.endpoint.stream_lost()
            except Exception:
                pass
            return self._result(turn, TurnStatus.NO_WAKE)
        except (KeyboardInterrupt, GeneratorExit):
            self._cancel_endpoint()
            self._transition(turn, State.CANCELLED)
            return self._result(turn, TurnStatus.CANCELLED, transcript, reply, task_id,
                                task_id is not None)
        except Exception as exc:
            self._cancel_endpoint()
            self._transition(turn, State.FAILED)
            # Avoid surfacing raw backend payloads that may contain private text.
            error = f"{type(exc).__name__}: {exc}"
            return self._result(turn, TurnStatus.FAILED, transcript, reply, task_id,
                                may_still_play or task_id is not None, error)
        finally:
            # Iterators backed by a capture adapter may use close() for device
            # shutdown. Never leave that resource owned by a completed turn.
            close = getattr(frames, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
            source_close = getattr(getattr(frames, "source", None), "close", None)
            if callable(source_close):
                try:
                    source_close()
                except Exception:
                    pass
            reset = getattr(self.wake_detector, "reset", None)
            if callable(reset):
                try:
                    reset()
                except Exception:
                    pass
            self.state = State.IDLE

    def _cancel_endpoint(self, timestamp: float | None = None) -> None:
        try:
            if getattr(self.endpoint, "active", False):
                self.endpoint.cancel(timestamp)
            else:
                # EndpointCollector.stream_lost also clears idle pre-roll.
                self.endpoint.stream_lost(timestamp)
        except Exception:
            pass

    def _transition(self, turn: _Turn, state: State) -> None:
        turn.state = state
        self.state = state
        turn.transitions.append(state)

    def _result(self, turn: _Turn, status: TurnStatus, transcript: str | None = None,
                reply: str | None = None, task_id: object | None = None,
                may_still_play: bool = False, error: str | None = None) -> TurnResult:
        # Expose the cleanup transition after the final meaningful state.
        if turn.state is not State.IDLE:
            turn.transitions.append(State.IDLE)
        return TurnResult(status, transcript, reply, error, task_id, may_still_play,
                          tuple(turn.transitions))


def _endpoint_wake(event: object) -> EndpointWakeEvent:
    timestamp = getattr(event, "timestamp", None)
    score = getattr(event, "score", None)
    if timestamp is None:
        raise TypeError("wake event must expose timestamp")
    return EndpointWakeEvent(float(timestamp), None if score is None else float(score))


def _recognized_text(result: object) -> str:
    text = result if isinstance(result, str) else getattr(result, "text", None)
    if not isinstance(text, str):
        raise TypeError("ASR result must be text or expose a text field")
    return text.strip()


def _status_text(status: object) -> str:
    value = status.get("status") if isinstance(status, dict) else getattr(status, "status", status)
    if not isinstance(value, str):
        value = getattr(value, "value", None)
    if not isinstance(value, str):
        raise TypeError("playback status must be a string or expose a string status")
    return value.strip().lower()


def manual_trigger(text: str, conversation: Conversation, playback: Playback,
                   *, session_id: str = "astero-local") -> TurnResult:
    """CLI-only text path for adapter smoke checks; it is not voice validation."""
    turn = _Turn()
    task_id = None
    try:
        if not isinstance(text, str) or not text.strip():
            return TurnResult(TurnStatus.EMPTY_INPUT, transcript="", transitions=(State.IDLE,))
        turn.transitions.append(State.PROCESSING)
        reply = conversation.respond(text.strip(), session_id)
        if not isinstance(reply, str) or not reply.strip():
            raise ValueError("conversation backend returned empty reply text")
        turn.transitions.append(State.SPEAKING)
        task_id = playback.announce(reply.strip())
        if task_id is None or task_id == "":
            raise RuntimeError("playback returned no task identifier")
        deadline = time.monotonic() + 120.0
        while time.monotonic() < deadline:
            status = _status_text(playback.get_status(task_id))
            if status == "completed":
                return TurnResult(TurnStatus.COMPLETED, text.strip(), reply.strip(),
                                  bridge_task_id=task_id,
                                  transitions=tuple(turn.transitions + [State.IDLE]))
            if status == "failed":
                raise RuntimeError("Voice Bridge reported playback failure")
            if status not in Orchestrator._BRIDGE_PENDING:
                raise RuntimeError(f"Voice Bridge returned unknown status {status!r}")
            time.sleep(0.1)
        return TurnResult(TurnStatus.FAILED, text.strip(), reply.strip(),
                          "Voice Bridge playback timed out; task may still be queued or playing",
                          task_id, True, tuple(turn.transitions + [State.IDLE]))
    except Exception as exc:
        return TurnResult(TurnStatus.FAILED, text.strip(), error=f"{type(exc).__name__}: {exc}",
                          transitions=tuple(turn.transitions + [State.FAILED, State.IDLE]))
