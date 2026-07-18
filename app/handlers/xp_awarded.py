"""Handler for ``xp_awarded`` — credit user XP (for synthetic events),
write the leaderboard, evaluate quests.

Returns a list of {handler, actions} dicts capturing what was done. The
dispatch loop merges these into the event-log row's ``outcomes`` column.

Producer split:
  - ``source="lesson"`` (lingo-core /progress/lessons/batch): user.xp is
    credited inline in the producer BEFORE the event publishes, so
    calling ``add_xp`` here would double-count.
  - ``source="manual"`` (lingo-ops admin synthetic events): producer
    skipped user.xp + the leaderboard, so we must hit lingo-core's
    ``/_internal/xp/add`` to credit both.
  - Other future sources (``review`` / ``quest`` / ``streak``): treat
    them like manual until those producers explicitly credit XP at
    publish time.
"""

import logging
from typing import Any

from app.config import settings
from app.contracts.messages import XpAwardedMessage
from app.http.lingo_core_client import LingoCoreClient
from app.leaderboard.updater import update_leaderboard
from app.quests.evaluator import evaluate_quests_for

logger = logging.getLogger("lingo_async.xp_awarded")

# ``lesson`` is the only source whose producer (lingo-core progress
# router) already credits user.xp + writes the leaderboard inline. Every
# other source needs the post-hoc callback.
_PRODUCER_ALREADY_CREDITED: set[str] = {"lesson"}


def handle(event: XpAwardedMessage) -> list[dict[str, Any]]:
    outcomes: list[dict[str, Any]] = []

    if event.source not in _PRODUCER_ALREADY_CREDITED and event.amount > 0:
        # Crediting user.xp is the PRIMARY effect for non-lesson sources — the
        # producer skipped it, so this callback is the only place it happens.
        # A failure here must NOT be swallowed: swallowing lets the dispatch
        # loop ack the message "ok", SQS deletes it, and the user's XP is
        # silently lost with no retry and no DLQ (violates the "handlers raise;
        # the dispatch loop catches" rule). Let it propagate so the message is
        # retried on a transient core blip and DLQ'd on a permanent one.
        #
        # It runs FIRST and short-circuits the rest of the fanout on failure so
        # the leaderboard (non-idempotent ADD) and quest bumps don't fire on
        # this attempt and then fire AGAIN on the retry that credits the XP —
        # i.e. a failed credit costs us a retry, not a double leaderboard/quest.
        try:
            resp = LingoCoreClient().add_xp(
                user_id=event.user_id,
                amount=event.amount,
                learning_language_id=event.learning_language_id,
                leaderboard_opt_in=event.leaderboard_opt_in,
            )
        except Exception as exc:
            logger.warning(
                "user_xp_credit_failed user_id=%s err=%s — failing event for retry",
                event.user_id,
                exc,
            )
            raise
        outcomes.append(
            {
                "handler": "user_xp",
                "actions": [
                    {
                        "user_id": event.user_id,
                        "amount_added": event.amount,
                        "new_xp": resp.get("new_xp"),
                        "source": event.source,
                    }
                ],
            }
        )
    # Leaderboard (DynamoDB direct write). Gated: default-off so the app can
    # ship without leaderboards incurring write cost. Quest eval + XP crediting
    # below/above are NOT gated — they must keep working.
    if settings.LEADERBOARD_ENABLED:
        lb_actions = update_leaderboard(event)
        outcomes.append({"handler": "leaderboard", "actions": lb_actions})
    else:
        outcomes.append(
            {"handler": "leaderboard", "actions": [{"skipped": "leaderboard_disabled"}]}
        )
    q_actions = evaluate_quests_for(event.user_id, event)
    outcomes.append({"handler": "quest_eval", "actions": q_actions})
    return outcomes
