import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest

from astero.speech.voice_bridge import (
    VoiceBridgeClient,
    VoiceBridgeConfig,
    VoiceBridgeResponseError,
    VoiceBridgeTaskError,
    VoiceBridgeTimeoutError,
    VoiceBridgeUnavailableError,
)


class FakeBridgeHandler(BaseHTTPRequestHandler):
    post_code = 202
    post_body = {"id": "task-1", "status": "queued"}
    states = ["completed"]
    requests_seen = []

    def log_message(self, *_args):
        pass

    def send_json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length))
        self.requests_seen.append((self.path, payload))
        if self.post_code == 202:
            if self.post_body == "invalid-json":
                body = b"not-json"
                self.send_response(202)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            return self.send_json(self.post_code, self.post_body)
        self.send_json(self.post_code, {"error": "unavailable"})

    def do_GET(self):
        self.requests_seen.append((self.path, None))
        if self.path == "/health":
            return self.send_json(200, {"status": "ok", "tts_ready": True})
        if self.path != "/announcements/task-1":
            return self.send_json(404, {"error": "missing"})
        if isinstance(self.states, Exception):
            return self.send_json(503, {"error": "offline"})
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        if state == "malformed":
            return self.send_json(200, {"id": "task-1"})
        result = {"id": "task-1", "status": state}
        if state == "failed":
            result["error"] = "player failed"
        return self.send_json(200, result)


class VoiceBridgeClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeBridgeHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        FakeBridgeHandler.post_code = 202
        FakeBridgeHandler.post_body = {"id": "task-1", "status": "queued"}
        FakeBridgeHandler.states = ["completed"]
        FakeBridgeHandler.requests_seen = []
        self.client = VoiceBridgeClient(VoiceBridgeConfig(
            base_url=self.base_url, timeout_seconds=0.05,
            poll_interval_seconds=0.005, request_timeout_seconds=1,
        ))

    def test_202_queued_is_polled_until_completed(self):
        FakeBridgeHandler.states = ["queued", "generating", "playing", "completed"]
        result = self.client.speak("请回答。", voice="vivian", style="calm")
        self.assertEqual((result.task_id, result.status), ("task-1", "completed"))
        self.assertEqual(FakeBridgeHandler.requests_seen[0], (
            "/announce", {"text": "请回答。", "voice": "vivian", "style": "calm"}))
        self.assertEqual(len(FakeBridgeHandler.requests_seen), 5)

    def test_failed_task_is_not_reported_as_success(self):
        FakeBridgeHandler.states = ["failed"]
        with self.assertRaises(VoiceBridgeTaskError) as caught:
            self.client.speak("private answer")
        self.assertEqual(caught.exception.task_id, "task-1")
        self.assertNotIn("private answer", str(caught.exception))

    def test_non_202_is_explicit_error(self):
        FakeBridgeHandler.post_code = 503
        with self.assertRaisesRegex(VoiceBridgeResponseError, "HTTP 503"):
            self.client.speak("hello")

    def test_invalid_json_and_invalid_queue_response_are_rejected(self):
        FakeBridgeHandler.post_body = "invalid-json"
        with self.assertRaisesRegex(VoiceBridgeResponseError, "invalid JSON"):
            self.client.speak("hello")
        FakeBridgeHandler.post_body = {"status": "queued"}
        with self.assertRaisesRegex(VoiceBridgeResponseError, "invalid queue response"):
            self.client.speak("hello")
        FakeBridgeHandler.post_body = {"id": "bad%2Ftask", "status": "queued"}
        with self.assertRaisesRegex(VoiceBridgeResponseError, "invalid queue response"):
            self.client.speak("hello")

    def test_task_id_rejects_url_delimiters_and_encoded_paths(self):
        for task_id in ("a/b", "a?b", "a#b", r"a\b", "a%2Fb", "a b"):
            with self.subTest(task_id=task_id), self.assertRaises(ValueError):
                self.client.get_status(task_id)

    def test_unknown_status_response_is_rejected(self):
        FakeBridgeHandler.states = ["malformed"]
        with self.assertRaisesRegex(VoiceBridgeResponseError, "unknown task status"):
            self.client.speak("hello")

    def test_timeout_explains_that_bridge_job_may_continue(self):
        FakeBridgeHandler.states = ["playing"]
        with self.assertRaises(VoiceBridgeTimeoutError) as caught:
            self.client.speak("hello", timeout_seconds=0.015)
        self.assertEqual(caught.exception.task_id, "task-1")
        self.assertIn("may still be queued or playing", str(caught.exception))

    def test_bridge_interrupt_during_poll_is_reported(self):
        FakeBridgeHandler.states = RuntimeError("offline")
        with self.assertRaises(VoiceBridgeUnavailableError) as caught:
            self.client.speak("hello")
        self.assertIn("task-1", str(caught.exception))
        self.assertIn("may still be queued or playing", str(caught.exception))

    def test_health_requires_tts_ready(self):
        self.assertEqual(self.client.health(), {"status": "ok", "tts_ready": True})


if __name__ == "__main__":
    unittest.main()
