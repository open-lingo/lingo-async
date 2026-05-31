from typing import Any

from app.contracts.messages import ReviewCompletedMessage
from app.quests.evaluator import evaluate_quests_for


def handle(event: ReviewCompletedMessage) -> list[dict[str, Any]]:
    actions = evaluate_quests_for(event.user_id, event)
    return [{"handler": "quest_eval", "actions": actions}]
