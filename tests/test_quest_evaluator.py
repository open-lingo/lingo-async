"""Quest evaluator — event → quest match + REST callback."""

import logging
from unittest.mock import MagicMock

import httpx
import pytest

from app.contracts.messages import (
    LessonCompletedMessage,
    XpAwardedMessage,
)
from app.quests import evaluator as eval_mod


def _quest(quest_id: str, unit: str, current: int = 0, target: int = 50, status: str = "active"):
    return {
        "id": quest_id, "type": "daily", "title": "k", "description": "k",
        "emoji": "", "rewards": {},
        "progress": {"current": current, "target": target, "unit": unit},
        "status": status,
    }


def test_xp_event_matches_xp_unit_quest(monkeypatch):
    fake = MagicMock()
    fake.list_quests.return_value = {"items": [_quest("q-xp", "XP"), _quest("q-cards", "cards")]}
    fake.bump_progress.return_value = {
        "id": "q-xp", "progress": {"current": 10, "target": 50, "unit": "XP"},
        "status": "active",
    }
    monkeypatch.setattr(eval_mod, "_client", lambda: fake)

    event = XpAwardedMessage(user_id="u-1", amount=10, source="lesson")
    actions = eval_mod.evaluate_quests_for("u-1", event)

    fake.bump_progress.assert_called_once_with("q-xp", user_id="u-1", delta=10)
    assert len(actions) == 1
    assert actions[0]["quest_id"] == "q-xp"
    assert actions[0]["unit"] == "XP"
    assert actions[0]["delta"] == 10


def test_lesson_event_matches_lessons_unit(monkeypatch):
    fake = MagicMock()
    fake.list_quests.return_value = {
        "items": [_quest("q-lessons", "lessons", current=2, target=5)],
    }
    fake.bump_progress.return_value = {
        "id": "q-lessons", "progress": {"current": 3, "target": 5, "unit": "lessons"},
        "status": "active",
    }
    monkeypatch.setattr(eval_mod, "_client", lambda: fake)

    event = LessonCompletedMessage(
        user_id="u-1", lesson_id="L1", score=1.0, perfect=True,
        attempted_at="2026-05-31T00:00:00Z",
    )
    actions = eval_mod.evaluate_quests_for("u-1", event)

    fake.bump_progress.assert_called_once_with("q-lessons", user_id="u-1", delta=1)
    assert actions[0]["progress_after"] == 3


def test_no_match_returns_empty(monkeypatch):
    fake = MagicMock()
    fake.list_quests.return_value = {"items": [_quest("q-cards", "cards")]}
    monkeypatch.setattr(eval_mod, "_client", lambda: fake)

    event = XpAwardedMessage(user_id="u-1", amount=10, source="lesson")
    actions = eval_mod.evaluate_quests_for("u-1", event)

    fake.bump_progress.assert_not_called()
    assert actions == []


def _http_404() -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://core/api/core/v1/quests/_internal/list")
    response = httpx.Response(404, request=request)
    return httpx.HTTPStatusError("not found", request=request, response=response)


def test_quests_surface_404_latches_and_suppresses_further_calls(monkeypatch, caplog):
    """lingo-core in beta mode doesn't mount /quests, so list_quests 404s on
    every call. The first 404 should latch a module flag, log once at INFO
    (not error/exception — no traceback flood), and return []. Every
    subsequent call for the lifetime of the process must short-circuit
    before ever calling the client again.
    """
    monkeypatch.setattr(eval_mod, "_quests_surface_unmounted", False)

    fake = MagicMock()
    fake.list_quests.side_effect = _http_404()
    monkeypatch.setattr(eval_mod, "_client", lambda: fake)

    event = XpAwardedMessage(user_id="u-1", amount=10, source="lesson")

    with caplog.at_level(logging.INFO, logger="lingo_async.quests"):
        actions_first = eval_mod.evaluate_quests_for("u-1", event)

    assert actions_first == []
    assert fake.list_quests.call_count == 1
    info_records = [
        r for r in caplog.records
        if r.levelno == logging.INFO and "quests_surface_not_mounted" in r.message
    ]
    assert len(info_records) == 1
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records == []

    # Second call: no exception raised this time (side_effect stays set),
    # but the latch must skip the client entirely.
    actions_second = eval_mod.evaluate_quests_for("u-1", event)
    assert actions_second == []
    assert fake.list_quests.call_count == 1, "second call must not touch the client"


def test_non_404_error_keeps_raising_and_does_not_latch(monkeypatch):
    """Other status codes (5xx, network errors, etc.) keep today's
    behavior: log an exception and re-raise so the handler retries via
    SQS. Must NOT trip the 404 latch."""
    monkeypatch.setattr(eval_mod, "_quests_surface_unmounted", False)

    fake = MagicMock()
    fake.list_quests.side_effect = RuntimeError("boom")
    monkeypatch.setattr(eval_mod, "_client", lambda: fake)

    event = XpAwardedMessage(user_id="u-1", amount=10, source="lesson")

    with pytest.raises(RuntimeError):
        eval_mod.evaluate_quests_for("u-1", event)

    assert eval_mod._quests_surface_unmounted is False


def test_test_out_lesson_event_skips_quest_evaluation(monkeypatch):
    """A lesson_completed event with is_test_out=True (placement /
    per-module test-out) must not advance lesson-count quests, and must
    not even call the client — mirrors lingo-core skipping XP/lingots for
    the same attempts."""
    fake = MagicMock()
    monkeypatch.setattr(eval_mod, "_client", lambda: fake)

    event = LessonCompletedMessage(
        user_id="u-1", lesson_id="L1", score=1.0, perfect=True,
        attempted_at="2026-05-31T00:00:00Z", is_test_out=True,
    )
    actions = eval_mod.evaluate_quests_for("u-1", event)

    assert actions == []
    fake.list_quests.assert_not_called()
    fake.bump_progress.assert_not_called()


def test_skips_inactive_quests(monkeypatch):
    fake = MagicMock()
    fake.list_quests.return_value = {
        "items": [_quest("q-claimable", "XP", status="claimable")],
    }
    monkeypatch.setattr(eval_mod, "_client", lambda: fake)

    event = XpAwardedMessage(user_id="u-1", amount=10, source="lesson")
    actions = eval_mod.evaluate_quests_for("u-1", event)

    fake.bump_progress.assert_not_called()
    assert actions == []
