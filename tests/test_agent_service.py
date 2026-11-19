import tempfile
import unittest
from pathlib import Path

from astero.agent.service import AgentService
from astero.observability import TraceRecorder
from astero.persistence import AsteroStore


class Provider:
    def __init__(self, reply="answer"):
        self.reply = reply
        self.requests = []

    def respond(self, text, session_id):
        self.requests.append((text, session_id))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class AgentServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = AsteroStore(Path(self.temp.name) / "astero.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def _service(self, provider):
        archived = []

        def sink(event):
            self.store.append_stage_event(
                event.event_id, event.trace_id, event.turn_id, event.session_id,
                event.stage, event.event, event.timestamp_utc, event.elapsed_ms,
                event.status, event.attempt, event.attributes,
                monotonic_ns=event.monotonic_ns,
            )

        return AgentService(
            self.store, provider, trace_recorder=TraceRecorder(sink=sink),
            archive_payload=lambda capture, turn, kind, payload: archived.append(
                (capture, turn, kind, payload)
            ),
        ), archived

    def test_persistent_context_knowledge_sources_and_trace(self):
        self.store.create_session("s1")
        self.store.add_memory("User prefers concise replies", "explicit user request",
                              scope="user")
        knowledge = self.store.add_knowledge(
            "Astero manual says use the blue indicator", "manual.md"
        )
        provider = Provider("The manual says blue. [" + knowledge.document_id + "]")
        service, archived = self._service(provider)

        result = service.submit_turn(
            "What does the Astero manual say?", "s1", turn_id="turn1", trace_id="trace1",
            capture_id="capture1",
        )

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.knowledge_sources, (knowledge.document_id,))
        self.assertIn("User prefers concise replies", provider.requests[0][0])
        self.assertIn("Astero manual says use the blue indicator", provider.requests[0][0])
        self.assertEqual([row[2] for row in archived], ["agent_request", "agent_reply"])
        self.assertEqual(self.store.get_turn("turn1").status, "completed")
        self.assertEqual([row.event for row in self.store.list_stage_events("turn1")],
                         ["started", "completed"] * 5)
        self.assertEqual(len(self.store.list_memories(session_id="s1", scope="user")), 1)

    def test_conversation_does_not_automatically_become_memory(self):
        service, _ = self._service(Provider("ok"))
        result = service.submit_turn("Remember my favorite color is green", "s1",
                                     turn_id="turn2", trace_id="trace2")
        self.assertEqual(result.status, "completed")
        self.assertEqual(self.store.list_memories(session_id="s1", scope="session"), ())
        self.assertEqual(len(self.store.get_conversation_context("s1")), 2)

    def test_explicit_memory_write_and_scoped_delete(self):
        service, _ = self._service(Provider())
        memory = service.remember("s1", "Prefers Mandarin", source="explicit request")
        self.assertEqual(memory.scope, "session")
        self.assertTrue(service.forget(memory.memory_id, "s1"))
        self.assertFalse(service.forget(memory.memory_id, "another-session"))

    def test_provider_failure_is_durable_and_never_claims_success(self):
        service, archived = self._service(Provider(RuntimeError("provider offline")))
        result = service.submit_turn("hello", "s1", turn_id="turn3", trace_id="trace3")
        self.assertEqual(result.status, "failed")
        self.assertIsNone(result.reply)
        self.assertEqual(self.store.get_turn("turn3").status, "failed")
        self.assertEqual(self.store.get_turn("turn3").error_code, "runtime_error")
        self.assertEqual([row.event for row in self.store.list_stage_events("turn3")],
                         ["started", "completed", "started", "completed",
                          "started", "failed"])
        self.assertEqual([row[2] for row in archived], ["agent_request"])

    def test_invalid_turn_input_fails_before_provider(self):
        provider = Provider()
        service, _ = self._service(provider)
        with self.assertRaises(ValueError):
            service.submit_turn("  ", "s1")
        self.assertEqual(provider.requests, [])


if __name__ == "__main__":
    unittest.main()
