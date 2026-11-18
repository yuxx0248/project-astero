"""Local wake-word detection adapters."""

from .detector import (
    DetectorConfig,
    ModelUnavailableError,
    OpenWakeWordModel,
    WakeDetector,
    WakeEvent,
)
from .testing import SequenceWakeModel

__all__ = [
    "DetectorConfig",
    "ModelUnavailableError",
    "OpenWakeWordModel",
    "SequenceWakeModel",
    "WakeDetector",
    "WakeEvent",
]
