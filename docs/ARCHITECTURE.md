# Architecture & integration contracts

## Decision: Plan A first, Plan B preserved

### Plan A — Xeon core + network microphone satellite

```text
Echo Dot 2 -- Wi-Fi PCM/Opus (protocol TBD) --> network capture adapter
   -> jitter buffer / timestamps -> resample -> AEC (if reference aligned)
   -> name-only "Astero" wake -> pre-roll / VAD -> Mandarin/English ASR
   -> local command or authenticated Agent Gateway -> answer / Voice Bridge TTS
   -> mixer / music duck / playback-reference tap -> BlueZ/PipeWire A2DP
   -> spherical Echo (unmodified Bluetooth speaker)
```

The always-on Xeon server hosts Astero Core. Neither the Raspberry Pi 4 nor ASUS A16 is in the runtime path. Existing Xeon ALC887/GTX970 HDMI sound hardware is irrelevant for Bluetooth playback; USB Bluetooth **radio**, if missing, is a separate peripheral. Server OS, BlueZ/PipeWire availability, Bluetooth controller and spherical Echo generation are to be confirmed.

The Echo Dot 2 (2016) is a candidate **rooted Wi-Fi microphone satellite**, not a normal USB microphone. Root or alternate firmware access does **not** prove Linux capture access to seven channels, Amazon beamforming/AEC, always-on streaming or controllable local wake engine. An audio satellite agent/protocol requires selection and measured compatibility before implementation. No arbitrary wake word support is presumed on Echo; run the name-only wake model on Xeon first.

For low perceived wake latency, establish a persistent LAN stream, bounded jitter buffering and ~0.5–1 s pre-roll. Track mic timestamps, dropout, end-to-end delays and Wi-Fi reconnect. Network proximity is favorable but cannot substitute for measurements.

**AEC is the major system risk:** Echo Dot 2 microphone and separate spherical Echo playback have independent clocks, network transport and Bluetooth playback buffering. Server-side PCM playback reference is necessary but not sufficient; measure delay/drift and suppression quality. Mitigate with duck/pause/half-duplex if full-duplex barge-in is unstable. Never claim the original Echo acoustic DSP survives root.

### Plan B — original Pi 4 voice portal

```text
Existing Bluetooth mic -> Pi 4 capture / AEC / "Astero" wake / VAD / ASR
Pi 4 music + Voice Bridge TTS -> PipeWire mixer -> Echo Bluetooth A2DP
Pi 4 -> authenticated Agent Gateway -> remote reasoning/coding backends
```

Preserved as the explicit fallback if Echo Dot 2 root/audio streaming fails, server-first reliability is poor or hardware constraints change. Validate simultaneous BT mic HFP/HSP input and Echo A2DP output, playback reference, delay/drift and audio profiles. A16 remains dev-only.

## Component boundaries (deployment-agnostic)

- **Audio input adapter:** source-independent frames, timestamps, sample rates, gap/drop counters and backpressure; `NetworkEchoMic` is proposed for A; Bluetooth mic adapter for B.
- **AEC + mixer:** playback-reference tap on the host actually emitting audio; adaptive delay tracking. Measure instead of assuming WebRTC/PipeWire guarantees.
- **Wake detector:** train/calibrate custom **"Astero"**, no "Hey" prefix. OpenWakeWord/sherpa-onnx considered but no model supplied.
- **VAD/endpoint:** streaming pre-roll; approximately 5 s speech wait, 1.5–3 s pause tolerance and configurable max duration are starting points.
- **ASR:** Mandarin/English-capable local engine on selected host where feasible; offline processing preferred.
- **Router/agent:** safe local commands or authenticated agent gateway; task IDs and truthful completion.
- **TTS:** external Voice Bridge adapter if verified, otherwise local TTS; support cancellation and music ducking.
- **Speaker transport:** BlueZ A2DP on Plan A Xeon or Plan B Pi. Verify echo speaker pairing/reconnect and current firmware behavior.
- **Music:** host-generated playback for a reference signal; music independently played by Alexa cannot be reliably cancelled with the host reference.

## Stable service contracts

```text
WakeDetector.detect(frame) -> bool
SpeechRecognizer.transcribe(audio) -> str
Speaker.speak(text) -> None
AgentGateway.submit(text, session_id) -> TaskReceipt
MusicController.duck()/restore()
```

Transport proposal, **not implemented**: authenticated `POST /v1/tasks`, task progress and cancel, events `accepted/running/confirmation_required/completed/failed`. No raw remote shell exposure.

## Lifecycle & security

`IDLE -> WAKE -> LISTEN -> PROCESS -> SPEAK -> FOLLOW_UP -> IDLE`; cancel/error/timeout transitions. Report completion only after backend success. Keep audio ring buffer memory-only and disable raw-audio persistence by default. Limit any rooted-device access to LAN and authenticated hosts.

## Alternatives

Dedicated mic HAT or purchased audio hardware is deferred until measured failures. Pi 4 is fallback, not the primary host. Larger on-device end-to-end voice LLMs are not an assumption. ASUS A16 is development only.
