"""Quest progress evaluator — stub.

The real schema lives in lingo-core (`app/quests/`); pulling the
``QuestRepository`` protocol over here is the next step. Until that
lands, this module logs the event and no-ops so the dispatch loop still
exercises the right shape.

Contract (future):
  1. Look up the user's active quests via the ``lingo_quests`` table.
  2. For each, check whether ``event`` advances progress (e.g. an
     ``xp_awarded`` advances any "earn N XP today" quest; a
     ``lesson_completed`` advances "complete N lessons today" plus
     "complete N lessons in language X" once language is on the
     message).
  3. Persist updated progress (atomic UpdateItem ADD on the quest row).
  4. Fire a follow-up ``quest_completed`` event when a quest crosses
     its target — handled by another consumer (not yet built) that
     awards rewards.

Tests in `tests/test_quest_evaluator.py` cover the stub's shape. When
the real impl lands, add per-quest-type cases there.
"""

import logging

from app.contracts.messages import EventMessage

logger = logging.getLogger("lingo_async.quests")


def evaluate_quests_for(user_id: str, event: EventMessage) -> None:
    """Evaluate quest progress for ``user_id`` against ``event``.

    Stub: logs the event type and returns. Synchronous because the eventual
    real impl will be one Query + N UpdateItem calls, all sync boto3.
    """
    logger.info(
        "quest_eval_stub user_id=%s event_type=%s",
        user_id,
        event.type,
    )
