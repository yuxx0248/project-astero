# 本机 Voice Bridge 更新 — 2026-11-18

当前阶段已改为先在 USB 连接 Echo Dot 2 的本机测试，不依赖 Xeon。真实设备已通过 ADB 验证 root、PCM 采集和一次合成语音声学回路；Voice Bridge 在本机健康运行。具体实现、结果和仍未验证的唤醒可靠性见 [本机语音回路说明](LOCAL_LIVE_RUN.md)。

以下远端审核记录保留其原有的 Xeon 长期部署背景和当时的状态；其中“仅用户报告 root”“未确认 PCM”等表述是审核时点的历史记录，不代表本机当前状态。

---

# Voice Bridge audit and Astero integration decision — 2026-11-18

**Source reviewed:** [yuxx0248/voice-bridge](https://github.com/yuxx0248/voice-bridge), default branch `main`; implementation in `voice_bridge/__main__.py`, `README.md`, `docs/deployment.md`, `docs/experiments.md`, `tests/test_bridge.py`, `deploy/systemd/*.service`. This supersedes the previous "repository not found" placeholder.

**Decision:** Keep Voice Bridge as a separate, loopback-only service and integrate through an Astero adapter. Do not vendor/copy its source or pretend the current notification API supports interactive full-duplex conversation. Existing repo does not contain a root LICENSE file; verify applicable code/upstream model licenses before redistribution.

## Verified from source (not Xeon runtime results)

- Python standard-library HTTP bridge, default `127.0.0.1:8765`, `GET /health`, `POST /announce`, `GET /announcements/{id}`.
- `POST /announce` accepts `text`, optional `voice` and `style`, responds **202 queued** with job ID. 202 is **not** synthesis or playback completion. Status lifecycle: `queued -> generating -> playing -> completed|failed`; completed means `paplay` exited successfully, which still does not guarantee audible output at the physical speaker.
- Serial worker, waiting queue default 32, completed/failed status retention default 256, HTTP 503 on full queue; prior job state is memory-only and lost on restart. Completed/failed jobs discard announcement text/style.
- `LocalAudio.synthesize` calls local `speech.cpp` at `127.0.0.1:8080/v1/audio/speech`; input fields `input`, `voice`, `instructions`, `response_format=wav`. Validates RIFF/WAVE and decoded frame length before passing WAV via stdin to `paplay`. `ffplay` is disallowed because playback failures can falsely report success.
- Current output is **full-WAV then play**, not streaming. HTTP API has **no cancel job endpoint**, no priority/preemption or per-session interruption. `LocalAudio.close` can terminate playback only on service shutdown.
- HTTP bind and TTS endpoint are deliberately **loopback-only and unauthenticated**; never simply expose to LAN.
- User systemd unit templates assume an interactive user's PulseAudio-compatible sound session and fixed deployment paths. Headless Xeon installation requires adapting this, not copying units unchanged.

## Model/runtime facts recorded in voice-bridge

- `speech.cpp 0.11.0`, pinned upstream commit `8d18924cd238b87c0297df112f7ff40783a6414f`; Qwen3-TTS 12Hz 1.7B CustomVoice Q8_0 GGUF, ~2.13 GiB model, documented SHA-256 `1bc0ef69547c003507b6536639a6080c8856382f4d0963b15d4475528330592d`.
- Existing `speech` executable is **AArch64** Linux CPU build, not executable on x86-64 Xeon. Rebuild/pin x86-64 from upstream source; copy the model through a verified transfer and checksum, not from Git.
- On the ARM64 development machine, documented steady service RSS ~2.3 GiB and three short announcement submit-to-completion durations ~3.6–5.0 s. **Not a Xeon benchmark and not time to first audio.**
- Tested voices include Mandarin `serena`, English `ryan`, default `vivian`; audio documented as 24 kHz, 16-bit mono WAV. Real Xeon performance, mixed Mandarin/English quality and Bluetooth output remain to be tested.

## Reuse matrix

| Part | Decision | Notes |
| --- | --- | --- |
| speech.cpp + Qwen3-TTS GGUF | **Reuse as standalone model service** | Rebuild x86-64, pin version and hash, benchmark |
| HTTP announce/status/health | **Reuse for asynchronous notifications** | Preserve semantics, loopback boundary |
| Synthesis request + WAV validation | **Adapt, do not duplicate blindly** | Build thin `VoiceBridgeSpeaker`/TTS adapter in Astero |
| FIFO single-worker queue | **Keep for background announcements** | Conversational speech needs priority, cancel, low latency |
| paplay/PipeWire routing | **Reuse for first playback smoke test** | Prove A2DP output/headless user audio session |
| systemd units | **Adapt to Xeon paths/user session** | Restart, linger, dependency/ready checks |
| Tests and error taxonomy | **Reuse test cases / behaviors** | Assert 202 != completion and playback failures |
| Streaming TTS, barge-in, cancel, duck/restore | **New work** | Not provided by current Bridge |
| Microphone, wake, VAD, STT, AEC | **Astero responsibilities** | Not in Voice Bridge |

## Proposed interfaces and state semantics

**MVP shortcut (no fork):** Astero `VoiceBridgeSpeaker` calls `POST /announce` on localhost, polls `/announcements/{id}`, and returns success only after `completed`. Bound polling, timeouts, error propagation, and speaker/music-state restoration are required. **Do not enter Astero FOLLOW_UP just because HTTP returned 202.** This enables a full-WAV, half-duplex conversational smoke test with existing API.

**Longer term:** Keep asynchronous announcements on existing Bridge. Introduce a separate interactive TTS/audio playback path sharing speech.cpp model service: priority over background announcements, cancellable synthesis/playback, sentence chunking/streaming where supported, playback start/end events, music ducking and PCM playback reference for AEC. Avoid two unsynchronized speakers or simultaneous writers to the same sink. The model server's ability to interrupt/stream must be confirmed before assuming an API.

**Proposed non-breaking Astero interface** (design only):
```text
TTSJob = synthesize(text, voice, style, priority, session_id)
AudioOutput.play(job) -> playing/completed/failed/cancelled
AudioOutput.stop(session_id) -> acknowledgement
```
Keep Bridge's existing endpoints backwards compatible. The existing `Portal` control-plane `Speaker.speak(text) -> None` is synchronous; adapting it to an async job requires adjusting state transitions and tests rather than treating job acceptance as playback completion.

## Immediate execution sequence

1. **Echo input P0:** User reports Echo Dot 2 root succeeded; record exact image/firmware, audio device and mic channels; prove 30 s clean capture and continuous authenticated LAN streaming to Xeon (timestamps/dropouts/latency).
2. **Xeon output P0:** inspect OS/CPU/BlueZ/PipeWire and BT adapter; pair spherical Echo A2DP across one wall; rebuild speech.cpp x86-64; verify checksum and TTS playback via `paplay`; measure latency/reconnect.
3. **P0 closure:** connect recorded Echo microphone audio to a basic Mandarin/English ASR engine and predefined response via existing Bridge; no wake word or agent needed yet.
4. **P1:** train/calibrate name-only **"Astero"** wake model on Xeon; implement VAD/pre-roll and secure agent routing.
5. **P2:** implement interruption/streaming/priority and measure AEC + real barge-in. Half-duplex and duck/pause are acceptable fallback.
6. **P3/P4:** operational lifecycle, soak, auth, privacy and multi-room only after round-trip works.

## Explicit status

User **reports Echo Dot 2 root successful**; this is not independently reproduced by this audit. No claim of working Echo audio capture, x86-64 speech.cpp build, Xeon Bluetooth speaker connection, wake, AEC, STT or end-to-end Astero playback. No hardware/code changes were made by this document-only audit.
