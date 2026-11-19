import io
import json
import logging
import threading
import unittest
from datetime import datetime, timezone

from astero.observability import (
    EventRecorderError,
    TraceContext,
    TraceRecorder,
    TraceSummaryBuilder,
    event_to_json,
)


class FakeClock:
    def __init__(self, value=0):
        self.value = value

    def __call__(self):
        return self.value

    def advance_ms(self, milliseconds):
        self.value += int(milliseconds * 1_000_000)


class ObservabilityTests(unittest.TestCase):
    def make_recorder(self, clock, sink=None):
        output = io.StringIO()
        logger = logging.Logger(f"test-observability-{id(output)}", level=logging.INFO)
        logger.addHandler(logging.StreamHandler(output))
        recorder = TraceRecorder(
            sink,
            logger=logger,
            monotonic_ns=clock,
            utc_clock=lambda: datetime(2026, 11, 18, tzinfo=timezone.utc),
        )
        return recorder, output

    def test_trace_ids_persist_across_stages_and_fake_clock_is_exact(self):
        clock = FakeClock(100)
        events = []
        recorder, _ = self.make_recorder(clock, events.append)
        context = TraceContext.create("session-7", trace_id="trace-7", turn_id="turn-7")

        with recorder.stage(context, "asr", attributes={"model": "sensevoice"}):
            clock.advance_ms(125.5)

        self.assertEqual([event.event for event in events], ["started", "completed"])
        self.assertEqual({event.trace_id for event in events}, {"trace-7"})
        self.assertEqual({event.turn_id for event in events}, {"turn-7"})
        self.assertEqual({event.session_id for event in events}, {"session-7"})
        self.assertEqual(events[0].monotonic_ns, 100)
        self.assertEqual(events[1].elapsed_ms, 125.5)
        self.assertEqual(events[1].attributes, {"model": "sensevoice"})

    def test_failed_retry_skipped_and_missing_stages_are_explicit(self):
        clock = FakeClock()
        events = []
        recorder, _ = self.make_recorder(clock, events.append)
        context = TraceContext.create("s", trace_id="trace", turn_id="turn")

        with self.assertRaises(RuntimeError):
            with recorder.stage(context, "llm", attempt=1):
                clock.advance_ms(40)
                raise RuntimeError("private prompt text")

        with recorder.stage(context, "llm", attempt=2):
            clock.advance_ms(15)
        with recorder.stage(context, "tts_enqueue") as stage:
            clock.advance_ms(3)
            stage.skip("not_configured")

        self.assertEqual(events[1].event, "failed")
        self.assertEqual(events[1].error_code, "RuntimeError")
        self.assertEqual(events[3].attempt, 2)
        self.assertEqual(events[-1].event, "skipped")
        summary = TraceSummaryBuilder("trace")
        for event in events:
            summary.observe(event)
        result = summary.build()
        self.assertEqual(result.llm_ms, 55)
        self.assertIsNone(result.asr_ms)
        self.assertIsNone(result.tts_enqueue_to_completed_ms)

    def test_capture_to_wake_speech_end_and_service_total_summary(self):
        clock = FakeClock()
        events = []
        recorder, _ = self.make_recorder(clock, events.append)
        context = TraceContext.create("s", trace_id="t", turn_id="u", capture_id="c")

        with recorder.stage(context, "capture"):
            clock.advance_ms(1000)
        with recorder.stage(context, "service_request"):
            clock.advance_ms(10)
        with recorder.stage(context, "wake_detection"):
            clock.advance_ms(50)
        with recorder.stage(context, "endpoint"):
            clock.advance_ms(400)
        with recorder.stage(context, "asr"):
            clock.advance_ms(120)
        with recorder.stage(context, "llm"):
            clock.advance_ms(300)
        with recorder.stage(context, "tts_enqueue"):
            clock.advance_ms(10)
        with recorder.stage(context, "playback"):
            clock.advance_ms(900)
        with recorder.stage(context, "service_response"):
            clock.advance_ms(1)

        summary = TraceSummaryBuilder("t")
        for event in events:
            summary.observe(event)
        result = summary.build()
        self.assertEqual(result.capture_to_wake_ms, 1060)
        self.assertEqual(result.wake_to_speech_end_ms, 400)
        self.assertEqual(result.asr_ms, 120)
        self.assertEqual(result.llm_ms, 300)
        self.assertEqual(result.tts_enqueue_to_completed_ms, 900)
        # service_request begins after long-running capture has already opened.
        self.assertEqual(result.total_elapsed_ms, 1791)

    def test_structured_json_redacts_private_text_and_credentials(self):
        clock = FakeClock(42)
        recorder, output = self.make_recorder(clock)
        context = TraceContext.create("session", trace_id="trace", turn_id="turn")
        event = recorder.emit(
            context,
            "llm_request",
            "completed",
            status="completed",
            attributes={
                "attempt_no": 2,
                "transcript": "what is my password",
                "diagnostic": "private spoken words here",
                "credential_hint": "should-not-appear",
                "api_key": "sk-test-secret",
                "mode": "voice",
            },
        )

        line = output.getvalue().strip()
        payload = json.loads(line)
        self.assertEqual(payload["event"], "completed")
        self.assertEqual(payload["attributes"], {"attempt_no": 2, "mode": "voice"})
        self.assertEqual(event_to_json(event), line)
        for private in ("password", "private spoken words", "sk-test-secret", "should-not-appear"):
            self.assertNotIn(private, line)

    def test_sink_error_is_explicit_and_preserves_original_cause(self):
        clock = FakeClock()
        cause = OSError("database is locked")

        def failing_sink(_event):
            raise cause

        recorder, output = self.make_recorder(clock, failing_sink)
        context = TraceContext.create("s", trace_id="stable", turn_id="turn")
        with self.assertRaises(EventRecorderError) as caught:
            recorder.emit(context, "asr", "started")
        self.assertIs(caught.exception.original_cause, cause)
        self.assertEqual(caught.exception.event.trace_id, "stable")
        # The structured logger still records what failed to persist.
        self.assertIn('"trace_id":"stable"', output.getvalue())

    def test_concurrent_sessions_keep_correlation_ids_separate(self):
        lock = threading.Lock()
        events = []

        def sink(event):
            with lock:
                events.append(event)

        recorder, _ = self.make_recorder(FakeClock(), sink)
        contexts = [
            TraceContext.create("session-a", trace_id="trace-a", turn_id="turn-a"),
            TraceContext.create("session-b", trace_id="trace-b", turn_id="turn-b"),
        ]

        def emit_turn(context):
            for _ in range(10):
                recorder.emit(context, "capture_frame_batch", "completed", attributes={"frame_count": 160})

        threads = [threading.Thread(target=emit_turn, args=(context,)) for context in contexts]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(events), 20)
        for event in events:
            expected = "a" if event.session_id == "session-a" else "b"
            self.assertEqual(event.trace_id, f"trace-{expected}")
            self.assertEqual(event.turn_id, f"turn-{expected}")


if __name__ == "__main__":
    unittest.main()
