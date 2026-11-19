import unittest
from unittest.mock import patch

from astero.validation import AcousticLoopRunner


class FakeService:
    def __init__(self):
        self.reads = 0
        self.submitted = []

    def health(self):
        return {"ready": True, "dependencies": {"asr": "ready"}}

    def submit_voice_turn(self, session_id):
        self.submitted.append(session_id)
        return {"turn_id": "turn1", "trace_id": "trace1", "status": "queued"}

    def get_turn(self, turn_id):
        self.reads += 1
        if self.reads == 1:
            return {"status": "running", "events": [
                {"stage": "capture_ready", "event": "completed"},
            ]}
        return {
            "status": "completed",
            "events": [{"stage": "asr", "event": "completed", "elapsed_ms": 2.0}],
            "result": {"capture_id": "capture1", "archive_complete": True,
                       "frame_count": 10, "transcript": "What is two plus two?",
                       "reply": "Four.", "total_elapsed_ms": 12.0},
        }


class FakePlayback:
    def __init__(self):
        self.spoken = []
        self.styles = []

    def health(self):
        return {"tts_ready": True}

    def speak(self, text, **kwargs):
        self.spoken.append(text)
        self.styles.append(kwargs)
        return {"task_id": f"tts{len(self.spoken)}", "status": "completed"}


class AcousticLoopTests(unittest.TestCase):
    def test_runs_wake_then_command_and_reports_synthetic_input(self):
        service = FakeService()
        playback = FakePlayback()
        runner = AcousticLoopRunner(service, playback, sleep=lambda _: None)
        result = runner.run("What is two plus two?", session_id="acceptance")
        self.assertEqual(service.submitted, ["acceptance"])
        self.assertEqual(playback.spoken, ["Astero", "What is two plus two?"])
        self.assertEqual(playback.styles, [{"style": "Speak English clearly and slowly."}, {}])
        self.assertEqual(result["wake_phrase"], "Astero")
        self.assertEqual(result["wake_style"], "Speak English clearly and slowly.")
        self.assertEqual(result["input_kind"], "synthetic_tts")
        self.assertTrue(result["capture_ready_seen"])
        self.assertTrue(result["archive_complete"])
        self.assertTrue(result["completed"])
        self.assertEqual(result["transcript"], "What is two plus two?")

    def test_rejects_diagnostic_completion_without_complete_archive(self):
        service = FakeService()
        service.reads = 0
        def get_turn(turn_id):
            service.reads += 1
            if service.reads == 1:
                return {"status": "running", "events": [
                    {"stage": "capture_ready", "event": "completed"},
                ]}
            return {"status": "completed", "events": [],
                    "result": {"diagnostic_only": True, "archive_complete": False}}
        service.get_turn = get_turn
        runner = AcousticLoopRunner(service, FakePlayback(), sleep=lambda _: None)
        result = runner.run("hello")
        self.assertFalse(result["completed"])
        self.assertFalse(result["archive_complete"])

    def test_empty_wake_style_is_preserved_and_command_style_is_untouched(self):
        playback = FakePlayback()
        result = AcousticLoopRunner(FakeService(), playback, sleep=lambda _: None).run(
            "hello", wake_phrase="Astero", wake_style="",
        )
        self.assertEqual(playback.styles, [{"style": ""}, {}])
        self.assertEqual(result["wake_style"], "")

    def test_cli_passes_wake_style_without_affecting_command(self):
        from astero.validation.__main__ import main
        with patch('astero.validation.__main__.run_acoustic_loop',
                   return_value={"completed": True}) as run, patch('builtins.print'):
            self.assertEqual(main(["--text", "hello", "--wake-style", ""]), 0)
        self.assertEqual(run.call_args.args[2], "hello")
        self.assertEqual(run.call_args.kwargs["wake_style"], "")

    def test_service_must_be_ready_before_speaking(self):
        service = FakeService()
        service.health = lambda: {"ready": False}
        playback = FakePlayback()
        runner = AcousticLoopRunner(service, playback, sleep=lambda _: None)
        with self.assertRaises(RuntimeError):
            runner.run("hello")
        self.assertEqual(playback.spoken, [])


if __name__ == "__main__":
    unittest.main()
