"""Local SQLite persistence for sessions, turns, traces, memories, and knowledge.

Large audio payloads belong in the archive package. This store keeps their
indexes and structured metadata, and deliberately never expires or removes
records unless a caller explicitly invokes a delete method.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Iterator, Mapping
import uuid


SCHEMA_VERSION = 3
_TURN_STATUSES = {
    "queued", "running", "completed", "failed", "cancelled", "no_wake",
    "no_speech", "empty_input", "interrupted",
}
_TERMINAL_STATUSES = {
    "completed", "failed", "cancelled", "no_wake", "no_speech",
    "empty_input", "interrupted",
}
_ROLES = {"system", "user", "assistant", "tool"}
_SENSITIVE_ATTR = re.compile(
    r"(?:api.?key|authorization|password|passwd|secret|token|credential|"
    r"transcript|prompt|reply|content|utterance|audio|pcm|body|text)",
    re.IGNORECASE,
)


class StoreError(RuntimeError):
    """Base error raised by :class:`AsteroStore`."""


class StoreConfigurationError(StoreError):
    """The configured database path or options are invalid."""


class StoreSchemaError(StoreError):
    """The database schema is newer than this implementation can read."""


class StoreCorruptError(StoreError):
    """SQLite reports that the configured database is corrupt or unreadable."""


class StoreNotFoundError(StoreError):
    """A requested parent row, session, or turn does not exist."""


class StoreStateError(StoreError):
    """A requested state transition or scope operation is invalid."""


@dataclass(frozen=True, slots=True)
class SessionRecord:
    session_id: str
    created_at_utc: str
    updated_at_utc: str
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class TurnRecord:
    turn_id: str
    trace_id: str
    session_id: str
    status: str
    created_at_utc: str
    updated_at_utc: str
    error_code: str | None = None
    result: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class StageEventRecord:
    event_id: str
    trace_id: str
    turn_id: str
    session_id: str
    stage: str
    event: str
    occurred_at_utc: str
    monotonic_ns: int | None
    elapsed_ms: float | None
    status: str | None
    attempt: int
    attributes: dict[str, Any]
    sequence: int


@dataclass(frozen=True, slots=True)
class ArchiveIndexRecord:
    capture_id: str
    turn_id: str | None
    manifest_path: str
    started_at_utc: str
    ended_at_utc: str | None
    frame_count: int
    complete: bool
    error_code: str | None


@dataclass(frozen=True, slots=True)
class ConversationTurnRecord:
    conversation_turn_id: str
    session_id: str
    turn_id: str | None
    role: str
    content: str
    source: str | None
    created_at_utc: str
    sequence: int


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    memory_id: str
    content: str
    source: str
    scope: str
    session_id: str | None
    created_at_utc: str
    updated_at_utc: str


@dataclass(frozen=True, slots=True)
class KnowledgeRecord:
    document_id: str
    content: str
    source: str
    scope: str
    session_id: str | None
    metadata: dict[str, Any]
    imported_at_utc: str
    updated_at_utc: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _json_dump(value: Mapping[str, Any] | None) -> str:
    try:
        return json.dumps(dict(value or {}), ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise StoreStateError("metadata must be JSON-compatible and finite") from exc


def _json_load(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    value = json.loads(raw)
    return value if isinstance(value, dict) else {}


def _required(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StoreStateError(f"{name} must be a non-empty string")
    return value.strip()


def _sanitize_attributes(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    """Redact secret and body-like event attributes before persistence."""
    def clean(value: Any, key: str = "") -> Any:
        if key and _SENSITIVE_ATTR.search(key):
            return "[REDACTED]"
        if isinstance(value, Mapping):
            return {str(child_key): clean(child_value, str(child_key))
                    for child_key, child_value in value.items()}
        if isinstance(value, (list, tuple)):
            return [clean(item) for item in value]
        if value is None or isinstance(value, (str, bool, int, float)):
            return value
        raise StoreStateError("event attributes must contain JSON-compatible values")

    result = clean(attributes or {})
    # Validate non-finite floats and serialization before opening a transaction.
    _json_dump(result)
    return result


class AsteroStore:
    """Thread-safe SQLite repository with explicit migrations.

    Each operation opens its own connection and transaction. The default DB is
    outside the repository at ``~/.local/share/astero/astero.sqlite3``. Set
    ``path`` to use a dedicated database for tests or another local profile.
    """

    def __init__(self, path: str | os.PathLike[str] | None = None,
                 *, busy_timeout_seconds: float = 30.0) -> None:
        if busy_timeout_seconds <= 0:
            raise StoreConfigurationError("busy_timeout_seconds must be positive")
        selected = Path(path).expanduser() if path is not None else (
            Path.home() / ".local" / "share" / "astero" / "astero.sqlite3"
        )
        if str(selected) == ":memory:":
            raise StoreConfigurationError("use a filesystem path; in-memory DBs cannot survive per-operation connections")
        self.path = selected.absolute()
        self.busy_timeout_seconds = float(busy_timeout_seconds)
        self._init_lock = threading.Lock()
        self._fts_available = False
        self._initialize()

    def __enter__(self) -> "AsteroStore":
        return self

    def __exit__(self, *_: object) -> None:
        # Connections are operation-scoped, so there is no persistent handle.
        return None

    @property
    def schema_version(self) -> int:
        with self._connection() as connection:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])

    @property
    def fts_available(self) -> bool:
        return self._fts_available

    def _prepare_path(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.path.exists() and self.path.is_dir():
                raise StoreConfigurationError("database path points to a directory")
        except StoreConfigurationError:
            raise
        except OSError as exc:
            raise StoreConfigurationError("could not create database directory") from exc

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self._prepare_path()
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                str(self.path), timeout=self.busy_timeout_seconds,
                isolation_level=None, check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_seconds * 1000)}")
            yield connection
        except sqlite3.DatabaseError as exc:
            if _looks_corrupt(exc):
                raise StoreCorruptError("SQLite database is corrupt or unreadable") from exc
            raise StoreError("SQLite operation failed") from exc
        except OSError as exc:
            raise StoreError("database filesystem operation failed") from exc
        finally:
            if connection is not None:
                connection.close()

    @contextmanager
    def _transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                yield connection
                connection.commit()
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise

    def _initialize(self) -> None:
        with self._init_lock:
            self._prepare_path()
            try:
                with self._connection() as connection:
                    connection.execute("PRAGMA journal_mode = WAL")
                    connection.execute("PRAGMA synchronous = FULL")
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        current = int(connection.execute("PRAGMA user_version").fetchone()[0])
                        if current > SCHEMA_VERSION:
                            raise StoreSchemaError(
                                f"database schema {current} is newer than supported {SCHEMA_VERSION}"
                            )
                        if current < 1:
                            self._migration_1(connection)
                            connection.execute("PRAGMA user_version = 1")
                            current = 1
                        if current < 2:
                            # FTS5 is an optimization; fallback search remains available.
                            self._try_create_fts(connection)
                            connection.execute("PRAGMA user_version = 2")
                            current = 2
                        else:
                            self._try_create_fts(connection)
                        if current < 3:
                            self._migration_3(connection)
                            connection.execute("PRAGMA user_version = 3")
                        connection.commit()
                    except BaseException:
                        if connection.in_transaction:
                            connection.rollback()
                        raise
                try:
                    os.chmod(self.path, 0o600)
                except OSError as exc:
                    raise StoreConfigurationError("could not restrict database file permissions") from exc
            except (StoreConfigurationError, StoreSchemaError, StoreCorruptError, StoreError):
                raise
            except sqlite3.DatabaseError as exc:
                if _looks_corrupt(exc):
                    raise StoreCorruptError("SQLite database is corrupt or unreadable") from exc
                raise StoreError("could not initialize SQLite database") from exc
            except OSError as exc:
                raise StoreConfigurationError("could not initialize database file") from exc

    @staticmethod
    def _migration_1(connection: sqlite3.Connection) -> None:
        statements = (
            """CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )""",
            """CREATE TABLE IF NOT EXISTS turns (
                turn_id TEXT PRIMARY KEY,
                trace_id TEXT NOT NULL,
                session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                status TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL,
                error_code TEXT,
                result_json TEXT
            )""",
            "CREATE INDEX IF NOT EXISTS idx_turns_session_created ON turns(session_id, created_at_utc)",
            "CREATE INDEX IF NOT EXISTS idx_turns_status ON turns(status)",
            """CREATE TABLE IF NOT EXISTS stage_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                trace_id TEXT NOT NULL,
                turn_id TEXT NOT NULL REFERENCES turns(turn_id) ON DELETE CASCADE,
                session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                stage TEXT NOT NULL,
                event TEXT NOT NULL,
                occurred_at_utc TEXT NOT NULL,
                elapsed_ms REAL,
                status TEXT,
                attempt INTEGER NOT NULL DEFAULT 1,
                attributes_json TEXT NOT NULL DEFAULT '{}'
            )""",
            "CREATE INDEX IF NOT EXISTS idx_stage_events_turn_sequence ON stage_events(turn_id, sequence)",
            "CREATE INDEX IF NOT EXISTS idx_stage_events_trace_sequence ON stage_events(trace_id, sequence)",
            """CREATE TABLE IF NOT EXISTS archive_indexes (
                capture_id TEXT PRIMARY KEY,
                turn_id TEXT REFERENCES turns(turn_id) ON DELETE SET NULL,
                manifest_path TEXT NOT NULL,
                started_at_utc TEXT NOT NULL,
                ended_at_utc TEXT,
                frame_count INTEGER NOT NULL DEFAULT 0 CHECK(frame_count >= 0),
                complete INTEGER NOT NULL DEFAULT 0 CHECK(complete IN (0, 1)),
                error_code TEXT
            )""",
            "CREATE INDEX IF NOT EXISTS idx_archive_turn ON archive_indexes(turn_id)",
            """CREATE TABLE IF NOT EXISTS conversation_turns (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_turn_id TEXT NOT NULL UNIQUE,
                session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                turn_id TEXT REFERENCES turns(turn_id) ON DELETE SET NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                source TEXT,
                created_at_utc TEXT NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS idx_conversation_session_sequence ON conversation_turns(session_id, sequence)",
            """CREATE TABLE IF NOT EXISTS memories (
                memory_id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                source TEXT NOT NULL,
                scope TEXT NOT NULL CHECK(scope IN ('user', 'session')),
                session_id TEXT REFERENCES sessions(session_id) ON DELETE CASCADE,
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL,
                CHECK((scope = 'user' AND session_id IS NULL) OR
                      (scope = 'session' AND session_id IS NOT NULL))
            )""",
            "CREATE INDEX IF NOT EXISTS idx_memories_scope_session ON memories(scope, session_id, updated_at_utc)",
            """CREATE TABLE IF NOT EXISTS knowledge_documents (
                document_id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                source TEXT NOT NULL,
                scope TEXT NOT NULL CHECK(scope IN ('global', 'session')),
                session_id TEXT REFERENCES sessions(session_id) ON DELETE CASCADE,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                imported_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL,
                CHECK((scope = 'global' AND session_id IS NULL) OR
                      (scope = 'session' AND session_id IS NOT NULL))
            )""",
            "CREATE INDEX IF NOT EXISTS idx_knowledge_scope_session ON knowledge_documents(scope, session_id, updated_at_utc)",
        )
        for statement in statements:
            connection.execute(statement)

    @staticmethod
    def _migration_3(connection: sqlite3.Connection) -> None:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(stage_events)")}
        if "monotonic_ns" not in columns:
            connection.execute("ALTER TABLE stage_events ADD COLUMN monotonic_ns INTEGER")

    def _try_create_fts(self, connection: sqlite3.Connection) -> None:
        try:
            had_memory_index = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_fts'"
            ).fetchone() is not None
            had_knowledge_index = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_fts'"
            ).fetchone() is not None
            connection.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(memory_id UNINDEXED, content, source)"
            )
            connection.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(document_id UNINDEXED, content, source)"
            )
            # Populate only newly created indexes. Rebuilding on every service
            # restart would make startup proportional to the whole knowledge base.
            if not had_memory_index:
                connection.execute(
                    "INSERT INTO memory_fts(memory_id, content, source) "
                    "SELECT memory_id, content, source FROM memories"
                )
            if not had_knowledge_index:
                connection.execute(
                    "INSERT INTO knowledge_fts(document_id, content, source) "
                    "SELECT document_id, content, source FROM knowledge_documents"
                )
            self._fts_available = True
        except sqlite3.OperationalError as exc:
            if "fts5" in str(exc).lower() or "no such module" in str(exc).lower():
                self._fts_available = False
                return
            raise

    def create_session(self, session_id: str, metadata: Mapping[str, Any] | None = None) -> SessionRecord:
        session_id = _required(session_id, "session_id")
        encoded = _json_dump(metadata)
        now = _utc_now()
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO sessions(session_id, created_at_utc, updated_at_utc, metadata_json) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
                "updated_at_utc=excluded.updated_at_utc, metadata_json=excluded.metadata_json",
                (session_id, now, now, encoded),
            )
            row = connection.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        return self._session_from_row(row)

    def get_session(self, session_id: str) -> SessionRecord | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM sessions WHERE session_id=?", (_required(session_id, "session_id"),)).fetchone()
        return self._session_from_row(row) if row else None

    def create_turn(self, turn_id: str, trace_id: str, session_id: str,
                    status: str = "queued") -> TurnRecord:
        turn_id, trace_id, session_id = (
            _required(turn_id, "turn_id"), _required(trace_id, "trace_id"),
            _required(session_id, "session_id"),
        )
        self._validate_turn_status(status)
        now = _utc_now()
        with self._transaction() as connection:
            if connection.execute("SELECT 1 FROM sessions WHERE session_id=?", (session_id,)).fetchone() is None:
                raise StoreNotFoundError("session does not exist")
            try:
                connection.execute(
                    "INSERT INTO turns(turn_id, trace_id, session_id, status, created_at_utc, updated_at_utc) "
                    "VALUES (?, ?, ?, ?, ?, ?)", (turn_id, trace_id, session_id, status, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise StoreStateError("turn_id already exists or violates a database constraint") from exc
            row = connection.execute("SELECT * FROM turns WHERE turn_id=?", (turn_id,)).fetchone()
        return self._turn_from_row(row)

    def update_turn(self, turn_id: str, status: str, *, error_code: str | None = None,
                    result: Mapping[str, Any] | None = None) -> TurnRecord:
        turn_id = _required(turn_id, "turn_id")
        self._validate_turn_status(status)
        result_json = _json_dump(result) if result is not None else None
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM turns WHERE turn_id=?", (turn_id,)).fetchone()
            if row is None:
                raise StoreNotFoundError("turn does not exist")
            previous = row["status"]
            if previous in _TERMINAL_STATUSES and status != previous:
                raise StoreStateError("terminal turn state cannot be changed")
            connection.execute(
                "UPDATE turns SET status=?, updated_at_utc=?, error_code=?, "
                "result_json=COALESCE(?, result_json) WHERE turn_id=?",
                (status, _utc_now(), error_code, result_json, turn_id),
            )
            updated = connection.execute("SELECT * FROM turns WHERE turn_id=?", (turn_id,)).fetchone()
        return self._turn_from_row(updated)

    def get_turn(self, turn_id: str) -> TurnRecord | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM turns WHERE turn_id=?", (_required(turn_id, "turn_id"),)).fetchone()
        return self._turn_from_row(row) if row else None

    def list_turns(self, session_id: str, limit: int = 100) -> tuple[TurnRecord, ...]:
        session_id, limit = _required(session_id, "session_id"), self._limit(limit)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM turns WHERE session_id=? ORDER BY created_at_utc DESC, turn_id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return tuple(self._turn_from_row(row) for row in rows)

    def list_unfinished_turns(self) -> tuple[TurnRecord, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM turns WHERE status IN ('queued','running') ORDER BY created_at_utc, turn_id"
            ).fetchall()
        return tuple(self._turn_from_row(row) for row in rows)

    def append_stage_event(self, event_id: str, trace_id: str, turn_id: str, session_id: str,
                           stage: str, event: str, occurred_at_utc: str | None = None,
                           elapsed_ms: float | None = None, status: str | None = None,
                           attempt: int = 1, attributes: Mapping[str, Any] | None = None,
                           monotonic_ns: int | None = None) -> StageEventRecord:
        event_id, trace_id, turn_id, session_id, stage, event = tuple(
            _required(value, name) for value, name in (
                (event_id, "event_id"), (trace_id, "trace_id"), (turn_id, "turn_id"),
                (session_id, "session_id"), (stage, "stage"), (event, "event"),
            )
        )
        if elapsed_ms is not None and (
            not isinstance(elapsed_ms, (int, float)) or isinstance(elapsed_ms, bool)
            or not math.isfinite(elapsed_ms) or elapsed_ms < 0
        ):
            raise StoreStateError("elapsed_ms must be non-negative or None")
        if not isinstance(attempt, int) or attempt < 1:
            raise StoreStateError("attempt must be a positive integer")
        if monotonic_ns is not None and (
            not isinstance(monotonic_ns, int) or isinstance(monotonic_ns, bool) or monotonic_ns < 0
        ):
            raise StoreStateError("monotonic_ns must be a non-negative integer or None")
        safe_attributes = _sanitize_attributes(attributes)
        occurred_at_utc = occurred_at_utc or _utc_now()
        with self._transaction() as connection:
            turn = connection.execute("SELECT trace_id, session_id FROM turns WHERE turn_id=?", (turn_id,)).fetchone()
            if turn is None:
                raise StoreNotFoundError("turn does not exist")
            if turn["trace_id"] != trace_id or turn["session_id"] != session_id:
                raise StoreStateError("stage event trace/session must match its turn")
            try:
                cursor = connection.execute(
                    "INSERT INTO stage_events(event_id, trace_id, turn_id, session_id, stage, event, "
                    "occurred_at_utc, monotonic_ns, elapsed_ms, status, attempt, attributes_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (event_id, trace_id, turn_id, session_id, stage, event, occurred_at_utc,
                     monotonic_ns, elapsed_ms, status, attempt, _json_dump(safe_attributes)),
                )
            except sqlite3.IntegrityError as exc:
                raise StoreStateError("event_id already exists or violates a database constraint") from exc
            row = connection.execute("SELECT * FROM stage_events WHERE sequence=?", (cursor.lastrowid,)).fetchone()
        return self._event_from_row(row)

    def list_stage_events(self, turn_id: str, limit: int | None = None) -> tuple[StageEventRecord, ...]:
        if limit is not None:
            limit = self._limit(limit)
        with self._connection() as connection:
            if limit is None:
                rows = connection.execute(
                    "SELECT * FROM stage_events WHERE turn_id=? ORDER BY sequence",
                    (_required(turn_id, "turn_id"),),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM (SELECT * FROM stage_events WHERE turn_id=? "
                    "ORDER BY sequence DESC LIMIT ?) ORDER BY sequence",
                    (_required(turn_id, "turn_id"), limit),
                ).fetchall()
        return tuple(self._event_from_row(row) for row in rows)

    def put_archive_index(self, capture_id: str, manifest_path: str, started_at_utc: str,
                          turn_id: str | None = None, ended_at_utc: str | None = None,
                          frame_count: int = 0, complete: bool = False,
                          error_code: str | None = None) -> ArchiveIndexRecord:
        capture_id = _required(capture_id, "capture_id")
        manifest_path = _required(manifest_path, "manifest_path")
        started_at_utc = _required(started_at_utc, "started_at_utc")
        if turn_id is not None:
            turn_id = _required(turn_id, "turn_id")
        if not isinstance(frame_count, int) or frame_count < 0:
            raise StoreStateError("frame_count must be a non-negative integer")
        with self._transaction() as connection:
            if turn_id and connection.execute("SELECT 1 FROM turns WHERE turn_id=?", (turn_id,)).fetchone() is None:
                raise StoreNotFoundError("turn does not exist")
            connection.execute(
                "INSERT INTO archive_indexes(capture_id, turn_id, manifest_path, started_at_utc, ended_at_utc, "
                "frame_count, complete, error_code) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(capture_id) DO UPDATE SET turn_id=excluded.turn_id, "
                "manifest_path=excluded.manifest_path, started_at_utc=excluded.started_at_utc, "
                "ended_at_utc=excluded.ended_at_utc, frame_count=excluded.frame_count, "
                "complete=excluded.complete, error_code=excluded.error_code",
                (capture_id, turn_id, manifest_path, started_at_utc, ended_at_utc,
                 frame_count, int(bool(complete)), error_code),
            )
            row = connection.execute("SELECT * FROM archive_indexes WHERE capture_id=?", (capture_id,)).fetchone()
        return self._archive_from_row(row)

    def get_archive_index(self, capture_id: str) -> ArchiveIndexRecord | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM archive_indexes WHERE capture_id=?", (_required(capture_id, "capture_id"),)).fetchone()
        return self._archive_from_row(row) if row else None

    def append_conversation_turn(self, session_id: str, turn_id: str | None,
                                 role: str, content: str, source: str | None = None,
                                 conversation_turn_id: str | None = None) -> ConversationTurnRecord:
        session_id = _required(session_id, "session_id")
        role = _required(role, "role").lower()
        content = _required(content, "content")
        if role not in _ROLES:
            raise StoreStateError("role must be system, user, assistant, or tool")
        conversation_turn_id = _required(conversation_turn_id or str(uuid.uuid4()), "conversation_turn_id")
        created_at = _utc_now()
        with self._transaction() as connection:
            if connection.execute("SELECT 1 FROM sessions WHERE session_id=?", (session_id,)).fetchone() is None:
                raise StoreNotFoundError("session does not exist")
            if turn_id is not None:
                turn = connection.execute("SELECT session_id FROM turns WHERE turn_id=?", (turn_id,)).fetchone()
                if turn is None:
                    raise StoreNotFoundError("turn does not exist")
                if turn["session_id"] != session_id:
                    raise StoreStateError("conversation turn session must match its turn")
            try:
                cursor = connection.execute(
                    "INSERT INTO conversation_turns(conversation_turn_id, session_id, turn_id, role, content, source, created_at_utc) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (conversation_turn_id, session_id, turn_id, role, content, source, created_at),
                )
            except sqlite3.IntegrityError as exc:
                raise StoreStateError("conversation_turn_id already exists") from exc
            row = connection.execute("SELECT * FROM conversation_turns WHERE sequence=?", (cursor.lastrowid,)).fetchone()
        return self._conversation_from_row(row)

    def get_conversation_context(self, session_id: str, limit: int = 16) -> tuple[ConversationTurnRecord, ...]:
        session_id, limit = _required(session_id, "session_id"), self._limit(limit)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM (SELECT * FROM conversation_turns WHERE session_id=? "
                "ORDER BY sequence DESC LIMIT ?) ORDER BY sequence",
                (session_id, limit),
            ).fetchall()
        return tuple(self._conversation_from_row(row) for row in rows)

    def add_memory(self, content: str, source: str, scope: str = "session",
                   session_id: str | None = None, memory_id: str | None = None) -> MemoryRecord:
        content, source = _required(content, "content"), _required(source, "source")
        scope = _required(scope, "scope").lower()
        session_id = _scope_session(scope, session_id, valid_scopes={"session", "user"})
        memory_id = _required(memory_id or str(uuid.uuid4()), "memory_id")
        now = _utc_now()
        with self._transaction() as connection:
            self._require_session_if_set(connection, session_id)
            try:
                connection.execute(
                    "INSERT INTO memories(memory_id, content, source, scope, session_id, created_at_utc, updated_at_utc) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (memory_id, content, source, scope, session_id, now, now),
                )
                if self._fts_available:
                    connection.execute("INSERT INTO memory_fts(memory_id, content, source) VALUES (?, ?, ?)",
                                       (memory_id, content, source))
            except sqlite3.IntegrityError as exc:
                raise StoreStateError("memory_id already exists or memory scope is invalid") from exc
            row = connection.execute("SELECT * FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
        return self._memory_from_row(row)

    def list_memories(self, session_id: str | None = None, scope: str | None = None,
                      limit: int = 100) -> tuple[MemoryRecord, ...]:
        limit = self._limit(limit)
        where, params = self._scope_query(session_id, scope, session_scope="session", global_scope="user")
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM memories {where} ORDER BY updated_at_utc DESC, memory_id LIMIT ?",
                (*params, limit),
            ).fetchall()
        return tuple(self._memory_from_row(row) for row in rows)

    def search_memories(self, query: str, session_id: str | None = None,
                        scope: str | None = None, limit: int = 10) -> tuple[MemoryRecord, ...]:
        query, limit = _required(query, "query"), self._limit(limit)
        where, params = self._scope_query(session_id, scope, session_scope="session", global_scope="user")
        if self._fts_available and _safe_fts_query(query):
            with self._connection() as connection:
                rows = connection.execute(
                    "SELECT m.* FROM memory_fts f JOIN memories m ON m.memory_id=f.memory_id "
                    f"WHERE memory_fts MATCH ? {where.replace('WHERE', 'AND', 1) if where else ''} "
                    "ORDER BY rank, m.updated_at_utc DESC LIMIT ?",
                    (_fts_query(query), *params, limit),
                ).fetchall()
        else:
            with self._connection() as connection:
                rows = connection.execute(
                    f"SELECT * FROM memories {where + ' AND' if where else 'WHERE'} "
                    "(content LIKE ? ESCAPE '\\' OR source LIKE ? ESCAPE '\\') "
                    "ORDER BY updated_at_utc DESC LIMIT ?",
                    (*params, _like_pattern(query), _like_pattern(query), limit),
                ).fetchall()
        return tuple(self._memory_from_row(row) for row in rows)

    def delete_memory(self, memory_id: str, session_id: str | None = None) -> bool:
        memory_id = _required(memory_id, "memory_id")
        with self._transaction() as connection:
            row = connection.execute("SELECT session_id FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
            if row is None:
                return False
            if session_id is not None and row["session_id"] != session_id:
                return False
            connection.execute("DELETE FROM memories WHERE memory_id=?", (memory_id,))
            if self._fts_available:
                connection.execute("DELETE FROM memory_fts WHERE memory_id=?", (memory_id,))
        return True

    def add_knowledge(self, content: str, source: str, document_id: str | None = None,
                      scope: str = "global", session_id: str | None = None,
                      metadata: Mapping[str, Any] | None = None) -> KnowledgeRecord:
        content, source = _required(content, "content"), _required(source, "source")
        scope = _required(scope, "scope").lower()
        session_id = _scope_session(scope, session_id, valid_scopes={"global", "session"})
        document_id = _required(document_id or str(uuid.uuid4()), "document_id")
        encoded = _json_dump(metadata)
        now = _utc_now()
        with self._transaction() as connection:
            self._require_session_if_set(connection, session_id)
            try:
                connection.execute(
                    "INSERT INTO knowledge_documents(document_id, content, source, scope, session_id, metadata_json, imported_at_utc, updated_at_utc) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (document_id, content, source, scope, session_id, encoded, now, now),
                )
                if self._fts_available:
                    connection.execute("INSERT INTO knowledge_fts(document_id, content, source) VALUES (?, ?, ?)",
                                       (document_id, content, source))
            except sqlite3.IntegrityError as exc:
                raise StoreStateError("document_id already exists or knowledge scope is invalid") from exc
            row = connection.execute("SELECT * FROM knowledge_documents WHERE document_id=?", (document_id,)).fetchone()
        return self._knowledge_from_row(row)

    def list_knowledge(self, session_id: str | None = None, limit: int = 100) -> tuple[KnowledgeRecord, ...]:
        limit = self._limit(limit)
        where, params = self._scope_query(session_id, None, session_scope="session", global_scope="global")
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM knowledge_documents {where} ORDER BY updated_at_utc DESC, document_id LIMIT ?",
                (*params, limit),
            ).fetchall()
        return tuple(self._knowledge_from_row(row) for row in rows)

    def search_knowledge(self, query: str, session_id: str | None = None,
                         limit: int = 10) -> tuple[KnowledgeRecord, ...]:
        query, limit = _required(query, "query"), self._limit(limit)
        where, params = self._scope_query(session_id, None, session_scope="session", global_scope="global")
        if self._fts_available and _safe_fts_query(query):
            with self._connection() as connection:
                rows = connection.execute(
                    "SELECT k.* FROM knowledge_fts f JOIN knowledge_documents k ON k.document_id=f.document_id "
                    f"WHERE knowledge_fts MATCH ? {where.replace('WHERE', 'AND', 1) if where else ''} "
                    "ORDER BY rank, k.updated_at_utc DESC LIMIT ?",
                    (_fts_query(query), *params, limit),
                ).fetchall()
        else:
            with self._connection() as connection:
                rows = connection.execute(
                    f"SELECT * FROM knowledge_documents {where + ' AND' if where else 'WHERE'} "
                    "(content LIKE ? ESCAPE '\\' OR source LIKE ? ESCAPE '\\') "
                    "ORDER BY updated_at_utc DESC LIMIT ?",
                    (*params, _like_pattern(query), _like_pattern(query), limit),
                ).fetchall()
        return tuple(self._knowledge_from_row(row) for row in rows)

    def delete_knowledge(self, document_id: str, session_id: str | None = None) -> bool:
        document_id = _required(document_id, "document_id")
        with self._transaction() as connection:
            row = connection.execute("SELECT session_id FROM knowledge_documents WHERE document_id=?", (document_id,)).fetchone()
            if row is None:
                return False
            if session_id is not None and row["session_id"] not in (None, session_id):
                return False
            connection.execute("DELETE FROM knowledge_documents WHERE document_id=?", (document_id,))
            if self._fts_available:
                connection.execute("DELETE FROM knowledge_fts WHERE document_id=?", (document_id,))
        return True

    @staticmethod
    def _validate_turn_status(status: str) -> None:
        if status not in _TURN_STATUSES:
            raise StoreStateError("unknown turn status")

    @staticmethod
    def _limit(limit: int) -> int:
        if not isinstance(limit, int) or limit < 1 or limit > 10_000:
            raise StoreStateError("limit must be an integer between 1 and 10000")
        return limit

    @staticmethod
    def _require_session_if_set(connection: sqlite3.Connection, session_id: str | None) -> None:
        if session_id is not None and connection.execute(
            "SELECT 1 FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone() is None:
            raise StoreNotFoundError("session does not exist")

    @staticmethod
    def _scope_query(session_id: str | None, scope: str | None, *, session_scope: str,
                     global_scope: str) -> tuple[str, tuple[Any, ...]]:
        if scope is not None:
            scope = _required(scope, "scope").lower()
            if scope not in {session_scope, global_scope}:
                raise StoreStateError("invalid scope")
        if session_id is not None:
            session_id = _required(session_id, "session_id")
            if scope == session_scope:
                return "WHERE scope=? AND session_id=?", (session_scope, session_id)
            if scope == global_scope:
                return "WHERE scope=? AND session_id IS NULL", (global_scope,)
            return "WHERE ((scope=? AND session_id=?) OR (scope=? AND session_id IS NULL))", (
                session_scope, session_id, global_scope,
            )
        if scope is not None:
            return "WHERE scope=?", (scope,)
        return "", ()

    @staticmethod
    def _session_from_row(row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(row["session_id"], row["created_at_utc"], row["updated_at_utc"],
                             _json_load(row["metadata_json"]))

    @staticmethod
    def _turn_from_row(row: sqlite3.Row) -> TurnRecord:
        return TurnRecord(row["turn_id"], row["trace_id"], row["session_id"], row["status"],
                          row["created_at_utc"], row["updated_at_utc"], row["error_code"],
                          _json_load(row["result_json"]) if row["result_json"] else None)

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> StageEventRecord:
        return StageEventRecord(row["event_id"], row["trace_id"], row["turn_id"], row["session_id"],
                                row["stage"], row["event"], row["occurred_at_utc"], row["monotonic_ns"],
                                row["elapsed_ms"],
                                row["status"], row["attempt"], _json_load(row["attributes_json"]),
                                row["sequence"])

    @staticmethod
    def _archive_from_row(row: sqlite3.Row) -> ArchiveIndexRecord:
        return ArchiveIndexRecord(row["capture_id"], row["turn_id"], row["manifest_path"],
                                  row["started_at_utc"], row["ended_at_utc"], row["frame_count"],
                                  bool(row["complete"]), row["error_code"])

    @staticmethod
    def _conversation_from_row(row: sqlite3.Row) -> ConversationTurnRecord:
        return ConversationTurnRecord(row["conversation_turn_id"], row["session_id"], row["turn_id"],
                                       row["role"], row["content"], row["source"],
                                       row["created_at_utc"], row["sequence"])

    @staticmethod
    def _memory_from_row(row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(row["memory_id"], row["content"], row["source"], row["scope"],
                            row["session_id"], row["created_at_utc"], row["updated_at_utc"])

    @staticmethod
    def _knowledge_from_row(row: sqlite3.Row) -> KnowledgeRecord:
        return KnowledgeRecord(row["document_id"], row["content"], row["source"], row["scope"],
                               row["session_id"], _json_load(row["metadata_json"]),
                               row["imported_at_utc"], row["updated_at_utc"])


def _scope_session(scope: str, session_id: str | None, *, valid_scopes: set[str]) -> str | None:
    if scope not in valid_scopes:
        raise StoreStateError("invalid scope")
    if scope in {"session"} and session_id is None:
        raise StoreStateError("session scope requires session_id")
    if scope in {"user", "global"} and session_id is not None:
        raise StoreStateError(f"{scope} scope must not specify session_id")
    return _required(session_id, "session_id") if session_id is not None else None


def _safe_fts_query(query: str) -> bool:
    return bool(query.strip())


def _fts_query(query: str) -> str:
    # Search the user string as a phrase so FTS operators/punctuation remain
    # plain text instead of being interpreted as query syntax.
    return '"' + query.replace('"', '""') + '"'


def _like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _looks_corrupt(exc: sqlite3.DatabaseError) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in ("malformed", "not a database", "disk image is malformed", "file is encrypted"))


__all__ = [
    "ArchiveIndexRecord", "AsteroStore", "ConversationTurnRecord", "KnowledgeRecord",
    "MemoryRecord", "SessionRecord", "StageEventRecord", "StoreConfigurationError",
    "StoreCorruptError", "StoreError", "StoreNotFoundError", "StoreSchemaError",
    "StoreStateError", "TurnRecord",
]
