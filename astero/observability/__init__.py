"""Trace context, timing, and structured logging for Project Astero."""

from .trace import (
    EventRecorderError,
    StageTimer,
    TraceContext,
    TraceEvent,
    TraceRecorder,
    TraceSummary,
    TraceSummaryBuilder,
    event_to_json,
)

__all__ = [
    "EventRecorderError",
    "StageTimer",
    "TraceContext",
    "TraceEvent",
    "TraceRecorder",
    "TraceSummary",
    "TraceSummaryBuilder",
    "event_to_json",
]
