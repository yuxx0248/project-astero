"""Compatibility entry point for Voice Bridge's shared clone service."""

from voice_bridge.clone_service import (
    BaseVoiceSynthesizer,
    CloneHTTPServer,
    ClonePlaybackService,
    SynthesizedAudio,
    main,
)

__all__ = [
    "BaseVoiceSynthesizer",
    "CloneHTTPServer",
    "ClonePlaybackService",
    "SynthesizedAudio",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
