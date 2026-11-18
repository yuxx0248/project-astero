from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
import unittest

from astero.agent.conversation import (
    BackendRequestError,
    BackendTimeoutError,
    EmptyResponseError,
    MockConversationBackend,
    OpenAICompatibleBackend,
    OpenAICompatibleConfig,
)


class _BackendHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []
    lock = threading.Lock()
    response_text = "answer"
    status = 200
    delay = 0.0

    def do_POST(self) -> None:
        content = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        with self.lock:
            type(self).requests.append({
                "body": json.loads(content),
                "authorization": self.headers.get("Authorization"),
            })
        if self.delay:
            time.sleep(self.delay)
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        if self.status == 200:
            body = json.dumps({"choices": [{"message": {"content": self.response_text}}]}).encode()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass

    def log_message(self, *_args) -> None:
        pass


class ConversationBackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _BackendHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.endpoint = f"http://127.0.0.1:{cls.server.server_port}/v1/chat/completions"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self) -> None:
        _BackendHandler.requests = []
        _BackendHandler.response_text = "answer"
        _BackendHandler.status = 200
        _BackendHandler.delay = 0.0

    def backend(self, timeout: float = 1.0) -> OpenAICompatibleBackend:
        return OpenAICompatibleBackend(OpenAICompatibleConfig(
            endpoint=self.endpoint,
            model="test-model",
            api_key="secret-test-key",
            timeout_seconds=timeout,
            system_prompt="Be helpful.",
        ))

    def test_sends_transcript_session_and_prior_context(self) -> None:
        backend = self.backend()
        self.assertEqual(backend.respond(" first ", "room-a"), "answer")
        self.assertEqual(backend.respond("second", "room-a"), "answer")
        self.assertEqual(backend.respond("other", "room-b"), "answer")
        calls = _BackendHandler.requests
        self.assertEqual(calls[0]["authorization"], "Bearer secret-test-key")
        self.assertEqual(calls[0]["body"]["messages"][-1], {"role": "user", "content": "first"})
        self.assertEqual(calls[1]["body"]["metadata"]["session_id"], "room-a")
        self.assertEqual(calls[1]["body"]["messages"][-3:], [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "second"},
        ])
        self.assertEqual(calls[2]["body"]["messages"], [
            {"role": "system", "content": "Be helpful."},
            {"role": "user", "content": "other"},
        ])

    def test_empty_failure_and_timeout_are_explicit(self) -> None:
        backend = self.backend()
        _BackendHandler.response_text = "  "
        with self.assertRaises(EmptyResponseError):
            backend.respond("question", "session")
        _BackendHandler.status = 503
        with self.assertRaisesRegex(BackendRequestError, "HTTP 503"):
            backend.respond("question", "session")
        _BackendHandler.status = 200
        _BackendHandler.delay = 0.2
        with self.assertRaises(BackendTimeoutError):
            self.backend(timeout=0.03).respond("question", "session")

    def test_concurrent_sessions_keep_their_context_separate(self) -> None:
        backend = self.backend()
        errors: list[BaseException] = []

        def call(text: str, session: str) -> None:
            try:
                backend.respond(text, session)
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=call, args=("alpha", "a")),
                   threading.Thread(target=call, args=("beta", "b"))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        requests = _BackendHandler.requests
        self.assertEqual({request["body"]["metadata"]["session_id"] for request in requests}, {"a", "b"})
        for request in requests:
            self.assertEqual(len(request["body"]["messages"]), 2)

    def test_mock_backend_is_local_and_explicitly_substitute(self) -> None:
        mock = MockConversationBackend()
        self.assertEqual(mock.respond("hello", "test-1"), "[mock:test-1] hello")
        with self.assertRaises(ValueError):
            mock.respond(" ", "test-1")


if __name__ == "__main__":
    unittest.main()
