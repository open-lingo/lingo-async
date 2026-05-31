"""Handler for ``review_completed`` events.

Quest evaluation only (review-count + review-streak quests). XP from
reviews flows through its own ``xp_awarded`` event from the SRS sync
endpoint, so no leaderboard call here.
"""

from typing import Any

from app.contracts.messages import ReviewCompletedMessage
from app.quests.evaluator import evaluate_quests_for


def handle(event: ReviewCompletedMessage) -> list[dict[str, Any]]:
    evaluate_quests_for(event.user_id, event)
    return []
