# Voice Bridge SDK ownership migration (2026-11-19)

This branch consumes the canonical SDK from [Voice Bridge](https://github.com/yuxx0248/voice-bridge). `pyproject.toml` pins package `voice-bridge-local` to immutable commit `9e18abb10be617de51c808fd950c517111238d0a`. The pin keeps a fresh install reproducible and can be advanced only after the corresponding Voice Bridge change is committed and tested.

Current main baseline: Astero's `astero/speech/voice_bridge.py` implements a generic polling client and Voice Bridge owns the HTTP server. After this migration Voice Bridge additionally owns the client SDK. Astero's module is an import-compatibility layer and contains no HTTP state machine.

## Release order (do not merge blindly)

1. Merge the Voice Bridge SDK change first. A fresh Astero package install resolves the pinned commit; for local development, install the paired Voice Bridge checkout editable first and run `python -m pip install --no-deps -e .` in Astero.
2. Verify that `voice_bridge.client.VoiceBridgeClient` imports from the installed distribution and Voice Bridge server behavior is unchanged.
3. Run the Voice Bridge suite and Astero suite; preserve all current AST-10 acceptance evidence.
4. Test a real utterance and ensure no `202` is treated as completed, recording completion in the Astero trace, restoring listening afterward.
5. Merge Astero's dependent branch only after the above passes; restart its user service as a separate controlled step.

Current limitation: this migration has not been deployed into the existing Astero venv or systemd unit and has not passed real systemd or Echo hardware acceptance. Do not restart the paused service as part of the code-only release. For rollback, restore the prior Astero commit/package pin, reinstall it in the Astero venv, then start the service only as a separately controlled deployment step; Voice Bridge's existing `/announce` and status API remain compatible.

The Voice Bridge SDK does not implement streaming, cancellation, priority or multi-output routing. Future features belong upstream.

## Verification record (2026-11-19)

- Baseline paired PR test run: 135 tests, 133 passed, 2 skipped in an isolated Python 3.15 environment after installing the SDK and NumPy used by the existing live-adapter test.
- After adding the pinned package dependency, a fresh temporary venv installed Astero with `python -m pip install -e .`; `voice_bridge.client` imported from the installed `voice-bridge-local` distribution. The full Astero suite again ran 135 tests: 133 passed, 2 skipped.
- The plain system interpreter initially failed to import six test modules because it did not have the newly extracted SDK installed. The production Astero venv was not altered to test this migration.
- The Voice Bridge full suite passed all 26 tests. See the Voice Bridge repository's `docs/INTEGRATION_BASELINE.md` for the paired commits and read-only runtime status.

No live utterance or audio playback was performed. At verification time `astero-local.service` remained stopped; Voice Bridge and its TTS service remained running. Neither PR was deployed or merged.
