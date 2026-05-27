"""Handler for ``xp_awarded`` events.

Two side-effects:
  1. Update the leaderboard (real — see ``app.leaderboard.updater``).
  2. Evaluate quests (stub — see ``app.quests.evaluator``).

Both run unconditionally. The dispatch loop will catch + report any
exception so SQS retries this specific message; we deliberately do NOT
swallow inside the handler so retryable Dynamo failures bubble up.
"""

from app.contracts.messages import XpAwardedMessage
from app.leaderboard.updater import update_leaderboard
from app.quests.evaluator import evaluate_quests_for


def handle(event: XpAwardedMessage) -> None:
    update_leaderboard(event)
    evaluate_quests_for(event.user_id, event)
