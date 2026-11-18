# Project Astero

**Astero** is a Raspberry Pi 4-based, always-on, voice-first personal AI portal inspired by the EVE Online exploration frigate. It is a self-hosted smart-speaker experience: music, a name-only wake word ("Astero"), spoken conversations, and a bridge to remote agents. The ASUS A16 is a **development workstation only**.

> **Status: scaffold / hardware discovery.** No real wake detection, AEC, ASR, Bluetooth audio, music playback, or voice synthesis is claimed operational yet.

## Non-negotiable MVP constraints

- No new hardware purchases. Reuse a **Bluetooth microphone** and an existing **spherical Amazon Echo** as Bluetooth speaker.
- Raspberry Pi 4 stays on 24/7. A16 is *not* a runtime dependency.
- One room, approximately 3 m pickup range, including music playback.
- Say **"Astero" alone**, never "Hey Astero". Name-only custom wake model to be trained and calibrated.
- Prefer on-Pi wake detection, voice activity detection, ASR and speech output when performance permits. Offload LLM reasoning / coding agent execution to an independently available service.
- Pi controls playback so echo cancellation has access to playback-reference audio. Spotify Connect via librespot/Raspotify is a possible integration.
- Reuse an existing **Voice Bridge** TTS capability if compatible; its repository has not yet been identified or audited.

## Read first

- [Product requirements](docs/PRODUCT.md)
- [Architecture and integration contracts](docs/ARCHITECTURE.md)
- [Risks, experiments and acceptance](docs/VALIDATION.md)
- [Voice Bridge reuse decision](docs/VOICE_BRIDGE.md)

## Development setup (dry-run, no audio hardware required)

Requires Python >= 3.11. The scaffold uses only Python's standard library.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m unittest discover -s tests -v
python -m astero --config configs/astero.example.json --demo
```

The demo exercises the state machine and a **dry-run** command gateway; it does **not** activate microphones, execute arbitrary shell commands or send tasks over the network.

## Planned phases

1. **P0 Audio feasibility:** enumerate BT microphone input and Echo output independently, pair/reconnect, capture playback reference, measure latency and echo cancel feasibility.
2. **P1 Wake & capture:** name-only custom wake model, VAD and end-of-turn, audio pre-roll, false-trigger measurements.
3. **P2 Speech round-trip:** local ASR; existing Voice Bridge TTS adapter (if confirmed), fallback local TTS; safe playback and music ducking.
4. **P3 Agent:** authenticated transport, progress/cancel/confirm events, follow-up window, long-running task management.
5. **P4 Music & reliability:** Spotify Connect, stable simultaneous listening, reconnect and reboot recovery, 24h soak.

## Safety/privacy

Default-deny privileged operations; require confirmation for destructive actions. Avoid retaining raw audio by default. Do not expose agent execution on the LAN without authentication. Never claim success based only on a dispatched request.

## Attribution

Inspired by EVE Online's Astero ship name; not affiliated with CCP Games.
