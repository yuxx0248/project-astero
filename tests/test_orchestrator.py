from dataclasses import dataclass
import threading
import unittest

from astero.audio.pcm import MonoFrame
from astero.orchestrator import Orchestrator, OrchestratorConfig, State, TurnStatus
from astero.speech.endpoint import EndpointEvent, EndpointKind, SpeechSegment


class FakeWake:
    @dataclass
    class Event:
        timestamp: float = 0.0
        score: float = 0.9

    def __init__(self):
        self.frames = 0
        self.reset_count = 0

    def feed(self, frame):
        self.frames += 1
        return (self.Event(),) if self.frames == 1 else ()

    def reset(self):
        self.reset_count += 1


class FakeEndpoint:
    def __init__(self):
        self.active = False
        self.wake_count = 0
        self.cancel_count = 0
        self.feed_count = 0
        self.pre_roll = []
        self.lost_count = 0

    def wake(self, event):
        self.active = True
        self.wake_count += 1
        self.wake_frame_sequences = tuple(item.sequence for item in self.pre_roll)
        return ()

    def feed(self, frame):
        if not self.active:
            self.pre_roll.append(frame)
            return ()
        self.feed_count += 1
        if self.feed_count == 1:
            prior = b"".join(item.data for item in self.pre_roll)
            segment = SpeechSegment(16000, "pcm_s16le", 0.0, 0.2, 1, frame.sequence,
                                    prior + frame.data)
            self.active = False
            return (EndpointEvent(EndpointKind.ENDPOINT, 0.1, segment),)
        return ()

    def cancel(self, timestamp=None):
        self.active = False
        self.cancel_count += 1

    def stream_lost(self, timestamp=None):
        self.active = False
        self.lost_count += 1
        self.pre_roll.clear()


class FakeASR:
    def __init__(self, text="turn on the lights", error=None, on_recognize=None):
        self.text = text
        self.error = error
        self.on_recognize = on_recognize
        self.frames = []

    def recognize(self, frame):
        self.frames.append(frame)
        if self.on_recognize:
            self.on_recognize()
        if self.error:
            raise self.error
        return type("Result", (), {"text": self.text})()


class FakeConversation:
    def __init__(self, error=None, answer="Lights are on"):
        self.error = error
        self.answer = answer
        self.calls = []

    def respond(self, text, session_id):
        self.calls.append((text, session_id))
        if self.error:
            raise self.error
        return self.answer


class FakePlayback:
    def __init__(self, statuses=("queued", "playing", "completed"), announce_error=None):
        self.statuses = list(statuses)
        self.announce_error = announce_error
        self.calls = []

    def announce(self, text):
        self.calls.append(("announce", text))
        if self.announce_error:
            raise self.announce_error
        return "task-1"

    def get_status(self, task_id):
        self.calls.append(("status", task_id))
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]


def frame(sequence):
    return MonoFrame(sequence / 10, sequence, 16000, "pcm_s16le", 2, b"\x01\x00\x02\x00")


