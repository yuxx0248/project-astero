# Hardware decision record — 2026-11-18

Status: **Decision accepted; no purchases for MVP.**

## Final current direction
- **Raspberry Pi 4 is the always-on Astero voice portal.** ASUS A16 is used only to develop/test/deploy, never required to stay powered on.
- **MVP microphone:** reuse the Bluetooth microphone already owned. Model/profile and simultaneous input capability need verification.
- **MVP speaker:** reuse existing spherical Amazon Echo as an external Bluetooth speaker. Echo's internal microphone and Alexa processing are not available to the Pi over ordinary Bluetooth.
- **Audio input/output:** BT microphone input + Echo A2DP output, if Pi/BlueZ/PipeWire support the two simultaneously. Verify codec, routing, reconnect and stability in P0.
- **AEC:** software first, on Pi, using an actual playback reference from music/TTS emitted by Pi. Bluetooth latency, drift and variable buffering are a major unverified risk. Falling back to music duck/pause and half-duplex is acceptable for MVP.
- **Wake name:** exactly **"Astero"**, no "Hey" prefix. Train/calibrate name-only wake model.
- **Other features unchanged:** VAD intelligent end-of-turn, local lightweight ASR/TTS where practical, music playback and ducking, follow-up conversations, remote Agent Gateway, background task status, speech replies, privacy and tool confirmation.
- **Existing Voice Bridge:** prioritize reuse of its TTS when repository can be identified and audited; do not copy unverified code.

## Final physical-product direction (later, not MVP)
- Keep the **speaker external**. Do not build a custom Hi-Fi speaker chamber, amp or crossover into the Astero box. A high-quality external Bluetooth or wired powered speaker may be substituted later.
- A compact **3D-printed case for Pi + microphone array + optional status LED / mute switch** is an attractive next-stage design; keep microphone acoustically isolated from fans, vibration and speaker surfaces.
- Possible post-MVP GPIO/I²S microphone: ReSpeaker 2-Mics Pi HAT V2 (~$12.90 previously observed, not a committed purchase or guaranteed current price); 4-mic options only if 3 m/music tests justify them. Old HAT overlay/driver support must be verified against chosen Pi OS/kernel.
- **No extra sound card / DAC now.** Bluetooth is digital on both sides. Only consider a DAC if a future wired analog active speaker requires it; passive loudspeakers require amplification, not merely a DAC.
- Avoid assuming I²S mic HAT and I²S amplifier can be installed together without pin/clock/device-tree conflict, but this is out of scope since speakers remain external.

## P0 feasibility questions
1. Can Pi simultaneously capture the existing Bluetooth microphone and send quality music to the Echo without conflict?
2. Can PipeWire provide properly synchronized output-reference frames to a software echo canceller despite Echo Bluetooth playback delay/drift?
3. Does single-word Astero reliably trigger at 1 m and 3 m in quiet and with music? How many false triggers in a long music/podcast soak?
4. Can music volume be ducked/restored transactionally? Are BT devices reconnected on reboot?
5. Is the independently available backend Agent reachable when A16 is off? Can the TTS Voice Bridge be reused on Pi or by network service?

## Procurement rule
**Do not purchase microphones, speaker units, amplifiers, DACs, dedicated sound cards, or HATs for MVP.** Reconsider only after measured P0/P1 failures.

## Build status
Existing repo code is a **dry-run control-plane scaffold**, not a working audio assistant. This decision log does not claim that AEC, wake, bluetooth routing, ASR, TTS or music have been implemented.
