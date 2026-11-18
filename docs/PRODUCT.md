# Product requirements — Astero v0

## Vision

Create a self-hosted Alexa-like voice portal: always ready for name-only **"Astero"**, with natural Mandarin/English conversation, music, voice commands and secure remote agent work. Prefer reusing existing devices. The target backend is the always-on Xeon server, with Raspberry Pi 4 retained as fallback.

## Deployment plans

- **Plan A (primary; unverified):** rooted 2016 Echo Dot 2 serves as Wi-Fi far-field microphone; Xeon runs the audio pipeline, wake model, speech and agent bridge; separate spherical Echo Dot 4/5 candidate is a stock Bluetooth A2DP speaker. The exact spherical model remains unverified. Server Bluetooth controller/USB adapter need verification.
- **Plan B (fallback, original):** Raspberry Pi 4 runs the audio portal, accepts the existing Bluetooth microphone and outputs to spherical Echo over A2DP. Test simultaneous BT capture and playback.
- ASUS A16 is for development only in either plan.
- Server and speaker are about 3 m apart with a wall between; measured connection stability is required. Xeon onboard ALC887 analog and GTX 970 HDMI audio are available but unused in Plan A's Bluetooth route.
- No new microphone or speaker purchase until feasibility evidence. A Linux-compatible USB Bluetooth adapter is an allowed **conditional** small purchase only if server lacks Bluetooth.
- Rooting and audio features of Echo Dot 2 are feasibility hypotheses, not established functionality.

## User journey

1. Host sends TTS or music to the spherical Echo through Bluetooth.
2. User says **"Astero" alone**, at up to ~3 m.
3. The microphone stream reaches the host; wake detector, with measured AEC/ducking, recognizes it and starts a short cue.
4. Pre-roll, VAD and endpointing capture a Mandarin or English command.
5. Local routing or an authenticated agent runs the work; progress and confirmations are truthful.
6. Voice Bridge or fallback TTS speaks the result; when reliable, allow barge-in and follow-up without repeated wake; restore music.
7. If backend is down, local controls can remain available where implemented.

## Functional requirements

- FR1: configurable name-only `Astero` wake model and threshold; never silently change to "Hey Astero".
- FR2: always-on capture stream, bounded jitter buffer, pre-roll, VAD, timeout and end-of-turn.
- FR3: local/LAN speech capture; no always-on cloud audio required.
- FR4: real measured acoustic echo suppression with playback reference; use half-duplex fallback if needed.
- FR5: common music/TTS mixer, duck and transactional volume restore.
- FR6: Mandarin and English ASR.
- FR7: agent task state, progress, cancel, error and privileged-action confirmation.
- FR8: reuse existing Voice Bridge after verifying repository, API and licensing.
- FR9: auto-start/reconnect, private diagnostics and optional opt-in recordings.
- FR10: local stop, barge-in when proven, privacy mute and short follow-up.

## Non-goals for MVP

- Purchasing a high-end integrated loudspeaker or new microphone before P0.
- Promising reuse of stock Echo beamforming, AEC or arbitrary on-device wake models.
- Requiring Pi 4 or A16 for the normal Plan A runtime.
- Running large coding models on the voice endpoint; unrestricted shell execution.

## Proposed quality targets (not measured)

- Wake at 3 m: >=95% in quiet and >=90% with normal music across adequately sampled trials.
- Wake-response P95 target <500 ms after the wake word ends, subject to measurement.
- Four-hour false-wake soak, reliable end-of-turn and no silent transcription loss.
- AEC/barge-in judged on recorded test scenes; allow fallback to pause/half-duplex.
- Bluetooth/Wi-Fi auto-reconnect and 24-hour uptime test; no fabricated task success.

## Decisions retained

Name = Astero, not "Hey Astero"; reusing Voice Bridge is preferred; Handy/A16 is separate from the always-on runtime; separate external speaker instead of DIY integrated Hi-Fi enclosure.
