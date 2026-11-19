"""Bounded overlapping keyword streams for long-running Echo listening.

Two staggered streams bound decoder history without splitting a word at reset.
Scores, thresholds and existing ASR aliases are unchanged. The larger ASR
fallback remains a single stream, avoiding doubled ASR inference cost.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import re
from astero.audio.pcm import MonoFrame


@dataclass(frozen=True, slots=True)
class WakeFrame(MonoFrame):
    """Primary mono processing audio plus an optional synchronous wake channel."""

    alternate_data: bytes | None = None

    def __post_init__(self):
        if self.sample_format != "pcm_s16le" or self.sample_rate != 16000:
            raise ValueError("wake input must be 16 kHz PCM16")
        if len(self.data) != self.sample_count * 2:
            raise ValueError("primary wake frame length mismatch")
        if self.alternate_data is not None and len(self.alternate_data) != len(self.data):
            raise ValueError("alternate wake frame must have the same sample count")


def with_alternate(primary: MonoFrame, alternate: MonoFrame) -> WakeFrame:
    """Attach one aligned microphone channel without changing primary audio."""
    if any(getattr(primary, name) != getattr(alternate, name) for name in
           ("timestamp", "sequence", "sample_rate", "sample_format", "sample_count")):
        raise ValueError("wake channels must originate from the same capture frame")
    return WakeFrame(primary.timestamp, primary.sequence, primary.sample_rate,
                     primary.sample_format, primary.sample_count, primary.data,
                     alternate.data)


class _OverlappingKeywordStreams:
    def __init__(self, spotter, *, sample_rate=16000, window_seconds=8, hop_seconds=4):
        if not 0 < hop_seconds < window_seconds:
            raise ValueError("keyword hop must be positive and shorter than window")
        self.spotter = spotter
        self.sample_rate = sample_rate
        self.window_samples = int(sample_rate * window_seconds)
        self.hop_samples = int(sample_rate * hop_seconds)
        self.reset()

    def reset(self):
        self.samples = 0
        self.streams = [(0, self.spotter.create_stream())]

    def feed(self, samples):
        if self.samples - self.streams[-1][0] >= self.hop_samples:
            self.streams.append((self.samples, self.spotter.create_stream()))
        self.streams = [(born, stream) for born, stream in self.streams
                        if self.samples - born < self.window_samples]
        detected = False
        for _, stream in self.streams:
            stream.accept_waveform(self.sample_rate, samples)
            while self.spotter.is_ready(stream):
                self.spotter.decode_stream(stream)
                if self.spotter.get_result(stream):
                    detected = True
        self.samples += len(samples)
        if detected:
            # Do not report the same word again from the overlapping stream.
            self.reset()
        return detected


@dataclass(frozen=True)
class Detected:
    timestamp: float
    score: float | None = None


class SherpaKeyword:
    """Open-vocabulary phoneme keyword spotter, configured only for Astero."""
    def __init__(self, model_dir: Path, keywords: Path):
        import sherpa_onnx as s
        stem = 'epoch-13-avg-2-chunk-8-left-64'
        self.spotter = s.KeywordSpotter(
            tokens=str(model_dir / 'tokens.txt'),
            encoder=str(model_dir / f'encoder-{stem}.int8.onnx'),
            decoder=str(model_dir / f'decoder-{stem}.onnx'),
            joiner=str(model_dir / f'joiner-{stem}.int8.onnx'),
            keywords_file=str(keywords), num_threads=2,
            keywords_threshold=0.05, keywords_score=5.0,
        )
        self.keyword_streams = _OverlappingKeywordStreams(self.spotter)
        self.secondary_keyword_streams = _OverlappingKeywordStreams(self.spotter)
        self._alternate_active = False
        asr_dir = Path(model_dir).parent / 'asr'
        self.recognizer = s.OnlineRecognizer.from_transducer(
            tokens=str(asr_dir / 'tokens.txt'),
            encoder=str(asr_dir / 'encoder-epoch-99-avg-1.int8.onnx'),
            decoder=str(asr_dir / 'decoder-epoch-99-avg-1.int8.onnx'),
            joiner=str(asr_dir / 'joiner-epoch-99-avg-1.int8.onnx'),
            num_threads=2, sample_rate=16000, feature_dim=80,
            decoding_method='greedy_search', provider='cpu',
        )
        self.asr_stream = self.recognizer.create_stream()
        self.stream_samples = 0

    def feed(self, frame):
        import numpy as np
        samples = np.frombuffer(frame.data, dtype='<i2').astype(np.float32) / 32768.0
        events = []
        primary_hit = self.keyword_streams.feed(samples)
        alternate = getattr(frame, "alternate_data", None)
        alternate_hit = False
        if alternate is not None:
            alternate_samples = np.frombuffer(alternate, dtype='<i2').astype(np.float32) / 32768.0
            alternate_hit = self.secondary_keyword_streams.feed(alternate_samples)
            self._alternate_active = True
        elif self._alternate_active:
            self.secondary_keyword_streams.reset()
            self._alternate_active = False
        if primary_hit or alternate_hit:
            events.append(Detected(frame.timestamp + frame.sample_count / frame.sample_rate))
        # A bilingual transducer provides a second open-source keyword path
        # for the user's spoken "Astero", which it often renders "ASTRO".
        self.asr_stream.accept_waveform(16000, samples)
        self.stream_samples += len(samples)
        while self.recognizer.is_ready(self.asr_stream):
            self.recognizer.decode_stream(self.asr_stream)
            text = self.recognizer.get_result(self.asr_stream)
            if not isinstance(text, str):
                text = text.text
            if not events and re.search(r'\b(?:ASTERO|ASTRO)\b', text, re.IGNORECASE):
                events.append(Detected(frame.timestamp + frame.sample_count / frame.sample_rate))
        if events or self.stream_samples >= 16000 * 8:
            self.asr_stream = self.recognizer.create_stream()
            self.stream_samples = 0
        if events:
            self.keyword_streams.reset()
            self.secondary_keyword_streams.reset()
        return events

    def reset(self):
        self.keyword_streams = _OverlappingKeywordStreams(self.spotter)
        self.secondary_keyword_streams = _OverlappingKeywordStreams(self.spotter)
        self._alternate_active = False
        self.asr_stream = self.recognizer.create_stream()
        self.stream_samples = 0
