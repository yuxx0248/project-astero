"""Silero adapter with short model hangover and explicit endpoint ownership."""
from __future__ import annotations

import math
from pathlib import Path


class SileroVAD:
    """Feed 512-sample windows while leaving long silence policy to endpoint.

    sherpa's default 0.5 s hangover otherwise adds to EndpointCollector's
    end-silence setting. Keep 0.1 s here to smooth frame decisions; the service
    defaults to a further 0.8 s endpoint silence. These are independently
    configurable for environments requiring longer pauses.
    """

    def __init__(self, path: Path, *, min_silence_seconds: float = 0.1):
        if not math.isfinite(min_silence_seconds) or min_silence_seconds < 0:
            raise ValueError("VAD minimum silence must be finite and nonnegative")
        import sherpa_onnx as s
        cfg = s.VadModelConfig()
        cfg.silero_vad.model = str(path)
        cfg.silero_vad.threshold = 0.5
        cfg.silero_vad.min_silence_duration = min_silence_seconds
        cfg.sample_rate = 16000
        cfg.num_threads = 1
        self.model = s.VadModel.create(cfg)
        self.pending = bytearray()
        self.last = False

    def is_speech(self, frame):
        import numpy as np
        self.pending.extend(frame.data)
        while len(self.pending) >= 1024:
            samples = np.frombuffer(bytes(self.pending[:1024]), dtype='<i2').astype(np.float32) / 32768.0
            del self.pending[:1024]
            self.last = bool(self.model.is_speech(samples))
        return self.last
