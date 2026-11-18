# Product requirements — Astero v0

## Vision
Create a DIY Alexa-like voice portal: one Raspberry Pi 4 and reused audio devices, permanently ready for a **name-only** "Astero" wake, conversational queries, music playback and command execution via a background AI agent. A16 is only the development environment.

## User journey
1. Spotify/local music is playing from Pi to Bluetooth Echo.
2. User says "Astero" at up to ~3 meters.
3. Post-AEC wake detector recognizes name without any prefix. Duck music; provide a brief cue.
4. Record speech, tolerate pauses, determine end-of-turn with VAD and optional semantic endpointing. Handle silence/no speech timeouts.
5. Lightweight local ASR produces text. Agent router decides local music control / simple action vs remote agent task.
6. Long tasks give accurate progress feedback without pretending completion. Confirm dangerous commands.
7. Speak results via TTS, allow barge-in and a short follow-up window without repeating the name; restore music afterwards.
8. If backend is unavailable, respond clearly and retain local music/control features.

## Hardware
- Raspberry Pi 4 (RAM variant unverified; use arm64 OS), 24/7.
- Existing Bluetooth microphone: model/profile unknown; **must verify HFP/HSP input and simultaneous Echo A2DP output**.
- Existing spherical Amazon Echo: Bluetooth speaker playback, not its internal mic.
- No hardware purchases in MVP. One ~3 m room. No screen requirement.
- Bluetooth speaker playback adds latency/drift to AEC. Hardest early feasibility gate.

## Functional requirements
- FR1: keyword-only wake "Astero"; configurable model and wake threshold.
- FR2: audio pipeline continuously monitors; streaming pre-roll; recording timeout; VAD endpoint.
- FR3: process microphone audio locally; no cloud wake or always-on cloud audio.
- FR4: playback reference enters software AEC where supported; distinguish AEC from source separation.
- FR5: music and TTS share managed mixer, with volume ducking, restore and echo-safe playback.
- FR6: multilingual Mandarin and English commands; prefer separate models/settings if needed.
- FR7: Agent Gateway accepts structured task requests, yields task ID, progress, completion, error, cancellation, confirmation.
- FR8: configurable speech output adapter; inspect and reuse existing Voice Bridge rather than duplicate TTS.
- FR9: systemd autostart, watchdog, graceful recovery, local diagnostics without storing conversations by default.
- FR10: TTS interruption, follow-up turn, local 'stop' and privacy mute.

## Non-goals for MVP
- Local 11B–30B end-to-end speech-to-speech model or on-Pi coding LLM.
- Shipping a polished integrated speaker enclosure.
- Far-field guarantees with untested Bluetooth microphone.
- Full smart-home integrations or unbounded agent shell privileges.
- Direct access to Echo's microphones, Alexa internal audio or Alexa wake engine.

## Quality targets (not measured)
- 20/20 short baseline smoke tests, then >=95% wake true-positive in quiet at 3 m and >=90% with normal music volume; validate false triggers over >=4 hours of music/podcasts.
- Responses do not falsely report task success; confirmations for destructive work.
- Music restores original volume after reply, failure or timeout.
- Device recovers after reboot and BT reconnect. AEC measured rather than assumed.

## Prior decisions
- Name: Astero (EVE Online ship); no "Hey".
- Choose 3-syllable wake word and train/customize with own room-noise samples.
- Traditional modular speech pipeline on Pi for resource efficiency; end-to-end voice LLM deferred.
- Microphone + Echo Bluetooth chosen to save cost; software WebRTC/PipeWire echo cancellation initially.
- Existing Handy stays separate (A16 dictation); not a required Pi dependency.
