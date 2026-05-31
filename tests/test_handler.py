"""Dispatch-loop tests for ``app.handler.lambda_handler``.

Each test stubs out the per-type handler so we can assert routing logic
without touching boto3.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from app import handler as handler_module


def test_routes_xp_awarded_to_handler(sqs_record_factory, xp_event_dict) -> None:
    event = {"Records": [sqs_record_factory(xp_event_dict)]}

    with patch.object(handler_module.xp_awarded, "handle") as h:
        result = handler_module.lambda_handler(event, None)

    h.assert_called_once()
    parsed = h.call_args.args[0]
    assert parsed.type == "xp_awarded"
    assert parsed.user_id == xp_event_dict["user_id"]
    assert result == {"batchItemFailures": []}


def test_routes_each_type_to_its_handler(sqs_record_factory) -> None:
    bodies = [
        {
            "type": "xp_awarded",
            "version": 1,
            "user_id": "u",
            "amount": 1,
            "source": "manual",
        },
        {
            "type": "lesson_completed",
            "version": 1,
            "user_id": "u",
            "lesson_id": "l",
            "score": 1.0,
            "perfect": True,
            "attempted_at": "2026-05-27T00:00:00+00:00",
        },
        {
            "type": "review_completed",
            "version": 1,
            "user_id": "u",
            "card_id": "c",
            "modality": "recognition",
            "rating": "good",
        },
        {
            "type": "friend_added",
            "version": 1,
            "user_id": "u",
            "friend_id": "f",
        },
        {
            "type": "subscription_changed",
            "version": 1,
            "user_id": "u",
            "tier": "supporter",
            "event": "new",
        },
    ]
    records = [sqs_record_factory(b, message_id=f"m-{i}") for i, b in enumerate(bodies)]
    event = {"Records": records}

    with (
        patch.object(handler_module.xp_awarded, "handle") as xp_h,
        patch.object(handler_module.lesson_completed, "handle") as lesson_h,
        patch.object(handler_module.review_completed, "handle") as review_h,
        patch.object(handler_module.friend_added, "handle") as friend_h,
        patch.object(handler_module.subscription_changed, "handle") as sub_h,
    ):
        result = handler_module.lambda_handler(event, None)

    xp_h.assert_called_once()
    lesson_h.assert_called_once()
    review_h.assert_called_once()
    friend_h.assert_called_once()
    sub_h.assert_called_once()
    assert result == {"batchItemFailures": []}


def test_malformed_body_reports_message_id_for_retry(sqs_record_factory) -> None:
    # Body fails JSON validation — should land in batchItemFailures.
    event = {"Records": [sqs_record_factory("not json", message_id="bad-1")]}

    result = handler_module.lambda_handler(event, None)

    assert result == {"batchItemFailures": [{"itemIdentifier": "bad-1"}]}


def test_validation_error_reports_message_id(sqs_record_factory) -> None:
    # Valid JSON but missing required fields.
    body = json.dumps({"type": "xp_awarded", "version": 1, "user_id": "u"})  # no amount/source
    event = {"Records": [sqs_record_factory(body, message_id="bad-2")]}

    result = handler_module.lambda_handler(event, None)

    assert result == {"batchItemFailures": [{"itemIdentifier": "bad-2"}]}


def test_handler_exception_reports_message_id(sqs_record_factory, xp_event_dict) -> None:
    event = {"Records": [sqs_record_factory(xp_event_dict, message_id="boom")]}

    with patch.object(handler_module.xp_awarded, "handle", side_effect=RuntimeError("boom")):
        result = handler_module.lambda_handler(event, None)

    assert result == {"batchItemFailures": [{"itemIdentifier": "boom"}]}


def test_partial_batch_failure_only_marks_bad_messages(sqs_record_factory, xp_event_dict) -> None:
    # Two records: one good, one whose handler raises.
    good = sqs_record_factory(xp_event_dict, message_id="ok-1")
    bad = sqs_record_factory(xp_event_dict, message_id="bad-1")
    event = {"Records": [good, bad]}

    call_count = {"n": 0}

    def flaky(_evt):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("second one fails")

    with patch.object(handler_module.xp_awarded, "handle", side_effect=flaky):
        result = handler_module.lambda_handler(event, None)

    assert result == {"batchItemFailures": [{"itemIdentifier": "bad-1"}]}


def test_empty_records_returns_empty_failures() -> None:
    result = handler_module.lambda_handler({"Records": []}, None)
    assert result == {"batchItemFailures": []}


def test_dispatch_writes_event_row(monkeypatch, sqs_record_factory, xp_event_dict, tmp_path):
    """The dispatch loop saves an event row before handling and updates it
    with status + outcomes after."""
    from app.config import settings
    from app.db.sqlite.events import SqliteEventsWriteRepository
    from app import handler as handler_mod

    monkeypatch.setattr(settings, "EVENT_LOG_BACKEND", "sqlite")
    monkeypatch.setattr(settings, "EVENT_LOG_SQLITE_PATH", str(tmp_path / "ev.sqlite"))

    # Force a known repo so we can inspect it after dispatch.
    repo = SqliteEventsWriteRepository(str(tmp_path / "ev.sqlite"))
    monkeypatch.setattr(handler_mod, "_get_events_repo", lambda: repo)

    # Stub the xp_awarded handler module's `handle` to return a known action list.
    from app.handlers import xp_awarded as xp_mod
    monkeypatch.setattr(xp_mod, "handle", lambda event: [{"handler": "test", "actions": [{"k": "v"}]}])

    record = sqs_record_factory(xp_event_dict, message_id="msg-x")
    result = handler_mod.lambda_handler({"Records": [record]}, object())
    assert result == {"batchItemFailures": []}

    import sqlite3
    con = sqlite3.connect(repo.path)
    row = con.execute(
        "SELECT id, user_id, event_type, status, outcomes_json FROM events"
    ).fetchone()
    assert row[0] == "msg-x"
    assert row[1] == "u-test-1"
    assert row[2] == "xp_awarded"
    assert row[3] == "ok"
    assert '"test"' in row[4]
