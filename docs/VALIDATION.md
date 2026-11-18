# Validation & decision gates

All audio paths remain **unverified**. The repo currently contains a dry-run scaffold only. Capture evidence with exact firmware/build versions, device IDs, commands and measured logs.

## P0-A — server-first / Echo Dot 2 / spherical Echo

- [ ] Identify exact 2016 Echo Dot 2 hardware revision, firmware, recovery capability and suitable documented root route. Do not flash before backup/recovery review.
- [ ] Verify root access in a controlled manner, without assuming a particular exploit works on current firmware.
- [ ] Establish usable audio capture from Echo Dot 2; identify sample rate/channels/processing, and whether native array DSP/beamforming/AEC is actually available.
- [ ] Choose/test a LAN streaming satellite protocol, persistent stream and auth. Log timestamp continuity, packet loss, gaps and reconnection.
- [ ] Record Xeon OS/kernel, CPU, current Bluetooth controller (`bluetoothctl list`, `lsusb`) and BlueZ/PipeWire/ALSA state. Built-in sound card does **not** imply Bluetooth.
- [ ] Identify spherical Echo generation; pair and play as Bluetooth A2DP sink without root. Test automated reconnect after host/Echo reboot.
- [ ] If no server Bluetooth controller exists, explicitly evaluate a Linux-supported USB Bluetooth adapter; avoid premature purchase.
- [ ] Measure stable A2DP reception across ~3 m and one wall, audio start latency, underruns, 30-min drift and wake/pause behavior.
- [ ] Verify host playback PCM reference and align it with mic signal; measure AEC attenuation and clock/drift correction in TTS/music cases.
- [ ] If AEC/barge-in is unreliable, prove duck/pause + half-duplex remains intelligible and does not self-trigger.
- [ ] Measure LAN-added latency P95 (goal <50 ms) and post-wake-word feedback latency P95 (goal <500 ms). Treat as targets, not measured guarantees.

### Plan A go/no-go

**GO** only if rooted Echo Dot 2 provides sustainable microphone frames, Xeon-to-Echo playback survives reboots, name-only wake works and echo/self-trigger is acceptably controlled. **NO-GO -> Plan B** if capture/root fails, latency/dropouts unacceptable or audio suppression cannot be mitigated with half-duplex.

## P0-B — original Raspberry Pi 4 fallback

- [ ] Record Pi RAM/OS/kernel, mic model/profile.
- [ ] Pair existing Bluetooth microphone via HFP/HSP and spherical Echo via A2DP.
- [ ] Confirm concurrent capture/playback without Bluetooth profile conflicts.
- [ ] Measure playback-reference alignment, delay drift and duck/pause fallback.
- [ ] Verify Pi reboot recovery, music and end-to-end round-trip.

## P1 — wake, ASR and endpoint (on selected host)

- [ ] Train/obtain **name-only "Astero"** wake model; no prefix. Calibrate varied speakers and noisy negatives.
- [ ] Quiet 1 m/3 m >=20 trials per condition; report true positive, detection latency and misses.
- [ ] Music 3 m >=20 trials plus >=4 h false-trigger soak.
- [ ] Mandarin/English ASR metrics; turn-end truncation and follow-up checks.

## P2 — reply and AEC

- [ ] Discover/audit existing Voice Bridge API, deployment, license and audio format; integration adapter rather than blind code copy.
- [ ] TTS Bluetooth playback and music duck/restore on completion/error/cancel.
- [ ] Barge-in under TTS; ensure no self-wake; accept half-duplex fallback.

## P3 — agent security

- [ ] Accepted/running/completed/failed/cancel/confirmation task events.
- [ ] No early success claims; authenticated transport and privilege guardrails.

## P4 — reliability

- [ ] Headless startup, reconnect on Wi-Fi/BT outage, 24h soak and thermal/CPU monitoring.
- [ ] No raw-audio or transcript retention by default.

## Procurement

No microphone, speaker, HAT, DAC or Pi-based add-on purchases before P0. Only consider small USB Bluetooth dongle if needed after hardware inventory.
