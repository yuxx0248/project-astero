"""Offline speech recognition adapters.

Audio and transcripts remain in memory.  The default adapter is a thin
wrapper around sherpa-onnx's streaming Zipformer transducer.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
import importlib
import math
from pathlib import Path
import struct
import threading
import time
from typing import Protocol

from astero.audio.pcm import MonoFrame


class ASRError(RuntimeError):
    """Recognition could not be completed."""


class ASRUnavailableError(ASRError):
    """Runtime or model files are unavailable."""


class ASRTimeoutError(ASRError, TimeoutError):
    """A recognition call exceeded its configured deadline."""


@dataclass(frozen=True, slots=True)
class ASRResult:
    text: str
    language: str | None
    elapsed_seconds: float
    audio_duration_seconds: float


class Recognizer(Protocol):
    def recognize(self, samples: tuple[float, ...], sample_rate: int) -> str: ...


@dataclass(frozen=True, slots=True)
class ZipformerConfig:
    encoder: Path
    decoder: Path
    joiner: Path
    tokens: Path
    sample_rate: int = 16_000
    num_threads: int = 2
    provider: str = "cpu"
    timeout_seconds: float = 20.0

    def __post_init__(self) -> None:
        if self.sample_rate != 16_000:
            raise ValueError("ASR input sample_rate must be 16000")
        if self.num_threads < 1:
            raise ValueError("num_threads must be positive")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        if self.provider != "cpu":
            raise ValueError("this adapter currently supports provider='cpu' only")


class _SherpaZipformer:
    """Concrete sherpa-onnx streaming transducer implementation."""

    def __init__(self, config: ZipformerConfig) -> None:
        paths = (config.encoder, config.decoder, config.joiner, config.tokens)
        missing = [str(path) for path in paths if not path.is_file()]
        if missing:
            raise ASRUnavailableError("ASR model files missing: " + ", ".join(missing))
        try:
            sherpa = importlib.import_module("sherpa_onnx")
        except ImportError as exc:
            raise ASRUnavailableError(
                "sherpa-onnx is not installed; install it in the active Python environment"
            ) from exc
        try:
            self._recognizer = sherpa.OnlineRecognizer.from_transducer(
                tokens=str(config.tokens),
                encoder=str(config.encoder),
                decoder=str(config.decoder),
                joiner=str(config.joiner),
                num_threads=config.num_threads,
                sample_rate=config.sample_rate,
                feature_dim=80,
                decoding_method="greedy_search",
                provider=config.provider,
            )
        except Exception as exc:
            raise ASRUnavailableError(f"could not load sherpa-onnx model: {exc}") from exc

    def recognize(self, samples: tuple[float, ...], sample_rate: int) -> str:
        stream = self._recognizer.create_stream()
        stream.accept_waveform(sample_rate, samples)
        stream.input_finished()
        while self._recognizer.is_ready(stream):
            self._recognizer.decode_stream(stream)
        result = self._recognizer.get_result(stream)
        return result if isinstance(result, str) else result.text


class SherpaOnnxASR:
    """Validate mono PCM and run local sherpa-onnx recognition.

    Language is reported as ``None``: the selected bilingual Zipformer does
    not expose language identification, so no guess is returned.  Exact digital
    silence short-circuits to an empty result.  No audio or transcript is
    written to disk or logged.
    """

    def __init__(self, config: ZipformerConfig, *, recognizer: Recognizer | None = None) -> None:
        self.config = config
        self._recognizer = recognizer or _SherpaZipformer(config)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="astero-asr")
        self._lock = threading.Lock()
        self._pending: Future[str] | None = None
        self._closed = False

    def recognize(self, frame: MonoFrame, *, timeout_seconds: float | None = None) -> ASRResult:
        samples = _to_float_samples(frame, self.config.sample_rate)
        duration = len(samples) / self.config.sample_rate
        started = time.perf_counter()
        if all(sample == 0.0 for sample in samples):
            return ASRResult("", None, time.perf_counter() - started, duration)

        deadline = self.config.timeout_seconds if timeout_seconds is None else timeout_seconds
        if not math.isfinite(deadline) or deadline <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        with self._lock:
            if self._closed:
                raise ASRError("ASR adapter is closed")
            if self._pending is not None and not self._pending.done():
                raise ASRTimeoutError("previous ASR call is still running after timeout")
            future = self._executor.submit(self._recognizer.recognize, samples, self.config.sample_rate)
            self._pending = future
        try:
            text = future.result(timeout=deadline)
        except FutureTimeout as exc:
            raise ASRTimeoutError(f"ASR exceeded {deadline:g} seconds") from exc
        except Exception as exc:
            raise ASRError(f"ASR recognition failed: {exc}") from exc
        elapsed = time.perf_counter() - started
        return ASRResult(text=text, language=None, elapsed_seconds=elapsed,
                         audio_duration_seconds=duration)

    def close(self) -> None:
        with self._lock:
            self._closed = True
        # Python cannot cancel a native inference already executing.  Do not
        # block this close call; a timed-out worker may finish naturally.
        self._executor.shutdown(wait=False, cancel_futures=True)

    def __enter__(self) -> "SherpaOnnxASR":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _to_float_samples(frame: MonoFrame, sample_rate: int) -> tuple[float, ...]:
    if frame.sample_rate != sample_rate:
        raise ValueError(f"sample_rate must be {sample_rate}")
    if frame.sample_format not in ("pcm_s16le", "float32_le"):
        raise ValueError("sample_format must be pcm_s16le or float32_le")
    if not math.isfinite(frame.timestamp) or frame.sequence < 0:
        raise ValueError("frame timestamp must be finite and sequence non-negative")
    if frame.sample_count <= 0:
        raise ValueError("audio must contain at least one sample")
    width, fmt = (2, "h") if frame.sample_format == "pcm_s16le" else (4, "f")
    if len(frame.data) != frame.sample_count * width:
        raise ValueError("data length does not match sample_count")
    if fmt == "h":
        return tuple(value / 32768.0 for (value,) in struct.iter_unpack("<h", frame.data))
    values = tuple(value for (value,) in struct.iter_unpack("<f", frame.data))
    if any(not math.isfinite(value) or abs(value) > 1.0 for value in values):
        raise ValueError("float32 samples must be finite and within [-1, 1]")
    return values
