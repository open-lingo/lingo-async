"""Sqlite event-log repo. Mirrors the future DynamoDB schema 1:1 so the
prod swap is mechanical."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.db.sqlite.events import SqliteEventsWriteRepository


@pytest.fixture
def repo(tmp_path: Path) -> SqliteEventsWriteRepository:
    return SqliteEventsWriteRepository(str(tmp_path / "events.sqlite"))


def test_save_received_row_is_readable(repo, tmp_path):
    received_at = datetime.now(UTC).isoformat()
    repo.save(
        id="evt-1",
        user_id="u-1",
        event_type="xp_awarded",
        payload_json='{"amount": 10}',
        received_at=received_at,
        ttl_epoch=int(datetime.now(UTC).timestamp()) + 86400,
    )

    # Read back via raw sqlite — read-side lives in lingo-ops.
    import sqlite3

    con = sqlite3.connect(repo.path)
    row = con.execute("SELECT id, user_id, event_type, status FROM events").fetchone()
    assert row == ("evt-1", "u-1", "xp_awarded", "received")


def test_update_status_to_ok_with_outcomes(repo):
    received_at = datetime.now(UTC).isoformat()
    repo.save(
        id="evt-2", user_id="u-1", event_type="xp_awarded",
        payload_json="{}", received_at=received_at, ttl_epoch=0,
    )
    repo.update_status(
        id="evt-2",
        status="ok",
        error_msg=None,
        outcomes_json='[{"handler": "leaderboard", "actions": []}]',
    )

    import sqlite3
    con = sqlite3.connect(repo.path)
    status, err, outcomes = con.execute(
        "SELECT status, error_msg, outcomes_json FROM events WHERE id='evt-2'"
    ).fetchone()
    assert status == "ok"
    assert err is None
    assert "leaderboard" in outcomes


def test_update_status_to_failed_with_error(repo):
    received_at = datetime.now(UTC).isoformat()
    repo.save(
        id="evt-3", user_id="u-1", event_type="xp_awarded",
        payload_json="{}", received_at=received_at, ttl_epoch=0,
    )
    repo.update_status(id="evt-3", status="failed", error_msg="boom", outcomes_json="[]")

    import sqlite3
    con = sqlite3.connect(repo.path)
    status, err = con.execute(
        "SELECT status, error_msg FROM events WHERE id='evt-3'"
    ).fetchone()
    assert status == "failed"
    assert err == "boom"
