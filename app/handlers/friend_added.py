"""Handler for ``friend_added`` events.

Quest evaluation only (social quests — "add your first friend",
"reach 5 friends", etc.). The producer fires ONE event per direction
of the edge (or two for the reciprocal case); we evaluate against the
``user_id`` on the message only.
"""

from app.contracts.messages import FriendAddedMessage
from app.quests.evaluator import evaluate_quests_for


def handle(event: FriendAddedMessage) -> None:
    evaluate_quests_for(event.user_id, event)
