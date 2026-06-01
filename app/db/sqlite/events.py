"""Sqlite write-side impl. Schema mirrors the prod Dynamo table — PK on
``user_id``, SK on ``received_at`` — so the inspector queries written
against this work unchanged once the Dynamo impl lands.

WAL mode is required because lingo-ops reads from this same file
concurrently. Sqlite handles "one writer + many readers" cleanly with
WAL; default rollback-journal mode would lock readers out during writes.
"""

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

logger = logging.getLogger("lingo_async.db.events")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    event_type    TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    received_at   TEXT NOT NULL,
    status        TEXT NOT NULL,
    error_msg     TEXT,
    outcomes_json TEXT NOT NULL DEFAULT '[]',
    ttl_epoch     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_events_received_at ON events(received_at);
CREATE INDEX IF NOT EXISTS idx_events_user_received ON events(user_id, received_at);
"""


class SqliteEventsWriteRepository:
    def __init__(self, path: str) -> None:
        self.path = path
        with self._conn() as con:
            con.executescript(_SCHEMA)
            con.execute("PRAGMA journal_mode=WAL")

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.path)
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def save(
        self,
        *,
        id: str,
        user_id: str,
        event_type: str,
        payload_json: str,
        received_at: str,
        ttl_epoch: int,
    ) -> None:
        with self._conn() as con:
            con.execute(
                "INSERT INTO events (id, user_id, event_type, payload_json, "
                "received_at, status, ttl_epoch) VALUES (?, ?, ?, ?, ?, 'received', ?)",
                (id, user_id, event_type, payload_json, received_at, ttl_epoch),
            )

    def update_status(
        self,
        *,
        id: str,
        status: str,
        error_msg: str | None,
        outcomes_json: str,
    ) -> None:
        with self._conn() as con:
            con.execute(
                "UPDATE events SET status=?, error_msg=?, outcomes_json=? WHERE id=?",
                (status, error_msg, outcomes_json, id),
            )
