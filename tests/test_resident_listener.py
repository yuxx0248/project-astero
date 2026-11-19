import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from astero.persistence import AsteroStore
from astero.service.app import AsteroService
from astero.service.listener import ResidentListener
from astero.service.local_voice import LocalVoiceTurnExecutor


def wait_until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition not reached")


class FakeVoice:
    def __init__(self):
        self.stopped = threading.Event()

    def request_stop(self):
        self.stopped.set()

    def activity(self):
        return {"state": "idle", "listening": False, "active_turn_id": None}


class ListenerTests(unittest.TestCase):
    def test_rearms_serially_after_success_and_no_wake_and_stops(self):
        with tempfile.TemporaryDirectory() as temp:
            store = AsteroStore(Path(temp) / "store.db")
            voice = FakeVoice()
            calls = []
            def execute(request):
                calls.append(request.session_id)
                if len(calls) == 1:
                    return {"status": "completed"}
                if len(calls) == 2:
                    return {"status": "no_wake"}
                voice.stopped.wait(2)
                return {"status": "cancelled"}
            app = AsteroService(store, voice_executor=execute, workers=1)
            listener = ResidentListener(app, voice, enabled=True, poll_seconds=0.005)
            listener.start()
            try:
                wait_until(lambda: len(calls) == 3)
                self.assertEqual(set(calls), {"local-voice"})
            finally:
                listener.stop()
                app.close()
            self.assertFalse(listener._thread.is_alive())
            self.assertEqual(len(calls), 3)
            self.assertEqual(listener.status()["state"], "stopped")

    def test_failure_backoff_is_interruptible(self):
        voice = FakeVoice()
        calls = []
        class FailedApp:
            def submit_turn(self, **kwargs):
                calls.append(1)
                raise RuntimeError("dependency unavailable")
        listener = ResidentListener(FailedApp(), voice, enabled=True, retry_seconds=30)
        listener.start()
        wait_until(lambda: listener.status()["state"] == "retrying")
        started = time.monotonic()
        listener.stop()
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(len(calls), 1)

    def test_disabled_does_not_start_and_health_uses_actual_wake_state(self):
        voice = LocalVoiceTurnExecutor.__new__(LocalVoiceTurnExecutor)
        voice._activity = {"state": "starting_capture", "listening": False, "active_turn_id": "turn"}
        listener = ResidentListener(None, voice)
        listener.start()
        self.assertIsNone(listener._thread)
        self.assertFalse(listener.status()["listening"])
        voice._observe(SimpleNamespace(stage="wake_detection", event="started", status="running"))
        self.assertTrue(listener.status()["listening"])
        voice._observe(SimpleNamespace(stage="wake_detection", event="completed", status="detected"))
        self.assertFalse(listener.status()["listening"])
        self.assertEqual(listener.status()["state"], "capturing_command")

    def test_already_completed_future_callback_does_not_deadlock(self):
        from concurrent.futures import Future
        with tempfile.TemporaryDirectory() as temp:
            store = AsteroStore(Path(temp) / "store.db")
            app = AsteroService(store, voice_executor=lambda r: {"status": "no_wake"})
            app._executor.shutdown()
            class ImmediateExecutor:
                def submit(self, *args):
                    future = Future()
                    future.set_result(None)
                    return future
                def shutdown(self, **kwargs):
                    pass
            app._executor = ImmediateExecutor()
            done = threading.Event()
            thread = threading.Thread(target=lambda: (app.submit_turn(session_id="s"), done.set()), daemon=True)
            thread.start()
            self.assertTrue(done.wait(1), "completed future callback deadlocked submit_turn")
            app.close()
