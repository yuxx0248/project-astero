# Hardware decision record — 2026-11-18

**Status: architecture reprioritized, implementation unverified.** This record supersedes the earlier Pi-first decision in this file. No new microphone/speaker purchases authorized.

## Plan A — primary: Xeon server-first

- Always-on Xeon server runs Astero Core (name-only wake "Astero", VAD, ASR, music/mixer, Voice Bridge TTS adapter, authenticated Agent Gateway); A16 is dev-only, Raspberry Pi 4 **not required**.
- Existing **Echo Dot 2 (2016)** is a **candidate** rooted microphone satellite communicating with Xeon through Wi-Fi. Root access, access to processed mic array audio, stable capture and LAN streaming are **unverified**; hardware/firmware specific verification is mandatory.
- Existing spherical Echo is Bluetooth **speaker only**, no root required. It is believed to be a small Dot 4/5 but exact generation must be verified. We are not relying on its microphone.
- Xeon and output Echo are approximately **3 m apart through one wall**; test Bluetooth signal quality, buffering and auto-reconnect.
- Xeon has **Realtek ALC887-VD analog audio** and **NVIDIA GTX 970 GM204 HDA HDMI audio**. These are not needed for Wi-Fi mic + A2DP speaker; they do not establish presence of a Bluetooth controller. Inventory first and use a USB Bluetooth adapter only if needed.
- Pi-to-Echo Bluetooth audio output is replaced with server-to-Echo Bluetooth A2DP in Plan A. Server playback reference can feed AEC, but independent microphone and speaker clocks/BT delay are major challenges.
- Initial wake detection should run on Xeon over a continuous LAN microphone stream. **Wake word = "Astero" alone**, not "Hey Astero".
- Prefer reusing Voice Bridge's working TTS via a verified interface rather than copying unknown source code.
- Initial hardware procurement: **none**, except optional USB Bluetooth dongle after confirming the server lacks Bluetooth.

## Plan B — retained original Raspberry Pi 4 setup

- Pi 4 is the always-on portal; existing Bluetooth microphone is input, spherical Echo via Bluetooth A2DP is output.
- Pi runs modular wake/VAD/ASR/AEC/mixer/controller and routes heavy agent work elsewhere.
- Original risk remains: two Bluetooth input/output profiles in parallel, variable latency and AEC timing. Validate before promoting fallback.
- Preserve ability to use an independent microphone/HAT later if all already-owned hardware options fail; no new equipment now.

## Longer-term product direction

Separate external speaker, no custom Hi-Fi speaker/amp enclosure. A custom 3D-printed terminal shell and GPIO/I²S microphone array remain optional **future** directions, not dependencies. No separate sound card or DAC required for current wireless Plan A.

## Decision gates

1. Confirm exact Echo devices, server OS, Bluetooth controller and root/firmware compatibility.
2. Prove Dot 2 microphone capture with appropriate signal processing, continuous Wi-Fi transport and safe recovery path.
3. Prove Xeon Bluetooth output to spherical Echo through wall, startup and reconnection.
4. Measure self-echo/AEC, wake latency, music ducking, interruption and 24h stability.
5. If Plan A fails, switch to preserved Pi-first Plan B; do not assume Plan B's concurrent Bluetooth I/O is already working.

## Implementation status

Only dry-run software scaffolding exists. No hardware root, AEC, wake, TTS, Bluetooth or audio streaming functionality is claimed implemented.
