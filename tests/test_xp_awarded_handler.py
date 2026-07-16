from unittest.mock import MagicMock

from app.config import settings
from app.contracts.messages import XpAwardedMessage
from app.handlers import xp_awarded


def _lesson_event() -> XpAwardedMessage:
    # source="lesson" ⇒ handler skips the add_xp callback (producer already
    # credited), so no LingoCoreClient mock is needed.
    return XpAwardedMessage(
        user_id="u-1", amount=10, source="lesson",
        learning_language_id="ja", leaderboard_opt_in=True,
    )


def test_leaderboard_skipped_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "LEADERBOARD_ENABLED", False)
    lb_mock = MagicMock()
    quests_mock = MagicMock(return_value=[{"quest_id": "q1"}])
    monkeypatch.setattr(xp_awarded, "update_leaderboard", lb_mock)
    monkeypatch.setattr(xp_awarded, "evaluate_quests_for", quests_mock)

    outcomes = xp_awarded.handle(_lesson_event())

    lb_mock.assert_not_called()
    quests_mock.assert_called_once()
    lb_outcome = next(o for o in outcomes if o["handler"] == "leaderboard")
    assert lb_outcome["actions"] == [{"skipped": "leaderboard_disabled"}]
    assert any(o["handler"] == "quest_eval" for o in outcomes)


def test_leaderboard_written_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "LEADERBOARD_ENABLED", True)
    lb_mock = MagicMock(return_value=[{"scope": "weekly", "xp_added": 10}])
    quests_mock = MagicMock(return_value=[])
    monkeypatch.setattr(xp_awarded, "update_leaderboard", lb_mock)
    monkeypatch.setattr(xp_awarded, "evaluate_quests_for", quests_mock)

    outcomes = xp_awarded.handle(_lesson_event())

    lb_mock.assert_called_once()
    quests_mock.assert_called_once()
    lb_outcome = next(o for o in outcomes if o["handler"] == "leaderboard")
    assert lb_outcome["actions"] == [{"scope": "weekly", "xp_added": 10}]
