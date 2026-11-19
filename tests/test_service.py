import json
import tempfile
import time
import unittest
from pathlib import Path
from urllib.request import Request, urlopen

from astero.persistence import AsteroStore
from astero.observability import TraceRecorder
from astero.service import AsteroService, LocalAsteroHTTPServer


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = AsteroStore(Path(self.temp.name) / "astero.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_health_and_readiness_are_distinct(self):
        app = AsteroService(self.store, text_executor=lambda request: {"status": "completed"},
                            readiness=lambda: {"ready": False, "dependencies": {"asr": "missing"}})
        result = app.health()
        self.assertEqual(result["status"], "degraded")
        self.assertFalse(result["ready"])
        app.close()

    def test_text_turn_is_marked_diagnostic_and_persisted(self):
        def sink(event):
            self.store.append_stage_event(
                event.event_id, event.trace_id, event.turn_id, event.session_id,
                event.stage, event.event, event.timestamp_utc, event.elapsed_ms,
                event.status, event.attempt, event.attributes,
                monotonic_ns=event.monotonic_ns,
            )
        app = AsteroService(
            self.store,
            text_executor=lambda request: {"status": "completed", "reply": "ok",
                                           "elapsed_ms": 3.0},
            trace_recorder=TraceRecorder(sink=sink),
            workers=1,
        )
        queued = app.submit_turn(session_id="session1", mode="text", text="hello",
                                 turn_id="turn1", trace_id="trace1")
        self.assertEqual(queued["status"], "queued")
        self.assertTrue(queued["diagnostic_only"])
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            row = self.store.get_turn("turn1")
            if row.status in {"completed", "failed"}:
                break
            time.sleep(0.01)
        self.assertEqual(row.status, "completed")
        result = app.get_turn("turn1")
        self.assertEqual(result["result"]["reply"], "ok")
        self.assertGreaterEqual(result["trace_summary"]["total_elapsed_ms"], 0)
        self.assertEqual({event["trace_id"] for event in result["events"]}, {"trace1"})
        app.close()

    def test_rejects_remote_bind_and_text_in_voice_mode(self):
        app = AsteroService(self.store, voice_executor=lambda request: {"status": "no_wake"})
        with self.assertRaises(ValueError):
            LocalAsteroHTTPServer(app, host="0.0.0.0")
        with self.assertRaises(ValueError):
            app.submit_turn(session_id="s", mode="voice", text="injected")
        app.close()

    def test_loopback_http_api_and_safe_request_logging(self):
        logged = []
        app = AsteroService(
            self.store,
            text_executor=lambda request: {"status": "completed", "reply": "ok"},
            readiness=lambda: {"ready": True, "dependencies": {"archive": "ready"}},
            workers=1,
        )
        server = LocalAsteroHTTPServer(
            app, port=0,
            request_logger=lambda method, route, status, elapsed: logged.append(
                (method, route, status, elapsed)
            ),
        )
        server.start()
        base = f"http://127.0.0.1:{server.address[1]}"
        try:
            with urlopen(base + "/health", timeout=2) as response:
                self.assertEqual(json.loads(response.read())["ready"], True)
            request = Request(
                base + "/v1/turns",
                data=json.dumps({"session_id": "s", "mode": "text", "text": "diagnostic"}).encode(),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urlopen(request, timeout=2) as response:
                queued = json.loads(response.read())
                self.assertEqual(response.status, 202)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                result = app.get_turn(queued["turn_id"])
                if result["status"] in {"completed", "failed"}:
                    break
                time.sleep(0.01)
            with urlopen(base + "/v1/turns/" + queued["turn_id"], timeout=2) as response:
                self.assertEqual(json.loads(response.read())["status"], "completed")
            self.assertTrue(all(entry[3] >= 0 for entry in logged))
            self.assertNotIn("diagnostic", repr(logged))
        finally:
            server.close()
            app.close()


if __name__ == "__main__":
    unittest.main()
