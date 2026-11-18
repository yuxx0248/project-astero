# Architecture & contracts

```text
Bluetooth Mic -> capture -> echo cancel (playback reference) -> wake model
                                                     |               |
Pi music player (librespot) -> PipeWire mixer -> Echo Bluetooth      v
                                                     |     pre-roll / VAD / ASR
                                                     |               |
                                                     +<- TTS <- reply router
                                                                     |
                                                    authenticated Agent Gateway
                                                                     |
                                                      remote LLM / coding agents
```

**A16 is build/test/deploy only; it is never part of the running request path.**

## Responsibilities
- **Audio Device Manager:** detect BT source/sink; monitor codec/profile, reconnect, configure 16-kHz mono model input while preserving A2DP playback. In practice concurrent BT mic + Echo can fail: gate this in P0.
- **AEC/Mixer:** PipeWire echo-cancel module with supported WebRTC implementation; route exact post-mixer playback reference, account for Bluetooth playback delays. Not all PipeWire builds expose the same options; inspect installed module docs. Avoid promising AEC3 specifically until implementation verified.
- **Wake Adapter:** optional openWakeWord custom "Astero" model or sherpa-onnx KWS; model not included. Output normalized confidence events. Threshold calibration and false-accept testing required.
- **VAD/Endpoint:** Silero VAD or sherpa-onnx; energy alone insufficient. Pre-roll ~500ms, speech wait ~5s, endpoint ~1.5s tunable, pause extension up to ~3s, max segment 90s initial targets.
- **ASR:** sherpa-onnx localized Mandarin/English model or tested alternative; should return transcript and language/confidence. Handy isn't assumed to have a Pi API.
- **Router:** local music commands and safe responses, otherwise send structured task to remote agent; session ID across follow-ups.
- **Voice Output:** reuse audited Voice Bridge by adapter, else Piper/sherpa-onnx local TTS. Must duck music and exclude synthesized speech from wake path where possible.
- **Music:** librespot / Spotify Connect when deployed; ensure Pi originates music so output reference is available. Music output directly originated from Echo itself cannot be referenced reliably for AEC.

## Service contracts
Python adapters deliberately remain independent from engine vendors:
- `WakeDetector.detect(frame) -> bool`
- `SpeechRecognizer.transcribe(audio) -> str`
- `Speaker.speak(text) -> None`
- `AgentGateway.submit(text, session_id) -> TaskReceipt`
- `MusicController.duck()/restore()`

**Transport proposal** (not implemented): `POST /v1/tasks`, `GET /v1/tasks/{id}`, `POST /v1/tasks/{id}/cancel`, event stream for `accepted/running/confirmation_required/completed/failed`; short-lived auth token + scoped tools. No remote arbitrary shell execution in v0.

## Voice lifecycle
`IDLE -> WAKE -> LISTEN -> PROCESS -> SPEAK -> FOLLOW_UP -> IDLE`.
Also `CANCEL`, `TIMEOUT`, `ERROR` transitions. A successful task requires backend completion, not a send acknowledgment. Stop/pause local playback when echo quality fails.

## Data and security
Audio ring buffer RAM only, no uploads unless explicitly configured. No stored transcripts by default. Device outbound-only to agent backend. Tools operate least-privilege and request confirmations. Encrypt credentials via root-owned config/environment store, never commit secrets.

## Alternatives reviewed
- Nemotron VoiceChat 11B / Qwen3-Omni / Moshi: native speech-to-speech interesting but not viable assumptions for realtime Pi 4 CPU; large accelerators usually required.
- A16 as host: rejected for always-on operation, retained as dev platform.
- Conference speakerphone / dedicated XMOS array: optional hardware fallback after P0 validation; do not buy for MVP.
