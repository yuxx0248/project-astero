"""Small deterministic ASR substitutes for offline tests and integration."""

from __future__ import annotations

from collections import deque
from typing import Iterable


class ScriptedRecognizer:
    """Return scripted strings or raise scripted exceptions, in order."""

    def __init__(self, outcomes: Iterable[str | BaseException]) -> None:
        self.outcomes = deque(outcomes)
        self.calls: list[tuple[tuple[float, ...], int]] = []

    def recognize(self, samples: tuple[float, ...], sample_rate: int) -> str:
        self.calls.append((samples, sample_rate))
        if not self.outcomes:
            raise RuntimeError("no scripted ASR outcome remains")
        outcome = self.outcomes.popleft()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome
