"""Long-running local application service for Astero turns."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, is_dataclass
import threading
import time
from typing import Callable, Mapping, Protocol
import uuid


class ServiceError(RuntimeError):
    """Invalid service request or unavailable service state."""


class TurnStore(Protocol):
    def create_session(self, session_id: str, metadata: Mapping[str, object] | None = None) -> object: ...
    def create_turn(self, turn_id: str, trace_id: str, session_id: str,
                    status: str = "queued") -> object: ...
    def update_turn(self, turn_id: str, status: str, *, error_code: str | None = None,
                    result: Mapping[str, object] | None = None) -> object: ...
    def get_turn(self, turn_id: str) -> object | None: ...
    def list_turns(self, session_id: str, limit: int = 100) -> object: ...
    def list_stage_events(self, turn_id: str, limit: int | None = None) -> object: ...


class TraceRecorderProtocol(Protocol):
    def emit(self, context: object, stage: str, event: str, *,
             monotonic_ns: int | None = None, elapsed_ms: float | None = None,
             status: str | None = None, attempt: int = 1,
             error_code: str | None = None, attributes: Mapping[str, object] | None = None): ...


@dataclass(frozen=True, slots=True)
class TurnRequest:
    turn_id: str
    trace_id: str
    session_id: str
    mode: str
    text: str | None
    diagnostic_only: bool
    submitted_monotonic_ns: int
    context: object | None = None


class AsteroService:
    """Accept durable turn requests and run injected voice/text executors.

    ``text_executor`` is intended for explicit diagnostics. It must not be
    presented as voice/e2e acceptance. ``voice_executor`` owns capture and the
    full wake-to-playback path and is supplied by the runtime composition.
    """

    def __init__(self, store: TurnStore, *,
                 text_executor: Callable[[TurnRequest], object] | None = None,
                 voice_executor: Callable[[TurnRequest], object] | None = None,
                 readiness: Callable[[], Mapping[str, object]] | None = None,
                 trace_recorder: TraceRecorderProtocol | None = None,
                 workers: int = 2, monotonic_ns: Callable[[], int] = time.monotonic_ns) -> None:
        if workers < 1:
            raise ValueError("workers must be positive")
        self.store = store
        self.text_executor = text_executor
        self.voice_executor = voice_executor
        self.readiness = readiness or (lambda: {"ready": True, "dependencies": {}})
        self.trace_recorder = trace_recorder
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="astero-turn")
        self._monotonic_ns = monotonic_ns
        self._futures: dict[str, Future] = {}
        self._lock = threading.RLock()
        self._closed = False

    def health(self) -> dict[str, object]:
        details = dict(self.readiness())
        ready = details.get("ready") is True
        return {"status": "ok" if ready else "degraded", **details}

    def submit_turn(self, *, session_id: str, mode: str = "voice",
                    text: str | None = None, turn_id: str | None = None,
                    trace_id: str | None = None) -> dict[str, object]:
        if self._closed:
            raise ServiceError("service is shutting down")
        session_id = _required_text(session_id, "session_id")
        if mode not in {"voice", "text"}:
            raise ValueError("mode must be 'voice' or 'text'")
        if mode == "text":
            text = _required_text(text, "text")
        elif text is not None:
            raise ValueError("voice mode does not accept injected text")
        executor = self.voice_executor if mode == "voice" else self.text_executor
        if executor is None:
            raise ServiceError(f"{mode} mode is not configured")
        turn_id = _required_text(turn_id or uuid.uuid4().hex, "turn_id")
        trace_id = _required_text(trace_id or uuid.uuid4().hex, "trace_id")
        context = None
        if self.trace_recorder is not None:
            from astero.observability import TraceContext
            context = TraceContext.create(session_id, turn_id=turn_id, trace_id=trace_id)
        request = TurnRequest(turn_id, trace_id, session_id, mode, text,
                              mode == "text", self._monotonic_ns(), context)
        self.store.create_session(session_id, metadata={})
        self.store.create_turn(turn_id, trace_id, session_id, status="queued")
        if context is not None:
            try:
                self.trace_recorder.emit(context, "service_request", "started",
                                         monotonic_ns=request.submitted_monotonic_ns,
                                         status="accepted", attributes={"mode": mode})
            except Exception as exc:
                self.store.update_turn(turn_id, "failed", error_code=_error_code(exc))
                raise ServiceError("could not record service request trace") from exc
        with self._lock:
            if self._closed:
                self.store.update_turn(turn_id, "failed", error_code="service_shutting_down")
                raise ServiceError("service is shutting down")
            future = self._executor.submit(self._execute, request, executor)
            self._futures[turn_id] = future
            future.add_done_callback(lambda _: self._forget_future(turn_id))
        return {"turn_id": turn_id, "trace_id": trace_id, "session_id": session_id,
                "status": "queued", "diagnostic_only": request.diagnostic_only}

    def get_turn(self, turn_id: str) -> dict[str, object]:
        turn_id = _required_text(turn_id, "turn_id")
        row = self.store.get_turn(turn_id)
        if row is None:
            raise KeyError(turn_id)
        result = _record(row)
        terminal = row.status in {"completed", "failed", "cancelled", "no_wake",
                                  "no_speech", "empty_input", "interrupted"}
        all_events = self.store.list_stage_events(turn_id) if terminal else None
        events = all_events if all_events is not None else self.store.list_stage_events(turn_id, limit=20)
        result["events"] = [_record(event) for event in events[-80:]]
        result["events_truncated"] = len(events) > 80 if all_events is not None else None
        try:
            if not terminal:
                result["trace_summary"] = None
                return result
            from astero.observability import TraceEvent, TraceSummaryBuilder
            builder = TraceSummaryBuilder(row.trace_id)
            for event in (all_events or events):
                if event.monotonic_ns is None:
                    continue
                builder.observe(TraceEvent(
                    event_id=event.event_id, trace_id=event.trace_id,
                    turn_id=event.turn_id, session_id=event.session_id,
                    capture_id=None, stage=event.stage, event=event.event,
                    timestamp_utc=event.occurred_at_utc,
                    monotonic_ns=event.monotonic_ns, elapsed_ms=event.elapsed_ms,
                    status=event.status, attempt=event.attempt,
                    error_code=event.attributes.get("error_code"),
                    attributes=event.attributes,
                ))
            result["trace_summary"] = builder.build().as_dict()
        except (AttributeError, TypeError, ValueError):
            result["trace_summary"] = None
        return result

    def list_turns(self, session_id: str, limit: int = 100) -> list[dict[str, object]]:
        session_id = _required_text(session_id, "session_id")
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        return [_record(row) for row in self.store.list_turns(session_id, limit=limit)]

    def close(self, *, wait: bool = True) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            pending = tuple(self._futures.items())
        for turn_id, future in pending:
            if future.cancel():
                try:
                    self.store.update_turn(turn_id, "cancelled", error_code="service_shutdown")
                except Exception:
                    pass
        self._executor.shutdown(wait=wait, cancel_futures=True)

    def _execute(self, request: TurnRequest, executor: Callable[[TurnRequest], object]) -> None:
        try:
            execution_started_ns = self._monotonic_ns()
            if request.context is not None and self.trace_recorder is not None:
                self.trace_recorder.emit(
                    request.context, "service_queue", "completed",
                    monotonic_ns=execution_started_ns,
                    elapsed_ms=max(0.0, (execution_started_ns - request.submitted_monotonic_ns) / 1_000_000),
                    status="running", attributes={"mode": request.mode},
                )
            self.store.update_turn(request.turn_id, "running")
            result = executor(request)
            status = _result_status(result)
            mapping = _record(result)
            mapping.setdefault("diagnostic_only", request.diagnostic_only)
            mapping.setdefault("mode", request.mode)
            mapping.setdefault("trace_id", request.trace_id)
            mapping.setdefault("session_id", request.session_id)
            if not mapping.get("error_code") and mapping.get("error"):
                mapping["error_code"] = _error_code(mapping["error"])
            if status not in {"completed", "failed", "cancelled", "no_wake", "no_speech",
                              "empty_input", "interrupted"}:
                raise ServiceError(f"executor returned invalid terminal status {status!r}")
            self.store.update_turn(request.turn_id, status,
                                   error_code=mapping.get("error_code"), result=mapping)
            if request.context is not None and self.trace_recorder is not None:
                finished_ns = self._monotonic_ns()
                self.trace_recorder.emit(
                    request.context, "service_request", "completed",
                    monotonic_ns=finished_ns,
                    elapsed_ms=max(0.0, (finished_ns - request.submitted_monotonic_ns) / 1_000_000),
                    status=status, attributes={"mode": request.mode},
                )
        except Exception as exc:
            # Do not put exception messages into public status; provider errors
            # may contain user content or service response bodies.
            code = _error_code(exc)
            try:
                self.store.update_turn(request.turn_id, "failed", error_code=code,
                                       result={"trace_id": request.trace_id,
                                               "session_id": request.session_id,
                                               "mode": request.mode})
            except Exception:
                pass
            if request.context is not None and self.trace_recorder is not None:
                finished_ns = self._monotonic_ns()
                try:
                    self.trace_recorder.emit(
                        request.context, "service_request", "failed",
                        monotonic_ns=finished_ns,
                        elapsed_ms=max(0.0, (finished_ns - request.submitted_monotonic_ns) / 1_000_000),
                        status="failed", error_code=code, attributes={"mode": request.mode},
                    )
                except Exception:
                    pass

    def _forget_future(self, turn_id: str) -> None:
        with self._lock:
            self._futures.pop(turn_id, None)


def _record(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    return {"value": value}


def _result_status(value: object) -> str:
    result = _record(value)
    status = result.get("status")
    if hasattr(status, "value"):
        status = status.value
    if not isinstance(status, str):
        raise ServiceError("executor result is missing a status")
    return status


def _required_text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _error_code(exc: Exception) -> str:
    name = type(exc).__name__
    return "".join(("_" + char.lower()) if char.isupper() else char for char in name).lstrip("_")