class OrchestratorTests(unittest.TestCase):
    def build(self, *, asr=None, conversation=None, playback=None, **kwargs):
        self.wake = FakeWake()
        self.endpoint = FakeEndpoint()
        self.playback = playback or FakePlayback()
        sleep = kwargs.pop("sleep", lambda _: None)
        controller = Orchestrator(
            self.wake, self.endpoint, asr or FakeASR(),
            conversation or FakeConversation(), self.playback,
            config=OrchestratorConfig(playback_poll_seconds=0),
            sleep=sleep,
            **kwargs,
        )
        return controller

    def test_full_turn_states_and_bridge_completion(self):
        asr = FakeASR()
        controller = self.build(asr=asr)
        result = controller.run_once([frame(1), frame(2), frame(3)])
        self.assertEqual(result.status, TurnStatus.COMPLETED)
        self.assertEqual(result.transitions, (State.IDLE, State.LISTENING, State.PROCESSING,
                                              State.SPEAKING, State.IDLE))
        self.assertEqual(self.wake.frames, 1)  # source is not read for wake while speaking
        self.assertEqual(self.endpoint.wake_frame_sequences, (1,))
        self.assertEqual(self.endpoint.pre_roll, [frame(1)])
        self.assertEqual(self.endpoint.feed_count, 1)  # wake frame is not fed a second time
        self.assertEqual(asr.frames[0].sample_count, 4)
        self.assertEqual(asr.frames[0].data, frame(1).data + frame(2).data)
        self.assertEqual([call[0] for call in self.playback.calls],
                         ["announce", "status", "status", "status"])
        self.assertEqual(controller.state, State.IDLE)
        self.assertEqual(self.wake.reset_count, 1)

    def test_bridge_status_dict_matches_voice_bridge_client(self):
        class DictPlayback(FakePlayback):
            def get_status(self, task_id):
                self.calls.append(("status", task_id))
                return {"id": task_id, "status": "completed"}
        result = self.build(playback=DictPlayback()).run_once([frame(1), frame(2)])
        self.assertEqual(result.status, TurnStatus.COMPLETED)

    def test_empty_asr_stops_before_llm_and_playback(self):
        conversation = FakeConversation()
        playback = FakePlayback()
        result = self.build(asr=FakeASR(text=""), conversation=conversation,
                            playback=playback).run_once([frame(1), frame(2)])
        self.assertEqual(result.status, TurnStatus.EMPTY_INPUT)
        self.assertFalse(conversation.calls)
        self.assertFalse(playback.calls)

    def test_cancel_after_asr_prevents_llm_submission(self):
        event = threading.Event()
        conversation = FakeConversation()
        asr = FakeASR(on_recognize=event.set)
        result = self.build(asr=asr, conversation=conversation).run_once(
            [frame(1), frame(2)], cancel=event)
        self.assertEqual(result.status, TurnStatus.CANCELLED)
        self.assertEqual(result.transcript, "turn on the lights")
        self.assertFalse(conversation.calls)

    def test_each_external_failure_returns_to_idle(self):
        cases = (
            (FakeASR(error=RuntimeError("asr")), FakeConversation(), FakePlayback()),
            (FakeASR(), FakeConversation(error=RuntimeError("llm")), FakePlayback()),
            (FakeASR(), FakeConversation(), FakePlayback(announce_error=RuntimeError("announce"))),
            (FakeASR(), FakeConversation(), FakePlayback(statuses=("failed",))),
        )
        for asr, conversation, playback in cases:
            with self.subTest(asr=asr.error, llm=conversation.error,
                              playback=playback.announce_error, statuses=playback.statuses):
                controller = self.build(asr=asr, conversation=conversation, playback=playback)
                result = controller.run_once([frame(1), frame(2)])
                self.assertEqual(result.status, TurnStatus.FAILED)
                self.assertEqual(result.transitions[-2:], (State.FAILED, State.IDLE))
                self.assertEqual(controller.state, State.IDLE)
                self.assertEqual(self.wake.reset_count, 1)

    def test_playback_timeout_and_external_cancel_mark_bridge_uncertain(self):
        controller = self.build(playback=FakePlayback(statuses=("queued",)),
                                clock=lambda: 100,)
        controller.config = OrchestratorConfig(playback_timeout_seconds=0,
                                               playback_poll_seconds=0)
        timeout = controller.run_once([frame(1), frame(2)])
        self.assertEqual(timeout.status, TurnStatus.FAILED)
        self.assertTrue(timeout.bridge_may_still_play)
        self.assertEqual(controller.state, State.IDLE)

        event = threading.Event()
        playback = FakePlayback(statuses=("queued",))
        controller = self.build(playback=playback, sleep=lambda _: event.set())
        # Set cancellation when playback polling first returns pending.
        original_status = playback.get_status
        def cancel_after_status(task_id):
            value = original_status(task_id)
            event.set()
            return value
        playback.get_status = cancel_after_status
        result = controller.run_once([frame(1), frame(2)], cancel=event)
        self.assertEqual(result.status, TurnStatus.CANCELLED)
        self.assertTrue(result.bridge_may_still_play)
        self.assertEqual(controller.state, State.IDLE)

    def test_cancellation_before_command_clears_endpoint(self):
        event = threading.Event()
        event.set()
        controller = self.build()
        result = controller.run_once([frame(1)], cancel=event)
        self.assertEqual(result.status, TurnStatus.CANCELLED)
        self.assertEqual(controller.endpoint.cancel_count, 0)
        self.assertEqual(controller.state, State.IDLE)

    def test_stream_ending_after_wake_is_explicit_failure(self):
        controller = self.build()
        result = controller.run_once([frame(1)])
        self.assertEqual(result.status, TurnStatus.FAILED)
        self.assertIn("audio input ended", result.error)
        self.assertEqual(self.endpoint.lost_count, 1)
        self.assertEqual(controller.state, State.IDLE)

    def test_cancel_during_listening_clears_active_endpoint(self):
        event = threading.Event()
        controller = self.build()
        def after_wake():
            yield frame(1)
            event.set()
            yield frame(2)
        result = controller.run_once(after_wake(), cancel=event)
        self.assertEqual(result.status, TurnStatus.CANCELLED)
        self.assertEqual(controller.endpoint.cancel_count, 1)
        self.assertFalse(controller.endpoint.active)
        self.assertEqual(controller.state, State.IDLE)

    def test_no_wake_eof_clears_idle_preroll(self):
        class NoWake:
            def feed(self, frame):
                return ()
        controller = Orchestrator(NoWake(), FakeEndpoint(), FakeASR(),
                                  FakeConversation(), FakePlayback())
        result = controller.run_once([frame(1), frame(2)])
        self.assertEqual(result.status, TurnStatus.NO_WAKE)
        self.assertEqual(controller.endpoint.pre_roll, [])

    def test_cancel_clears_idle_preroll(self):
        class NoWake:
            def feed(self, frame):
                return ()
        endpoint = FakeEndpoint()
        endpoint.pre_roll.append(frame(0))
        event = threading.Event()
        event.set()
        controller = Orchestrator(NoWake(), endpoint, FakeASR(),
                                  FakeConversation(), FakePlayback())
        result = controller.run_once([frame(1)], cancel=event)
        self.assertEqual(result.status, TurnStatus.CANCELLED)
        self.assertEqual(endpoint.pre_roll, [])

    def test_listen_timeout_clears_idle_preroll(self):
        class NoWake:
            def feed(self, frame):
                return ()
        endpoint = FakeEndpoint()
        endpoint.pre_roll.append(frame(0))
        controller = Orchestrator(NoWake(), endpoint, FakeASR(),
                                  FakeConversation(), FakePlayback(),
                                  config=OrchestratorConfig(max_listen_seconds=0.5),
                                  clock=lambda: 1.0)
        result = controller.run_once([frame(1)])
        self.assertEqual(result.status, TurnStatus.NO_WAKE)
        self.assertEqual(endpoint.pre_roll, [])

    def test_listen_timeout_cancels_active_command(self):
        clock_value = [0.0]
        controller = Orchestrator(FakeWake(), FakeEndpoint(), FakeASR(),
                                  FakeConversation(), FakePlayback(),
                                  config=OrchestratorConfig(max_listen_seconds=0.5),
                                  clock=lambda: clock_value[0])
        def frames():
            yield frame(1)
            clock_value[0] = 1.0
            yield frame(2)
        result = controller.run_once(frames())
        self.assertEqual(result.status, TurnStatus.NO_SPEECH)
        self.assertEqual(controller.endpoint.cancel_count, 1)
        self.assertFalse(controller.endpoint.active)


if __name__ == "__main__":
    unittest.main()
