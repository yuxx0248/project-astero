# Project Astero

**Astero** is an always-on, voice-first personal AI portal inspired by the EVE Online exploration frigate. It uses a modular speech pipeline, name-only wake word **"Astero"**, music, conversation and an authenticated remote-agent bridge. ASUS A16 is a development workstation only.

> **Status: scaffold / hardware discovery.** Plan A is the accepted target architecture, **not an implemented or validated audio system**. The owner reports successful Echo Dot 2 rooting; audio capture/streaming and Xeon Bluetooth output remain unverified.

> **Voice Bridge 组件归属**：TTS、播报队列、播放器和通用 Python SDK 由 [voice-bridge](https://github.com/yuxx0248/voice-bridge) 仓库维护；Astero 只拥有会话与语音编排，并通过兼容导入使用 `voice_bridge.client`。Astero 的 package metadata 固定依赖 Voice Bridge SDK commit `9e18abb10be617de51c808fd950c517111238d0a`。这项代码依赖不代表已部署到运行中的 venv；安装与回滚步骤见 [迁移说明](docs/VOICE_BRIDGE_MIGRATION.md)。

## Deployment priority (2026-11-18)

### Plan A — Xeon server-first (primary)

```text
Echo Dot 2 (2016, root reported successful; mic access unverified)
    -- Wi-Fi / LAN microphone audio -->
Xeon server (Astero Core: wake/VAD/AEC/ASR/agent/Voice Bridge/TTS/music)
    -- USB Bluetooth adapter if needed / A2DP -->
Spherical Echo (exact Dot 4/5 model TBD; Bluetooth speaker, stock firmware)
```

- Reuse existing always-on Xeon compute; **Pi 4 is not in Plan A's runtime path**.
- Echo Dot 2 is intended as a microphone satellite, not a high-quality speaker. Its seven-mic array and stock processing **must not be assumed accessible after root**.
- The spherical Echo is output only, with no root required. Verify Bluetooth receiver pairing, reconnect and playback. If it's a Dot 4/5, its 3.5mm port is not an AUX input.
- Xeon has Realtek ALC887 analog output and NVIDIA HDMI audio, but **neither is used for Bluetooth playback**, nor do they provide Bluetooth radio. Detect existing controller first; buy a Linux-supported USB adapter **only if required**.
- Approx. 3 m through a wall is promising for Bluetooth but requires actual reliability testing.
- Name-only wake **"Astero"** on the Xeon initially, with a continuous local-network microphone stream. No "Hey Astero" prefix. Keep raw-audio retention disabled by default.
- Voice Bridge repository/API has been audited; reuse its local speech.cpp/Qwen3-TTS service and notification API via an Astero adapter, without copying the entire repository.
- Echo microphone capture, LAN streaming, playback timing and AEC/barge-in are hard **P0 gates**, not promised features.

### Plan B — Raspberry Pi 4 (fallback; original architecture)

```text
Existing Bluetooth microphone -- BT input --> Pi 4 (wake/VAD/AEC/ASR/router/TTS)
    -- Bluetooth A2DP --> spherical Echo speaker
```

Use if Echo Dot 2 rooting/capture/streaming cannot be made reliable, or the server-first deployment fails its operational gates. Maintain original constraints: no new audio hardware purchases for initial testing, reuse Voice Bridge if verified, test simultaneous Bluetooth capture/playback, and offload remote agents as necessary.

## Read first

- [Product requirements](docs/PRODUCT.md)
- [Architecture & contracts / Plan A and Plan B](docs/ARCHITECTURE.md)
- [Hardware decision record](docs/HARDWARE_DECISIONS_2026-11-18.md)
- [Validation / P0 gates](docs/VALIDATION.md)
- [Voice Bridge reuse audit and integration plan](docs/VOICE_BRIDGE.md)

## Development setup (dry-run only)

Requires Python >= 3.11; existing scaffold uses standard library only.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -v
python -m astero --config configs/astero.example.json --demo
```

For a paired local checkout, install the Voice Bridge worktree editable first,
then install Astero without replacing that SDK with its pinned Git dependency:

```sh
python -m pip install -e ../voice-bridge
python -m pip install --no-deps -e .
```

The demo only runs the control-plane state machine and dry-run gateway; it does not capture microphone input, synthesize speech or execute real agent actions. Current example configuration may reflect the original Pi scaffold and is **not** a working Xeon configuration.

## Phases

1. **P0:** verify exact hardware models, Dot 2 post-root microphone access and recovery information, access to usable mic stream, Wi-Fi continuity, server Bluetooth A2DP, playback reference and AEC feasibility.
2. **P1:** calibrate name-only wake model at 1–3 m; VAD, pre-roll, Mandarin/English ASR, false-awaken and latency trials.
3. **P2:** integrate audited Voice Bridge, then improve streaming TTS, ducking, stop/barge-in, latency and fallback to half-duplex.
4. **P3:** secure Agent Gateway, progress/cancel/confirm and follow-up windows.
5. **P4:** music integration, headless startup, reconnection and 24h reliability soak.

## Privacy/security

Rooting any Echo may void support or brick the device. Follow hardware/firmware-specific verified procedures and preserve a recovery path; never assume all stock acoustic DSP features survive. Use authenticated service endpoints, default-deny dangerous actions, and avoid retaining voice data unless explicitly enabled.

## Attribution

Inspired by EVE Online's Astero ship name; not affiliated with CCP Games.
