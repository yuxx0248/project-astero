from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from astero.persistence import (
    AsteroStore,
    StoreConfigurationError,
    StoreCorruptError,
    StoreNotFoundError,
    StoreSchemaError,
    StoreStateError,
)


class AsteroStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "data" / "astero.sqlite3"
        self.store = AsteroStore(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def test_schema_migrates_idempotently_and_database_is_private(self):
        self.assertEqual(self.store.schema_version, 3)
        self.assertEqual(AsteroStore(self.db_path).schema_version, 3)
        self.assertTrue(self.db_path.exists())
        if os.name == "posix":
            self.assertEqual(self.db_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.db_path.parent.stat().st_mode & 0o777, 0o700)

    def test_session_turn_events_and_archive_survive_reopen(self):
        session = self.store.create_session("s-1", {"origin": "voice"})
        self.assertEqual(session.metadata, {"origin": "voice"})
        turn = self.store.create_turn("t-1", "trace-1", "s-1")
        self.assertEqual(turn.status, "queued")
        self.store.update_turn("t-1", "running")
        self.store.append_stage_event(
            "e-1", "trace-1", "t-1", "s-1", "asr", "completed",
            elapsed_ms=42.5, status="ok", attempt=1,
            monotonic_ns=123456789,
            attributes={"language": "en", "transcript": "private text", "api_key": "secret"},
        )
        self.store.put_archive_index(
            "cap-1", "/tmp/astero/cap-1/manifest.json", "2026-11-18T01:00:00+00:00",
            turn_id="t-1", ended_at_utc="2026-11-18T01:00:01+00:00", frame_count=12,
            complete=True,
        )
        self.store.update_turn("t-1", "completed", result={"reply": "hello"})

        store = AsteroStore(self.db_path)
        self.assertEqual(store.get_turn("t-1").status, "completed")
        self.assertEqual(store.list_unfinished_turns(), ())
        self.assertEqual(store.list_stage_events("t-1")[0].sequence, 1)
        self.assertEqual(store.list_stage_events("t-1")[0].monotonic_ns, 123456789)
        attrs = store.list_stage_events("t-1")[0].attributes
        self.assertEqual(attrs["transcript"], "[REDACTED]")
        self.assertEqual(attrs["api_key"], "[REDACTED]")
        archive = store.get_archive_index("cap-1")
        self.assertTrue(archive.complete)
        self.assertEqual(archive.frame_count, 12)

    def test_scope_isolation_search_and_explicit_deletion(self):
        self.store.create_session("s-1")
        self.store.create_session("s-2")
        self.store.add_memory("prefers quiet replies", "user asked to remember", scope="user", memory_id="m-global")
        self.store.add_memory("uses a blue microphone", "voice setup", session_id="s-1", memory_id="m-s1")
        self.store.add_memory("uses a red microphone", "voice setup", session_id="s-2", memory_id="m-s2")
        s1 = self.store.search_memories("microphone", session_id="s-1")
        self.assertEqual({item.memory_id for item in s1}, {"m-s1"})
        s2 = self.store.search_memories("microphone", session_id="s-2")
        self.assertEqual({item.memory_id for item in s2}, {"m-s2"})
        self.assertEqual(self.store.search_memories("quiet", session_id="s-1")[0].memory_id, "m-global")
        self.assertFalse(self.store.delete_memory("m-s2", session_id="s-1"))
        self.assertTrue(self.store.delete_memory("m-s1", session_id="s-1"))
        self.assertFalse(any(item.memory_id == "m-s1" for item in self.store.search_memories("blue", "s-1")))

    def test_knowledge_keeps_source_and_session_scope(self):
        self.store.create_session("s-1")
        self.store.create_session("s-2")
        self.store.add_knowledge(
            "Voice Bridge reports completed after playback exits.",
            "docs/voice-bridge.md#status", document_id="k-global",
            metadata={"format": "markdown"},
        )
        self.store.add_knowledge(
            "Channel three works for this room.", "local-calibration-2026-11-18",
            document_id="k-s1", scope="session", session_id="s-1",
        )
        matches = self.store.search_knowledge("Channel three", session_id="s-1")
        self.assertEqual([item.document_id for item in matches], ["k-s1"])
        self.assertEqual(matches[0].source, "local-calibration-2026-11-18")
        self.assertEqual(self.store.search_knowledge("Channel three", session_id="s-2"), ())
        self.assertTrue(self.store.delete_knowledge("k-s1", session_id="s-1"))
        self.assertEqual(self.store.search_knowledge("Channel three", session_id="s-1"), ())

    def test_conversation_context_is_ordered_and_session_scoped(self):
        self.store.create_session("s-1")
        self.store.create_session("s-2")
        self.store.append_conversation_turn("s-1", None, "user", "question", source="asr")
        self.store.append_conversation_turn("s-1", None, "assistant", "answer", source="llm")
        self.store.append_conversation_turn("s-2", None, "user", "other")
        context = self.store.get_conversation_context("s-1")
        self.assertEqual([item.content for item in context], ["question", "answer"])
        self.assertEqual(self.store.get_conversation_context("s-2")[0].content, "other")

    def test_terminal_turn_cannot_be_reopened_and_invalid_write_rolls_back(self):
        self.store.create_session("s-1")
        self.store.create_turn("t-1", "trace-1", "s-1")
        self.store.update_turn("t-1", "failed", error_code="llm_timeout")
        with self.assertRaises(StoreStateError):
            self.store.update_turn("t-1", "running")
        with self.assertRaises(StoreNotFoundError):
            self.store.add_memory("orphan", "test", session_id="missing")
        self.assertEqual(self.store.list_memories(), ())

    def test_unfinished_turn_is_recoverable_after_restart(self):
        self.store.create_session("s-1")
        self.store.create_turn("t-q", "trace-q", "s-1", "queued")
        self.store.create_turn("t-r", "trace-r", "s-1", "running")
        self.store.create_turn("t-d", "trace-d", "s-1", "completed")
        reopened = AsteroStore(self.db_path)
        self.assertEqual({item.turn_id for item in reopened.list_unfinished_turns()}, {"t-q", "t-r"})

    def test_concurrent_session_writes_are_transaction_safe(self):
        for session_id in ("s-1", "s-2", "s-3", "s-4"):
            self.store.create_session(session_id)

        def add(index: int):
            session_id = f"s-{index % 4 + 1}"
            return self.store.add_memory(f"fact number {index}", "parallel test", session_id=session_id)

        with ThreadPoolExecutor(max_workers=8) as pool:
            inserted = list(pool.map(add, range(64)))
        self.assertEqual(len({item.memory_id for item in inserted}), 64)
        self.assertEqual(sum(len(self.store.list_memories(f"s-{n}")) for n in range(1, 5)), 64)

    def test_invalid_path_corrupt_db_and_newer_schema_fail_explicitly(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(StoreConfigurationError):
                AsteroStore(Path(directory))

            corrupt = Path(directory) / "corrupt.sqlite3"
            corrupt.write_bytes(b"not sqlite data")
            with self.assertRaises(StoreCorruptError):
                AsteroStore(corrupt)

            newer = Path(directory) / "newer.sqlite3"
            connection = sqlite3.connect(newer)
            connection.execute("PRAGMA user_version = 99")
            connection.close()
            with self.assertRaises(StoreSchemaError):
                AsteroStore(newer)

    def test_stage_event_trace_and_scope_must_match_turn(self):
        self.store.create_session("s-1")
        self.store.create_session("s-2")
        self.store.create_turn("t-1", "trace-1", "s-1")
        with self.assertRaises(StoreStateError):
            self.store.append_stage_event("e-1", "wrong-trace", "t-1", "s-1", "asr", "started")
        with self.assertRaises(StoreStateError):
            self.store.append_stage_event("e-2", "trace-1", "t-1", "s-2", "asr", "started")
        with self.assertRaises(StoreStateError):
            self.store.append_stage_event("e-3", "trace-1", "t-1", "s-1", "asr", "started", elapsed_ms=-1)


if __name__ == "__main__":
    unittest.main()
