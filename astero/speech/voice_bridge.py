"""Compatibility imports for the canonical Voice Bridge SDK.

Voice Bridge owns its HTTP client contract in `voice_bridge.client`.
Astero owns conversation orchestration and can import these public names
through this module without keeping a second implementation.

Before merging/installing this change:
    /home/zuohang/.local/share/astero/venv/bin/python -m pip install -e /home/zuohang/workspace/voice-bridge
"""

try:
    from voice_bridge.client import (
        AnnouncementResult,
        VoiceBridgeClient,
        VoiceBridgeConfig,
        VoiceBridgeError,
        VoiceBridgeProfileMismatchError,
        VoiceBridgeResponseError,
        VoiceBridgeTaskError,
        VoiceBridgeTimeoutError,
        VoiceBridgeUnavailableError,
    )
except ModuleNotFoundError as exc:
    if exc.name in {"voice_bridge", "voice_bridge.client"}:
        raise ModuleNotFoundError(
            "Project Astero requires the canonical Voice Bridge client SDK. "
            "Install the voice-bridge checkout into Astero's Python environment "
            "before running Astero (python -m pip install -e /path/to/voice-bridge)."
        ) from exc
    raise

__all__ = [
    "AnnouncementResult",
    "VoiceBridgeClient",
    "VoiceBridgeConfig",
    "VoiceBridgeError",
    "VoiceBridgeProfileMismatchError",
    "VoiceBridgeResponseError",
    "VoiceBridgeTaskError",
    "VoiceBridgeTimeoutError",
    "VoiceBridgeUnavailableError",
]
