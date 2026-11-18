# Validation / gates

No hardware claims are verified yet. Keep evidence for the exact Pi OS/kernel, Bluetooth codecs, device models, audio routes and model versions.

## P0 — feasibility before investing in AI code
- [ ] Record Pi 4 RAM variant, OS 64-bit, CPU temperature, audio server.
- [ ] Identify Bluetooth microphone manufacturer/model and whether capture profile works.
- [ ] Pair Echo as A2DP playback device; auto reconnect on boot.
- [ ] **Simultaneously** record the BT microphone and play Echo music; confirm no device/profile conflict.
- [ ] Capture local music playback reference, align with microphone track; measure Bluetooth delay and drift over 30 minutes.
- [ ] Validate PipeWire echo-cancel plugin API and compare raw/AEC output while playing music.
- [ ] If Bluetooth AEC cannot track, first test duck/pause-on-wake fallback; don't assume buying a new mic is necessary.

## P1 — wake and endpoint
- [ ] Obtain/train a name-only 'Astero' model using varied own utterances, multiple distances and music/noise negatives.
- [ ] Quiet 1 m and 3 m: >=20 trials each; track true-positive, latency, false rejects.
- [ ] Music normal volume 3 m: >=20 trials, 4h false wake soak.
- [ ] Run ASR Mandarin / English sentences, record word errors and endpoint truncation.
- [ ] Voice inactivity 5s, pause tolerance 1.5–3s, max turn 90s (tune experimentally).

## P2 — response
- [ ] Discover existing Voice Bridge repository and audit license, dependencies, API, voice quality and ARM64 suitability.
- [ ] Adapter sends sample text; speaker emits audible audio with no overlap corruption.
- [ ] Duck/music restoration after reply, timeout, cancel and error.
- [ ] Barge-in during TTS without self-trigger; fall back to half-duplex if unreliable.

## P3 — agent
- [ ] Mock agent accepted -> running -> completed transitions.
- [ ] Error, timeout, cancel, confirmation required, follow-up within 10–15s.
- [ ] No result is announced as finished before remote completion.
- [ ] Authentication and privilege boundaries verified.

## P4 — stability
- [ ] Boot/headless startup, BT reconnect, 24-hour idle soak, network outage recovery.
- [ ] CPU and memory use, audio xruns, per-phase latency, thermal throttling and power recorded.
- [ ] No audio/content retention without opt-in.

## Decision gates
- **Blocker:** Pi cannot simultaneously use BT microphone + Echo output; adapt routing or choose different already-owned device before buying.
- **Blocker:** no accessible, independently running agent backend. In that case only local mock/music demo works.
- **Risk:** Echo music started by Alexa itself lacks Pi playback reference for software AEC.
- **Fallback:** lower/pause music, half-duplex; never silently claim full-duplex.
