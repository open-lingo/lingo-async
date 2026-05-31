"""Quest progress evaluator.

For each incoming event:
  1. List the user's quests via the lingo-core internal API.
  2. Match by progress_unit (XP / lessons / cards / friends).
  3. Bump each matching active quest by the appropriate delta.
  4. Return action records for the event-log outcomes.

Errors from the HTTP layer bubble up — the handler dispatch loop catches
them and marks the event "failed" + retries via SQS.
"""

import logging
from typing import Any

from app.contracts.messages import (
    EventMessage,
    FriendAddedMessage,
    LessonCompletedMessage,
    ReviewCompletedMessage,
    XpAwardedMessage,
)
from app.http.lingo_core_client import LingoCoreClient

logger = logging.getLogger("lingo_async.quests")


def _client() -> LingoCoreClient:
    """Indirection so tests can monkeypatch."""
    return LingoCoreClient()


def _event_unit_and_delta(event: EventMessage) -> tuple[str, int] | None:
    """Return (progress_unit, delta) for events that advance quests, else None."""
    if isinstance(event, XpAwardedMessage):
        return ("XP", event.amount)
    if isinstance(event, LessonCompletedMessage):
        return ("lessons", 1)
    if isinstance(event, ReviewCompletedMessage):
        return ("cards", 1)
    if isinstance(event, FriendAddedMessage):
        return ("friends", 1)
    return None


def evaluate_quests_for(user_id: str, event: EventMessage) -> list[dict[str, Any]]:
    pair = _event_unit_and_delta(event)
    if pair is None:
        return []
    target_unit, delta = pair
    if delta <= 0:
        return []

    client = _client()
    try:
        listed = client.list_quests(user_id)
    except Exception:
        logger.exception("quests_list_failed user_id=%s", user_id)
        raise

    actions: list[dict[str, Any]] = []
    for quest in listed.get("items", []) or []:
        if quest.get("status") != "active":
            continue
        progress = quest.get("progress") or {}
        if progress.get("unit") != target_unit:
            continue
        quest_id = quest["id"]
        before = int(progress.get("current") or 0)
        status_before = quest.get("status", "active")

        updated = client.bump_progress(quest_id, user_id=user_id, delta=delta)
        new_progress = updated.get("progress") or {}
        after = int(new_progress.get("current") or 0)
        status_after = updated.get("status", status_before)

        actions.append({
            "quest_id": quest_id,
            "unit": target_unit,
            "delta": delta,
            "progress_before": before,
            "progress_after": after,
            "status_before": status_before,
            "status_after": status_after,
        })
        logger.info(
            "quest_advanced user_id=%s quest_id=%s unit=%s delta=%d %s→%s",
            user_id, quest_id, target_unit, delta, before, after,
        )

    return actions
