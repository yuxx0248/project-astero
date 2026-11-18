"""Wake-triggered speech collection with a bounded pre-roll buffer.

The endpointer deliberately does not implement voice detection itself. Inject a
model-backed VAD (for example, a Silero VAD ONNX adapter) so energy thresholds
are never mistaken for speech recognition.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum
import math
from typing import Protocol

from astero.audio.pcm import MonoFrame


@dataclass(frozen=True, slots=True)
class WakeEvent:
    """A wake detector event. timestamp uses the audio clock in seconds."""

    timestamp: float
    score: float | None = None


class VAD(Protocol):
    """Model adapter boundary; return whether a complete frame contains speech."""

    def is_speech(self, frame: MonoFrame) -> bool: ...


@dataclass(frozen=True, slots=True)
class EndpointConfig:
    sample_rate: int = 16_000
    pre_roll_seconds: float = 0.5
    pre_roll_buffer_seconds: float = 0.75
    start_timeout_seconds: float = 5.0
    end_silence_seconds: float = 1.5
    max_duration_seconds: float = 90.0

    def __post_init__(self) -> None:
        if self.sample_rate != 16_000:
            raise ValueError("endpoint input is fixed at 16 kHz")
        for name in (
            "pre_roll_seconds",
            "pre_roll_buffer_seconds",
            "start_timeout_seconds",
            "end_silence_seconds",
            "max_duration_seconds",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite non-negative number")
        if self.pre_roll_buffer_seconds < self.pre_roll_seconds:
            raise ValueError("pre_roll_buffer_seconds must cover pre_roll_seconds")
        if self.start_timeout_seconds <= 0 or self.end_silence_seconds <= 0:
            raise ValueError("start timeout and end silence must be positive")
        if self.max_duration_seconds <= 0:
            raise ValueError("max_duration_seconds must be positive")


@dataclass(frozen=True, slots=True)
class SpeechSegment:
    """Collected mono PCM with its source timing and encoding."""

    sample_rate: int
    sample_format: str
    start_timestamp: float
    end_timestamp: float
    first_sequence: int
    last_sequence: int
    data: bytes


class EndpointKind(str, Enum):
    WAKE = "wake"
    SPEECH_STARTED = "speech_started"
    ENDPOINT = "endpoint"
    NO_SPEECH_TIMEOUT = "no_speech_timeout"
    MAX_DURATION = "max_duration"
    CANCELLED = "cancelled"
    STREAM_LOST = "stream_lost"


@dataclass(frozen=True, slots=True)
class EndpointEvent:
    kind: EndpointKind
    timestamp: float
    segment: SpeechSegment | None = None
    wake_score: float | None = None


class EndpointCollector:
    """Collect one command after a wake event.

    Call feed continuously, including while idle, to maintain pre-roll. Audio
    is retained only in bounded in-memory buffers; terminal events clear those
    buffers after attaching a completed segment where appropriate.
    """

    def __init__(self, vad: VAD, config: EndpointConfig | None = None) -> None:
        self.vad = vad
        self.config = config or EndpointConfig()
        self._preroll: deque[MonoFrame] = deque()
        self._preroll_duration = 0.0
        self._active: list[MonoFrame] = []
        self._wake: WakeEvent | None = None
        self._speech_started_at: float | None = None
        self._last_speech_end: float | None = None
        self._last_timestamp: float | None = None
        self._format: str | None = None

    @property
    def active(self) -> bool:
        return self._wake is not None

    def feed(self, frame: MonoFrame) -> tuple[EndpointEvent, ...]:
        """Accept one audio frame and return zero or more state events."""
        self._validate_frame(frame)
        duration = frame.sample_count / frame.sample_rate
        if duration <= 0:
            raise ValueError("audio frames must contain at least one sample")
        if self._last_timestamp is not None and frame.timestamp < self._last_timestamp:
            raise ValueError("audio timestamps must be non-decreasing")
        self._last_timestamp = frame.timestamp

        if not self.active:
            self._remember_preroll(frame, duration)
            return ()

        self._active.append(frame)
        now = frame.timestamp + duration
        elapsed = now - self._wake.timestamp  # type: ignore[union-attr]

        events: list[EndpointEvent] = []
        speech = self.vad.is_speech(frame)
        if self._speech_started_at is None:
            if speech:
                self._speech_started_at = frame.timestamp
                self._last_speech_end = now
                events.append(EndpointEvent(
                    EndpointKind.SPEECH_STARTED, frame.timestamp,
                    wake_score=self._wake.score,  # type: ignore[union-attr]
                ))
            elif elapsed >= self.config.start_timeout_seconds:
                return (self._finish(EndpointKind.NO_SPEECH_TIMEOUT, now),)
        elif speech:
            self._last_speech_end = now
        elif self._last_speech_end is not None and now - self._last_speech_end >= self.config.end_silence_seconds:
            return (self._finish(EndpointKind.ENDPOINT, now),)

        if elapsed >= self.config.max_duration_seconds:
            events.append(self._finish(EndpointKind.MAX_DURATION, now))
        return tuple(events)

    def wake(self, event: WakeEvent) -> tuple[EndpointEvent, ...]:
        """Start collection and seed it from the recent bounded pre-roll."""
        if not math.isfinite(event.timestamp):
            raise ValueError("wake timestamp must be finite")
        if event.score is not None and not math.isfinite(event.score):
            raise ValueError("wake score must be finite when present")
        if self.active:
            raise RuntimeError("a command is already being collected")
        cutoff = event.timestamp - self.config.pre_roll_seconds
        self._active = [
            frame for frame in self._preroll
            if frame.timestamp + frame.sample_count / frame.sample_rate > cutoff
            and frame.timestamp <= event.timestamp
        ]
        # Frame-granular pre-roll can over-capture by at most one VAD frame.
        self._wake = event
        self._speech_started_at = None
        self._last_speech_end = None
        self._format = self._active[-1].sample_format if self._active else None
        self._preroll.clear()
        self._preroll_duration = 0.0
        events = [EndpointEvent(EndpointKind.WAKE, event.timestamp, wake_score=event.score)]
        # The wake callback can arrive after the first command syllable. Run
        # those retained frames through VAD once so pre-roll can preserve it.
        for frame in self._active:
            end = frame.timestamp + frame.sample_count / frame.sample_rate
            if self.vad.is_speech(frame):
                if self._speech_started_at is None:
                    self._speech_started_at = frame.timestamp
                    events.append(EndpointEvent(
                        EndpointKind.SPEECH_STARTED, frame.timestamp, wake_score=event.score
                    ))
                self._last_speech_end = end
        return tuple(events)

    def cancel(self, timestamp: float | None = None) -> EndpointEvent:
        """Discard the current utterance and report cancellation."""
        if not self.active:
            raise RuntimeError("no command is being collected")
        at = self._timestamp(timestamp)
        self._clear_active()
        self._preroll.clear()
        self._preroll_duration = 0.0
        return EndpointEvent(EndpointKind.CANCELLED, at)

    def stream_lost(self, timestamp: float | None = None) -> EndpointEvent:
        """Report a source disconnect, discarding any incomplete utterance."""
        at = self._timestamp(timestamp)
        self._clear_active()
        self._preroll.clear()
        self._preroll_duration = 0.0
        return EndpointEvent(EndpointKind.STREAM_LOST, at)

    def _validate_frame(self, frame: MonoFrame) -> None:
        if frame.sample_rate != self.config.sample_rate:
            raise ValueError(f"sample_rate must be {self.config.sample_rate}")
        if frame.sample_format not in ("pcm_s16le", "float32_le"):
            raise ValueError("sample_format must be pcm_s16le or float32_le")
        width = 2 if frame.sample_format == "pcm_s16le" else 4
        if frame.sample_count < 0 or len(frame.data) != frame.sample_count * width:
            raise ValueError("data length does not match mono frame sample_count")
        if not math.isfinite(frame.timestamp) or frame.sequence < 0:
            raise ValueError("frame timestamp must be finite and sequence non-negative")
        if self._format is not None and self._format != frame.sample_format:
            raise ValueError("sample format cannot change within a command")
        if self.active and self._format is None:
            self._format = frame.sample_format

    def _remember_preroll(self, frame: MonoFrame, duration: float) -> None:
        if self._preroll and self._preroll[0].sample_format != frame.sample_format:
            raise ValueError("sample format cannot change within the pre-roll buffer")
        self._preroll.append(frame)
        self._preroll_duration += duration
        while self._preroll and self._preroll_duration > self.config.pre_roll_buffer_seconds:
            old = self._preroll.popleft()
            self._preroll_duration -= old.sample_count / old.sample_rate

    def _finish(self, kind: EndpointKind, timestamp: float) -> EndpointEvent:
        segment = None
        if self._speech_started_at is not None and self._active:
            frames = self._active
            segment = SpeechSegment(
                sample_rate=frames[0].sample_rate,
                sample_format=frames[0].sample_format,
                start_timestamp=frames[0].timestamp,
                end_timestamp=timestamp,
                first_sequence=frames[0].sequence,
                last_sequence=frames[-1].sequence,
                data=b"".join(frame.data for frame in frames),
            )
        score = self._wake.score if self._wake else None
        self._clear_active()
        self._preroll.clear()
        self._preroll_duration = 0.0
        return EndpointEvent(kind, timestamp, segment=segment, wake_score=score)

    def _clear_active(self) -> None:
        self._active.clear()
        self._wake = None
        self._speech_started_at = None
        self._last_speech_end = None
        self._format = None

    def _timestamp(self, value: float | None) -> float:
        if value is None:
            if self._last_timestamp is None:
                raise ValueError("timestamp is required before any audio frame")
            return self._last_timestamp
        if not math.isfinite(value):
            raise ValueError("timestamp must be finite")
        return value
