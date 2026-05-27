"""Stub-shape tests for the quest evaluator.

When the real implementation lands, replace these with per-quest-type
assertions. For now we only verify the stub accepts the right shape and
doesn't raise.
"""

from __future__ import annotations

import logging

from app.contracts.messages import (
    FriendAddedMessage,
    LessonCompletedMessage,
    XpAwardedMessage,
)
from app.quests.evaluator import evaluate_quests_for


def test_stub_accepts_xp_awarded_event() -> None:
    event = XpAwardedMessage(user_id="u", amount=1, source="manual")
    evaluate_quests_for(event.user_id, event)  # no raise


def test_stub_accepts_lesson_completed_event() -> None:
    event = LessonCompletedMessage(
        user_id="u",
        lesson_id="l",
        score=0.5,
        perfect=False,
        attempted_at=__import__("datetime").datetime(2026, 5, 27, tzinfo=__import__("datetime").UTC),
    )
    evaluate_quests_for(event.user_id, event)


def test_stub_logs_event_type(caplog) -> None:
    event = FriendAddedMessage(user_id="u-1", friend_id="u-2")
    with caplog.at_level(logging.INFO, logger="lingo_async.quests"):
        evaluate_quests_for(event.user_id, event)
    assert any("quest_eval_stub" in r.message for r in caplog.records)
    assert any("event_type=friend_added" in r.message for r in caplog.records)
