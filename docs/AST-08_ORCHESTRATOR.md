# AST-08 Orchestrator

`astero.orchestrator.Orchestrator` runs one wake-to-playback turn with injected
components. It accepts timestamped 16 kHz mono `MonoFrame` objects from the
audio conversion boundary, maintains the endpoint's bounded pre-roll before
wake, and then calls the endpoint collector, ASR, conversation backend, and
Voice Bridge adapter in order.

## Adapter contract

- Wake detector: `feed(MonoFrame) -> iterable of events` with `timestamp` and
  optional `score` attributes.
- Endpoint: `feed(MonoFrame)`, `wake(WakeEvent)`, `cancel(timestamp)`, and
  `stream_lost(timestamp)`. Wake frame is fed exactly once before the wake
  event so its samples can be included in pre-roll.
- ASR: `recognize(MonoFrame) -> string` or an object with a `text` field.
- Conversation: `respond(text, session_id) -> non-empty string`.
- Playback: `announce(text) -> task_id`, followed by
  `get_status(task_id) -> status string` or an object/dict with a `status`
  field containing queued, generating, playing, completed, or failed. The
  adapter must map HTTP 202 to a task ID; queued is not completion.

The controller reports `IDLE`, `LISTENING`, `PROCESSING`, `SPEAKING`, then
`IDLE`. Failures and cancellations pass through `FAILED` or `CANCELLED` before
returning to `IDLE`. It does not read further microphone frames while polling
playback, so wake detection and ASR cannot be triggered by Astero's own reply.
An audio iterator ending after wake is an explicit stream-loss failure. A
playback timeout or cancellation after enqueueing sets
`bridge_may_still_play`, because the Bridge has no per-task cancel endpoint.
The controller clears endpoint pre-roll on idle EOF/cancellation and calls an
available wake detector `reset()` after each turn to discard partial chunks
and its pre-roll.

## Offline smoke check

Run `python -m astero --manual-text 'hello'`. This routes text through the
local mock conversation backend and mock playback and prints JSON. It verifies
only this adapter boundary, not microphone capture, wake detection, real ASR,
an authorized LLM, real playback, or user audibility. The example configuration
is in `configs/orchestrator.example.json`.

## Verification

Run `python -m unittest tests.test_orchestrator -v` for the injected end-to-end
tests. Real full-chain verification remains outstanding until an Astero wake,
spoken command, ASR result, authorized LLM response, Bridge `completed` status,
and user-confirmed audibility are observed together.
