"""Handler for ``xp_awarded`` — leaderboard update + quest evaluation.

Returns a list of {handler, actions} dicts capturing what was done.
The dispatch loop merges these into the event-log row's ``outcomes``
column so the inspector can show "event X advanced quest Y, wrote
leaderboard bucket Z".

NOTE: Today's wrapper returns ``[]`` because the called helpers still
return ``None``. Tasks 9 + 10 rewrite the helpers to return action lists;
this wrapper will then collect and return them.
"""

from typing import Any

from app.contracts.messages import XpAwardedMessage
from app.leaderboard.updater import update_leaderboard
from app.quests.evaluator import evaluate_quests_for


def handle(event: XpAwardedMessage) -> list[dict[str, Any]]:
    update_leaderboard(event)
    evaluate_quests_for(event.user_id, event)
    return []
