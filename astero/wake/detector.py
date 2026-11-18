"""Streaming wake-word detection with injectable scoring models.

The detector knows nothing about LLMs, speech capture, or output devices. A
model adapter scores fixed PCM16 chunks; thresholding, debounce, and bounded
pre-roll are handled here so offline tests do not need ML dependencies.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
from pathlib import Path
import struct
from typing import Protocol

from astero.audio.pcm import MonoFrame


class WakeModel(Protocol):
    """A scorer for one little-endian mono PCM16 chunk."""

    def score(self, pcm_s16le: bytes, sample_rate: int) -> float: ...


class ModelUnavailableError(RuntimeError):
    """Raised when the configured real model cannot be loaded."""


@dataclass(frozen=True, slots=True)
class DetectorConfig:
    sample_rate: int = 16_000
    chunk_ms: int = 80
    threshold: float = 0.5
    consecutive_frames: int = 2
    refractory_seconds: float = 1.5
    pre_roll_seconds: float = 0.5
    model_path: str | None = None

    def __post_init__(self) -> None:
        if self.sample_rate != 16_000:
            raise ValueError("wake detector currently requires 16000 Hz audio")
        if self.chunk_ms <= 0 or (self.sample_rate * self.chunk_ms) % 1000:
            raise ValueError("chunk_ms must produce a whole number of samples")
        if not 0.0 <= self.threshold <= 1.0:
            raise ValueError("threshold must be between 0 and 1")
        if self.consecutive_frames < 1:
            raise ValueError("consecutive_frames must be at least 1")
        if not math.isfinite(self.refractory_seconds) or self.refractory_seconds < 0:
            raise ValueError("refractory_seconds must be finite and non-negative")
        if not math.isfinite(self.pre_roll_seconds) or self.pre_roll_seconds < 0:
            raise ValueError("pre_roll_seconds must be finite and non-negative")

    @property
    def chunk_samples(self) -> int:
        return self.sample_rate * self.chunk_ms // 1000


@dataclass(frozen=True, slots=True)
class WakeEvent:
    """Detection edge and bounded preceding PCM, suitable for a command VAD."""

    timestamp: float
    sequence: int
    score: float
    sample_rate: int
    pre_roll_pcm_s16le: bytes
    pre_roll_samples: int


class OpenWakeWordModel:
    """Explicit custom-model adapter; no built-in keyword model is selected."""

    def __init__(self, model_path: str | Path) -> None:
        path = Path(model_path).expanduser()
        if not path.is_file():
            raise ModelUnavailableError(
                f"Astero wake model not found at {path}; prepare a licensed "
                "custom Astero model and set wake.model_path"
            )
        try:
            from openwakeword.model import Model
        except ImportError as exc:
            raise ModelUnavailableError(
                "openWakeWord is not installed; install the optional ARM64 "
                "runtime described in docs/WAKE_MODEL.md"
            ) from exc
        try:
            # Passing only the explicit path prevents accidental fallback to
            # Alexa/Jarvis/other bundled wake words.
            self._model = Model(wakeword_models=[str(path)], inference_framework="tflite")
        except Exception as exc:
            raise ModelUnavailableError(f"could not load custom wake model {path}: {exc}") from exc

    def score(self, pcm_s16le: bytes, sample_rate: int) -> float:
        if sample_rate != 16_000:
            raise ValueError("openWakeWord adapter requires 16000 Hz audio")
        try:
            import numpy as np
        except ImportError as exc:
            raise ModelUnavailableError("numpy is required by openWakeWord") from exc
        samples = np.frombuffer(pcm_s16le, dtype="<i2")
        predictions = self._model.predict(samples)
        if not predictions:
            return 0.0
        score = float(max(predictions.values()))
        if not math.isfinite(score):
            raise RuntimeError("wake model returned a non-finite score")
        return max(0.0, min(1.0, score))


@dataclass(frozen=True, slots=True)
class _AudioChunk:
    pcm: bytes
    timestamp: float
    sequence: int


class WakeDetector:
    """Turn timestamped mono PCM16 frames into debounced wake events."""

    def __init__(self, model: WakeModel, config: DetectorConfig = DetectorConfig()) -> None:
        self.model = model
        self.config = config
        self._pending = bytearray()
        self._pending_start: float | None = None
        self._pre_roll: deque[_AudioChunk] = deque()
        self._pre_roll_samples = 0
        self._above_count = 0
        self._last_event_time: float | None = None
        self._last_timestamp: float | None = None
        self._last_sequence: int | None = None

    def feed(self, frame: MonoFrame) -> tuple[WakeEvent, ...]:
        """Consume one frame and return zero or more detection events."""
        self._validate_frame(frame)
        if self._last_timestamp is not None and frame.timestamp < self._last_timestamp:
            raise ValueError("frame timestamps must be monotonic")
        if self._last_sequence is not None and frame.sequence <= self._last_sequence:
            raise ValueError("frame sequence must increase")
        self._last_timestamp = frame.timestamp
        self._last_sequence = frame.sequence

        if self._pending_start is None:
            self._pending_start = frame.timestamp
        self._pending.extend(frame.data)
        chunk_bytes = self.config.chunk_samples * 2
        events: list[WakeEvent] = []
        while len(self._pending) >= chunk_bytes:
            pcm = bytes(self._pending[:chunk_bytes])
            del self._pending[:chunk_bytes]
            chunk_timestamp = self._pending_start
            duration = self.config.chunk_samples / self.config.sample_rate
            self._pending_start = chunk_timestamp + duration
            if self.config.pre_roll_seconds > 0:
                self._append_pre_roll(
                    _AudioChunk(pcm, chunk_timestamp, frame.sequence), self.config.chunk_samples
                )
            score = float(self.model.score(pcm, self.config.sample_rate))
            if not math.isfinite(score) or not 0 <= score <= 1:
                raise ValueError("wake model score must be finite and between 0 and 1")
            if score >= self.config.threshold:
                self._above_count += 1
            else:
                self._above_count = 0
            if self._above_count < self.config.consecutive_frames:
                continue
            detected_at = chunk_timestamp + duration
            if self._last_event_time is not None and detected_at - self._last_event_time < self.config.refractory_seconds:
                continue
            preroll = b"".join(item.pcm for item in self._pre_roll)
            events.append(
                WakeEvent(
                    timestamp=detected_at,
                    # The completed chunk may span capture frames; use the
                    # sequence of the frame that completed it.
                    sequence=frame.sequence,
                    score=score,
                    sample_rate=self.config.sample_rate,
                    pre_roll_pcm_s16le=preroll,
                    pre_roll_samples=len(preroll) // 2,
                )
            )
            self._last_event_time = detected_at
        return tuple(events)

    def reset(self) -> None:
        """Forget stream state and buffered audio between capture sessions."""
        self._pending.clear()
        self._pending_start = None
        self._pre_roll.clear()
        self._pre_roll_samples = 0
        self._above_count = 0
        self._last_event_time = None
        self._last_timestamp = None
        self._last_sequence = None

    def _validate_frame(self, frame: MonoFrame) -> None:
        if frame.sample_rate != self.config.sample_rate:
            raise ValueError(f"frame sample_rate must be {self.config.sample_rate}")
        if frame.sample_format != "pcm_s16le":
            raise ValueError("wake model input must be mono pcm_s16le")
        if not math.isfinite(frame.timestamp):
            raise ValueError("frame timestamp must be finite")
        if frame.sequence < 0 or frame.sample_count < 0:
            raise ValueError("frame sequence and sample_count must be non-negative")
        if len(frame.data) != frame.sample_count * 2:
            raise ValueError("frame data length does not match sample_count")

    def _append_pre_roll(self, chunk: _AudioChunk, samples: int) -> None:
        limit = round(self.config.pre_roll_seconds * self.config.sample_rate)
        if limit <= 0:
            return
        if samples > limit:
            chunk = _AudioChunk(chunk.pcm[-limit * 2 :], chunk.timestamp, chunk.sequence)
            samples = limit
        self._pre_roll.append(chunk)
        self._pre_roll_samples += samples
        while self._pre_roll and self._pre_roll_samples > limit:
            excess = self._pre_roll_samples - limit
            oldest = self._pre_roll[0]
            oldest_samples = len(oldest.pcm) // 2
            if oldest_samples <= excess:
                self._pre_roll.popleft()
                self._pre_roll_samples -= oldest_samples
            else:
                self._pre_roll[0] = _AudioChunk(
                    oldest.pcm[excess * 2 :],
                    oldest.timestamp + excess / self.config.sample_rate,
                    oldest.sequence,
                )
                self._pre_roll_samples -= excess


def pcm16_frame(
    data: bytes,
    *,
    timestamp: float,
    sequence: int,
    sample_rate: int = 16_000,
) -> MonoFrame:
    """Convenience constructor for tests and adapters receiving PCM16 bytes."""
    if len(data) % 2:
        raise ValueError("PCM16 data must contain complete 16-bit samples")
    return MonoFrame(timestamp, sequence, sample_rate, "pcm_s16le", len(data) // 2, data)


def silence(samples: int) -> bytes:
    """Return silent PCM16 bytes for offline stream fixtures."""
    if samples < 0:
        raise ValueError("samples must be non-negative")
    return struct.pack("<h", 0) * samples
