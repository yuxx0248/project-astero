# Voice Bridge SDK ownership migration (2026-11-19)

This branch depends on [the paired Voice Bridge branch](https://github.com/yuxx0248/voice-bridge/tree/refactor/voice-bridge-sdk-20261119).

Current main baseline: Astero's `astero/speech/voice_bridge.py` implements a generic polling client and Voice Bridge owns the HTTP server. After this migration Voice Bridge additionally owns the client SDK. Astero's module is an import-compatibility layer and contains no HTTP state machine.

## Release order (do not merge blindly)

1. Merge and install the Voice Bridge SDK branch first, in the **same Astero virtualenv**.
2. Verify that `voice_bridge.client.VoiceBridgeClient` imports and Voice Bridge server behavior is unchanged.
3. Run the Voice Bridge suite and Astero suite; preserve all current AST-10 acceptance evidence.
4. Test a real utterance and ensure no `202` is treated as completed, recording completion in the Astero trace, restoring listening afterward.
5. Merge Astero's dependent branch only after the above passes; restart its user service as a separate controlled step.

Current limitation: we have only authored compatibility changes and tests in GitHub branches, **not** performed a real systemd or Echo hardware acceptance. The new import will fail until Voice Bridge is installed into the Astero environment.

The Voice Bridge SDK does not implement streaming, cancellation, priority or multi-output routing. Future features belong upstream.
