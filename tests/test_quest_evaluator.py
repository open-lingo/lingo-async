"""Quest evaluator — event → quest match + REST callback."""

from unittest.mock import MagicMock

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
