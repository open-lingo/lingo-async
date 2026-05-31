"""Handler for ``xp_awarded`` — leaderboard update + quest evaluation.

Returns a list of {handler, actions} dicts capturing what was done. The
dispatch loop merges these into the event-log row's ``outcomes`` column.
"""

from typing import Any

from app.contracts.messages import XpAwardedMessage
from app.leaderboard.updater import update_leaderboard
from app.quests.evaluator import evaluate_quests_for


def handle(event: XpAwardedMessage) -> list[dict[str, Any]]:
    outcomes: list[dict[str, Any]] = []
    lb_actions = update_leaderboard(event)
    outcomes.append({"handler": "leaderboard", "actions": lb_actions})
    q_actions = evaluate_quests_for(event.user_id, event)
    outcomes.append({"handler": "quest_eval", "actions": q_actions})
    return outcomes
