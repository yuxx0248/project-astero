"""Offline wake scoring substitutes for integration tests and simulations."""

from __future__ import annotations

from collections.abc import Iterable


class SequenceWakeModel:
    """Return deterministic scores in order, then return 0.0 indefinitely."""

    def __init__(self, scores: Iterable[float]) -> None:
        self._scores = iter(scores)
        self.chunks_seen: list[tuple[bytes, int]] = []

    def score(self, pcm_s16le: bytes, sample_rate: int) -> float:
        self.chunks_seen.append((pcm_s16le, sample_rate))
        return next(self._scores, 0.0)
