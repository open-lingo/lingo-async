from unittest.mock import MagicMock

import pytest

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


def _manual_event() -> XpAwardedMessage:
    # source="manual" (lingo-ops synthetic event) ⇒ handler MUST credit XP via
    # the /_internal/xp/add callback (the producer skipped it).
    return XpAwardedMessage(
        user_id="u-1", amount=10, source="manual",
        learning_language_id="ja", leaderboard_opt_in=True,
    )


def test_manual_source_credits_xp_then_fans_out(monkeypatch):
    monkeypatch.setattr(settings, "LEADERBOARD_ENABLED", True)
    client = MagicMock()
    client.add_xp.return_value = {"ok": True, "xp_added": 10, "new_xp": 110}
    monkeypatch.setattr(xp_awarded, "LingoCoreClient", lambda: client)
    monkeypatch.setattr(xp_awarded, "update_leaderboard", MagicMock(return_value=[]))
    monkeypatch.setattr(xp_awarded, "evaluate_quests_for", MagicMock(return_value=[]))

    outcomes = xp_awarded.handle(_manual_event())

    client.add_xp.assert_called_once_with(
        user_id="u-1", amount=10, learning_language_id="ja", leaderboard_opt_in=True
    )
    xp_outcome = next(o for o in outcomes if o["handler"] == "user_xp")
    assert xp_outcome["actions"][0]["new_xp"] == 110


def test_add_xp_failure_propagates_and_halts_fanout(monkeypatch):
    """A non-lesson XP credit that fails must NOT be swallowed.

    Swallowing acks the SQS message as success — SQS deletes it and the user's
    XP is silently lost with no retry, no DLQ (violates the "handlers raise;
    the dispatch loop catches" rule). The failure has to propagate so the
    message is retried / DLQ'd. It also must short-circuit BEFORE the
    leaderboard / quest side effects fire, so those don't double-count on the
    retry that finally credits the XP.
    """
    monkeypatch.setattr(settings, "LEADERBOARD_ENABLED", True)
    client = MagicMock()
    client.add_xp.side_effect = RuntimeError("core down")
    monkeypatch.setattr(xp_awarded, "LingoCoreClient", lambda: client)
    lb_mock = MagicMock()
    quests_mock = MagicMock()
    monkeypatch.setattr(xp_awarded, "update_leaderboard", lb_mock)
    monkeypatch.setattr(xp_awarded, "evaluate_quests_for", quests_mock)

    with pytest.raises(RuntimeError):
        xp_awarded.handle(_manual_event())

    lb_mock.assert_not_called()
    quests_mock.assert_not_called()


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
