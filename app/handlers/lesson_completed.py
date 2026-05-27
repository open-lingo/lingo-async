"""Handler for ``lesson_completed`` events.

Only side-effect today is quest evaluation (lesson-count quests).
The XP earned for a lesson arrives as a separate ``xp_awarded`` event
from the same producer batch, so leaderboard updates happen there —
NOT here.
"""

from app.contracts.messages import LessonCompletedMessage
from app.quests.evaluator import evaluate_quests_for


def handle(event: LessonCompletedMessage) -> None:
    evaluate_quests_for(event.user_id, event)
