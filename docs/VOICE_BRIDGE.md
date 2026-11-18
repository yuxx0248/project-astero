# Existing Voice Bridge reuse assessment

The user reports an existing repository named / described as **Voice Bridge** with working voice output.

## Discovery result (2026-11-18)
- The linked GitHub account `yuxx0248` exposes `project-astero`, but no repository whose name contains `voice` or `bridge` was found in accessible listings/search.
- Its source, protocol, repository URL, license, supported CPU architecture and audio playback behavior are **not verified**.
- **No code copied** and no hard dependency included until repository provenance and API are established.

## Integration decision
Prefer an **external Voice Bridge integration** over copying:
1. Verify exact repository URL, owner, branch/commit and license.
2. Inspect public API or CLI, audio output format, streaming behavior, Python environment, deployment assumptions, ARM64 compatibility, logs and error modes.
3. If it is a network service, build `VoiceBridgeSpeaker` adapter; isolate endpoint/auth in environment config and keep device-specific audio route on Pi.
4. If it is an importable library, pin version and adapt without copying, unless licensing and portability justify vendoring.
5. If it is only a desktop tool, expose a minimal compatible TTS service with permission and explicit deployment plan, or use local Piper fallback.
6. Verify Chinese/English voices, latency, cancellation, barge-in, music ducking and success/error semantics.

Do **not** blindly copy code, since the user's working audio implementation might rely on unavailable hardware, cloud services or environment secrets.

## Placeholder
The v0 scaffold's `NullSpeaker` logs TTS text without emitting sound. It is intentionally nonfunctional audio output; the real Voice Bridge adapter is a P2 task.
