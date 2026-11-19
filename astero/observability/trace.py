"""Low-overhead trace events and timing without logging conversation content.

The event sink is synchronous so callers can persist a stage before proceeding.
Normal logs contain only safe identifiers and structured metadata. Private
audio, transcript, prompt, reply, TTS text, and credentials belong in the
separate debug archive and must not be placed in event attributes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import logging
import math
import re
import time
from typing import Callable, Literal, Mapping, Protocol
from uuid import uuid4


EventKind = Literal["started", "completed", "failed", "retry", "skipped"]
ClockNs = Callable[[], int]
UtcClock = Callable[[], datetime]

_SAFE_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
_SAFE_STRING = re.compile(r"[A-Za-z0-9_.:/@+-]{1,96}\Z")
_SENSITIVE_KEY_PARTS = (
    "audio", "pcm", "transcript", "utterance", "prompt", "reply", "text",
    "content", "body", "token", "secret", "auth", "credential", "password", "key",
    "environment", "env", "header", "request", "response", "payload",
)
_BEARER = re.compile(r"(?i)\bbearer\s+\S+")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|secret|password)\s*[:=]\s*\S+"
)
_TOKEN_VALUE = re.compile(
    r"(?i)^(?:sk[-_][A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{20,}|"
    r"xox[a-z]-[A-Za-z0-9-]{12,}|AIza[A-Za-z0-9_-]{20,})$"
)
_SUMMARY_STAGES = frozenset({
    "service_request", "service_response", "capture", "capture_frame_batch",
    "pcm_conversion", "wake_detection", "endpoint", "asr", "context_load",
    "knowledge_retrieval", "llm", "llm_request", "llm_response", "tts_request",
    "tts_enqueue", "playback",
})


class EventSink(Protocol):
    def __call__(self, event: "TraceEvent") -> None: ...


class EventRecorderError(RuntimeError):
    """A synchronous event sink failed and the event was not confirmed stored."""

    def __init__(self, event: "TraceEvent", cause: BaseException) -> None:
        super().__init__(
            f"could not persist trace event {event.event} for stage {event.stage!r}"
        )
        self.event = event
        self.original_cause = cause


@dataclass(frozen=True, slots=True)
class TraceContext:
    """Stable correlation identifiers shared by every stage in one turn."""

    trace_id: str
    turn_id: str
    session_id: str
    capture_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("trace_id", "turn_id", "session_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if self.capture_id is not None and (
            not isinstance(self.capture_id, str) or not self.capture_id.strip()
        ):
            raise ValueError("capture_id must be a non-empty string or None")

    @classmethod
    def create(
        cls,
        session_id: str,
        *,
        turn_id: str | None = None,
        trace_id: str | None = None,
        capture_id: str | None = None,
    ) -> "TraceContext":
        """Create IDs at the application boundary, or preserve supplied IDs."""
        return cls(
            trace_id=trace_id or uuid4().hex,
            turn_id=turn_id or uuid4().hex,
            session_id=session_id,
            capture_id=capture_id,
        )

    def with_capture(self, capture_id: str) -> "TraceContext":
        """Return the same turn context after capture has been created."""
        return TraceContext(self.trace_id, self.turn_id, self.session_id, capture_id)


@dataclass(frozen=True, slots=True)
class TraceEvent:
    """One structured event. ``elapsed_ms`` is set on terminal events."""

    event_id: str
    trace_id: str
    turn_id: str
    session_id: str
    capture_id: str | None
    stage: str
    event: EventKind
    timestamp_utc: str
    monotonic_ns: int
    elapsed_ms: float | None = None
    status: str | None = None
    attempt: int = 1
    error_code: str | None = None
    attributes: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.event not in {"started", "completed", "failed", "retry", "skipped"}:
            raise ValueError("unsupported event kind")
        if not self.stage or not self.stage.strip():
            raise ValueError("stage must be non-empty")
        if self.monotonic_ns < 0:
            raise ValueError("monotonic_ns must be non-negative")
        if self.elapsed_ms is not None and (
            not math.isfinite(self.elapsed_ms) or self.elapsed_ms < 0
        ):
            raise ValueError("elapsed_ms must be finite and non-negative")
        if self.attempt < 1:
            raise ValueError("attempt must be at least 1")

    def as_dict(self, *, redact: bool = True) -> dict[str, object]:
        value = asdict(self)
        if redact:
            value["attributes"] = sanitize_attributes(self.attributes)
        return value


@dataclass(frozen=True, slots=True)
class TraceSummary:
    """Compact per-turn timing summary; absent intervals remain ``None``."""

    trace_id: str
    total_elapsed_ms: float | None
    stage_elapsed_ms: Mapping[str, float | None]
    capture_to_wake_ms: float | None
    wake_to_speech_end_ms: float | None
    asr_ms: float | None
    llm_ms: float | None
    tts_enqueue_to_completed_ms: float | None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class TraceRecorder:
    """Emit trace events to a synchronous sink and a JSON standard logger.

    The sink is called first. If persistence fails, ``EventRecorderError`` is
    raised with both the failed event and original cause available. No event
    is silently dropped, and the caller's trace IDs are never changed.
    """

    def __init__(
        self,
        sink: EventSink | None = None,
        *,
        logger: logging.Logger | None = None,
        monotonic_ns: ClockNs = time.monotonic_ns,
        utc_clock: UtcClock | None = None,
    ) -> None:
        self.sink = sink
        self.logger = logger or logging.getLogger("astero.observability")
        self.monotonic_ns = monotonic_ns
        self.utc_clock = utc_clock or (lambda: datetime.now(timezone.utc))

    def stage(
        self,
        context: TraceContext,
        stage: str,
        *,
        attempt: int = 1,
        attributes: Mapping[str, object] | None = None,
    ) -> "StageTimer":
        return StageTimer(self, context, stage, attempt=attempt, attributes=attributes)

    def emit(
        self,
        context: TraceContext,
        stage: str,
        event: EventKind,
        *,
        monotonic_ns: int | None = None,
        elapsed_ms: float | None = None,
        status: str | None = None,
        attempt: int = 1,
        error_code: str | None = None,
        attributes: Mapping[str, object] | None = None,
    ) -> TraceEvent:
        """Create and synchronously emit one event for the supplied context."""
        now_ns = self.monotonic_ns() if monotonic_ns is None else monotonic_ns
        now_utc = self.utc_clock()
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)
        safe_attributes = sanitize_attributes(attributes or {})
        safe_status = _safe_label(status)
        safe_error = _safe_label(error_code)
        trace_event = TraceEvent(
            event_id=uuid4().hex,
            trace_id=context.trace_id,
            turn_id=context.turn_id,
            session_id=context.session_id,
            capture_id=context.capture_id,
            stage=_safe_stage(stage),
            event=event,
            timestamp_utc=now_utc.astimezone(timezone.utc).isoformat(),
            monotonic_ns=now_ns,
            elapsed_ms=elapsed_ms,
            status=safe_status,
            attempt=attempt,
            error_code=safe_error,
            attributes=safe_attributes,
        )
        sink_error: BaseException | None = None
        if self.sink is not None:
            try:
                self.sink(trace_event)
            except BaseException as exc:
                sink_error = exc
        self.logger.info("%s", event_to_json(trace_event))
        if sink_error is not None:
            raise EventRecorderError(trace_event, sink_error) from sink_error
        return trace_event


class StageTimer:
    """Context manager that emits paired started and terminal events."""

    def __init__(
        self,
        recorder: TraceRecorder,
        context: TraceContext,
        stage: str,
        *,
        attempt: int = 1,
        attributes: Mapping[str, object] | None = None,
    ) -> None:
        if attempt < 1:
            raise ValueError("attempt must be at least 1")
        self.recorder = recorder
        self.context = context
        self.stage = _safe_stage(stage)
        self.attempt = attempt
        self.attributes = sanitize_attributes(attributes or {})
        self.started_ns: int | None = None
        self.terminal_event: TraceEvent | None = None

    def __enter__(self) -> "StageTimer":
        if self.started_ns is not None:
            raise RuntimeError("a StageTimer cannot be entered more than once")
        self.started_ns = self.recorder.monotonic_ns()
        self.recorder.emit(
            self.context,
            self.stage,
            "started",
            monotonic_ns=self.started_ns,
            attempt=self.attempt,
            status="running",
            attributes=self.attributes,
        )
        return self

    def succeed(
        self,
        *,
        status: str = "completed",
        attributes: Mapping[str, object] | None = None,
    ) -> TraceEvent:
        return self._finish("completed", status=status, attributes=attributes)

    def fail(
        self,
        error_code: str,
        *,
        attributes: Mapping[str, object] | None = None,
    ) -> TraceEvent:
        return self._finish("failed", status="failed", error_code=error_code, attributes=attributes)

    def retry(
        self,
        error_code: str,
        *,
        attributes: Mapping[str, object] | None = None,
    ) -> TraceEvent:
        return self._finish("retry", status="retrying", error_code=error_code, attributes=attributes)

    def skip(
        self,
        reason_code: str = "not_applicable",
        *,
        attributes: Mapping[str, object] | None = None,
    ) -> TraceEvent:
        return self._finish("skipped", status="skipped", error_code=reason_code, attributes=attributes)

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if self.terminal_event is None:
            if exc is None:
                self.succeed()
            else:
                try:
                    self.fail(type(exc).__name__)
                except EventRecorderError as recorder_error:
                    exc.add_note(
                        f"trace event persistence also failed: {recorder_error}; "
                        f"original cause: {type(recorder_error.original_cause).__name__}"
                    )
        return False

    def _finish(
        self,
        event: Literal["completed", "failed", "retry", "skipped"],
        *,
        status: str,
        error_code: str | None = None,
        attributes: Mapping[str, object] | None = None,
    ) -> TraceEvent:
        if self.started_ns is None:
            raise RuntimeError("StageTimer must be entered before it can finish")
        if self.terminal_event is not None:
            raise RuntimeError("a StageTimer may have only one terminal event")
        finished_ns = self.recorder.monotonic_ns()
        elapsed_ms = max(0, finished_ns - self.started_ns) / 1_000_000
        combined = dict(self.attributes)
        combined.update(sanitize_attributes(attributes or {}))
        self.terminal_event = self.recorder.emit(
            self.context,
            self.stage,
            event,
            monotonic_ns=finished_ns,
            elapsed_ms=elapsed_ms,
            status=status,
            attempt=self.attempt,
            error_code=error_code,
            attributes=combined,
        )
        return self.terminal_event


class TraceSummaryBuilder:
    """Build a compact summary incrementally without retaining all events."""

    _TERMINAL = {"completed", "failed", "retry", "skipped"}

    def __init__(self, trace_id: str) -> None:
        if not trace_id:
            raise ValueError("trace_id must be non-empty")
        self.trace_id = trace_id
        self._first_ns: int | None = None
        self._last_ns: int | None = None
        self._starts: dict[tuple[str, int], int] = {}
        self._durations: dict[str, float] = {}
        self._marks: dict[str, int] = {}

    def observe(self, event: TraceEvent) -> None:
        if event.trace_id != self.trace_id:
            return
        self._first_ns = event.monotonic_ns if self._first_ns is None else min(
            self._first_ns, event.monotonic_ns
        )
        self._last_ns = event.monotonic_ns if self._last_ns is None else max(
            self._last_ns, event.monotonic_ns
        )
        if event.stage not in _SUMMARY_STAGES:
            return
        key = (event.stage, event.attempt)
        if event.event == "started":
            self._starts[key] = event.monotonic_ns
            self._marks[f"{event.stage}.start"] = event.monotonic_ns
        elif event.event in self._TERMINAL:
            start_ns = self._starts.pop(key, None)
            if start_ns is not None:
                self._durations[event.stage] = self._durations.get(event.stage, 0.0) + (
                    max(0, event.monotonic_ns - start_ns) / 1_000_000
                )
            if event.event == "completed":
                self._marks[event.stage] = event.monotonic_ns

    def build(self) -> TraceSummary:
        total = None
        request_start = self._marks.get("service_request.start")
        response_end = self._marks.get("service_response")
        if request_start is not None and response_end is not None:
            total = max(0, response_end - request_start) / 1_000_000
        elif self._first_ns is not None and self._last_ns is not None:
            total = max(0, self._last_ns - self._first_ns) / 1_000_000
        stage_names = {
            "capture", "capture_frame_batch", "wake_detection", "endpoint", "asr",
            "context_load", "knowledge_retrieval", "llm", "llm_request", "llm_response",
            "tts_enqueue", "playback",
        }
        stage_times = {name: self._durations.get(name) for name in sorted(stage_names)}
        capture = self._marks.get("capture.start")
        wake = self._marks.get("wake_detection")
        endpoint = self._marks.get("endpoint")
        playback = self._marks.get("playback")
        tts_enqueued = self._marks.get("tts_enqueue")
        return TraceSummary(
            trace_id=self.trace_id,
            total_elapsed_ms=total,
            stage_elapsed_ms=stage_times,
            capture_to_wake_ms=_delta_ms(capture, wake),
            wake_to_speech_end_ms=_delta_ms(wake, endpoint),
            asr_ms=self._durations.get("asr"),
            llm_ms=sum(self._durations.get(name, 0.0) for name in
                       ("llm", "llm_request", "llm_response")) or None,
            tts_enqueue_to_completed_ms=_delta_ms(tts_enqueued, playback),
        )


def sanitize_attributes(attributes: Mapping[str, object]) -> dict[str, object]:
    """Keep compact safe metadata and drop text-like or secret-like values.

    Attribute strings are limited to one token from a conservative character
    set. This rejects transcripts and natural-language prompt/reply content,
    even if a caller uses a non-obvious key. Keys associated with content or
    credentials are always dropped.
    """
    safe: dict[str, object] = {}
    for key, value in attributes.items():
        if not isinstance(key, str) or not _SAFE_KEY.fullmatch(key):
            continue
        normalized = key.lower().replace("-", "_")
        if any(part in normalized for part in _SENSITIVE_KEY_PARTS):
            continue
        if isinstance(value, bool) or value is None:
            safe[key] = value
        elif isinstance(value, int) and not isinstance(value, bool):
            safe[key] = value
        elif isinstance(value, float) and math.isfinite(value):
            safe[key] = value
        elif isinstance(value, str) and _SAFE_STRING.fullmatch(value):
            if (
                _BEARER.search(value)
                or _SECRET_ASSIGNMENT.search(value)
                or _TOKEN_VALUE.fullmatch(value)
            ):
                continue
            safe[key] = value
    return safe


def event_to_json(event: TraceEvent) -> str:
    """Serialize a trace event to one JSON line after metadata sanitization."""
    return json.dumps(event.as_dict(), ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _safe_stage(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("stage must be a non-empty string")
    token = value.strip()
    if not _SAFE_STRING.fullmatch(token):
        raise ValueError("stage must be a short identifier, not free text")
    return token


def _safe_label(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _SAFE_STRING.fullmatch(value):
        return "redacted"
    if (
        _BEARER.search(value)
        or _SECRET_ASSIGNMENT.search(value)
        or _TOKEN_VALUE.fullmatch(value)
    ):
        return "redacted"
    return value


def _delta_ms(start_ns: int | None, end_ns: int | None) -> float | None:
    if start_ns is None or end_ns is None:
        return None
    return max(0, end_ns - start_ns) / 1_000_000
