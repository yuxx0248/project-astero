# Astero as a Voice Bridge consumer

**Authoritative implementation:** [Voice Bridge](https://github.com/yuxx0248/voice-bridge), specifically `voice_bridge/__main__.py` (TTS / queue / playback server), `voice_bridge/client.py` (generic Python SDK), and `docs/CLIENT_SDK.md` (HTTP contract).

Astero owns the conversational experience and calls Voice Bridge as an external service. Astero must not fork or copy TTS synthesis, FIFO queues, WAV validation, the `paplay` subprocess, or generic HTTP client state/validation. `astero/speech/voice_bridge.py` is now a **compatibility re-export** of the upstream SDK; existing Astero call sites need not change.

## Local development / deployment precondition

This branch changes a Python import and is not safe to merge or deploy before installing the SDK into Astero's venv:

```sh
/home/zuohang/.local/share/astero/venv/bin/python -m pip install -e /home/zuohang/workspace/voice-bridge
/home/zuohang/.local/share/astero/venv/bin/python -c 'from voice_bridge.client import VoiceBridgeClient; print(VoiceBridgeClient.__module__)'
/home/zuohang/.local/share/astero/venv/bin/python -m unittest discover -s tests -v
```

Astero declares the SDK as a direct, immutable Git commit dependency in
`pyproject.toml`. A clean deployment install with `python -m pip install -e .`
therefore resolves the same SDK without relying on the current directory or
`PYTHONPATH`. For a local paired checkout, install that Voice Bridge checkout
editable first, then run `python -m pip install --no-deps -e .`.

From the Voice Bridge repository, also run `python3 -m unittest discover -s tests -v`. Finally run **one explicit real A16/Echo end-to-end speech turn** and verify the listener resumes and archives are complete before enabling the changed user service. Do not modify the running service or its virtualenv during the migration without a rollback path.

The runtime still expects local loopback `http://127.0.0.1:8765`. Voice Bridge accepts HTTP 202 as **queued**, not success. Its `completed` status means the player exited successfully, not verified audibility. Its API has no cancel endpoint; a timed-out Astero call may leave a queued or playing announcement.

## Architecture boundary

- **Voice Bridge:** TTS, validation, announcement queue, player, optional future streaming/cancel/output selection, generic SDK, API versioning and contract tests.
- **Astero:** wake/listen lifecycle, utterance endpointing, ASR, agent request, per-turn tracing, acoustic validation, timing, follow-up/duck/barge-in coordination.
- **Shared handshake:** announce -> task_id, poll status. Astero maps it into its per-turn trace and never claims a physical first-audio timestamp from a polling event.
- **Security:** Voice Bridge is currently unauthenticated and loopback only. Do not treat this integration as permission to expose it to Xeon over LAN; use an authenticated transport when moving across machines.

The original AST-10 latency and acoustic benchmark documents record historical results and remain unchanged. This migration introduces no claimed TTS, performance or hardware feature.

## Rollback

Before deploying, retain the last working Astero main checkout/service unit. If SDK installation or the integration fails, revert only Astero's compatibility-import commit or switch back to the previously working main revision; the Voice Bridge server protocol is unchanged.
