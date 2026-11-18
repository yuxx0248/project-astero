# Development notes

Run from the repository root:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m unittest discover -s tests -v
python -m astero --config configs/astero.example.json --demo
```

Current adapter classes are **control-plane test doubles** only; no mic capture, wake inference, networking, real music control or TTS output. Do not install the sample systemd unit expecting an always-on assistant.

## P0 host probes (run on Pi, inspect before changing audio routes)
```sh
uname -m
cat /proc/device-tree/model
bluetoothctl devices
pactl info
pactl list short sources
pactl list short sinks
wpctl status
```

Use this evidence to choose audio devices and discover PipeWire echo-cancel availability. Hardware output/profile capabilities must be confirmed before implementation.

## Expected next feature additions
1. `astero/audio/`: capture, ring buffer, reference mixer, device reconnect.
2. `astero/wake/`: model wrapper and threshold calibration for **Astero** alone.
3. `astero/speech/`: VAD+ASR streaming and TTS/Voice Bridge adapters.
4. `astero/music/`: librespot playback control and transactional duck/restore.
5. `astero/agent/`: authenticated task transport with progress and confirmation.

Do not commit raw microphone recordings, third-party model weights or tokens to this public repository.
