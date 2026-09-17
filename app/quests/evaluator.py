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

import httpx

from app.contracts.messages import (
    EventMessage,
    FriendAddedMessage,
    LessonCompletedMessage,
    ReviewCompletedMessage,
    XpAwardedMessage,
)
from app.http.lingo_core_client import LingoCoreClient

logger = logging.getLogger("lingo_async.quests")

# Latches True the first time list_quests() 404s — lingo-core in
# SURFACE_MODE=beta doesn't mount the quests router
# (`_BETA_GROUPS = {"boot","users","srs","progress"}` in its
# app/v1/router.py), so until that changes every single event hits this.
# Without the latch, evaluate_quests_for calls core on every event, gets
# a 404 every time, and logger.exception on the bare `raise` below floods
# the logs with a full traceback per event (1,378 in 50 minutes on
# 2026-09-16). Once tripped we stop calling core for the life of this
# Lambda instance — cheap, and correct because the surface doesn't come
# back without a redeploy, which recycles the container anyway.
_quests_surface_unmounted = False


def _client() -> LingoCoreClient:
    """Indirection so tests can monkeypatch."""
    return LingoCoreClient()


def _event_unit_and_delta(event: EventMessage) -> tuple[str, int] | None:
    """Return (progress_unit, delta) for events that advance quests, else None.

    Batched events (currently just ``review_completed``) carry their delta
    in ``count``; per-item events default to 1.
    """
    if isinstance(event, XpAwardedMessage):
        return ("XP", event.amount)
    if isinstance(event, LessonCompletedMessage):
        if event.is_test_out:
            # Placement / per-module test-out — lingo-core already skips
            # XP/lingots for these; don't advance lesson-count quests
            # either (a placement run can fire dozens in one batch).
            return None
        return ("lessons", 1)
    if isinstance(event, ReviewCompletedMessage):
        return ("cards", event.count)
    if isinstance(event, FriendAddedMessage):
        return ("friends", 1)
    return None


def evaluate_quests_for(user_id: str, event: EventMessage) -> list[dict[str, Any]]:
    global _quests_surface_unmounted

    pair = _event_unit_and_delta(event)
    if pair is None:
        return []
    target_unit, delta = pair
    if delta <= 0:
        return []

    if _quests_surface_unmounted:
        # Already confirmed core doesn't mount quests this process —
        # don't even try.
        return []

    client = _client()
    try:
        listed = client.list_quests(user_id)
    except Exception as exc:
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404:
            _quests_surface_unmounted = True
            logger.info(
                "quests_surface_not_mounted user_id=%s — lingo-core "
                "returned 404 for list_quests (beta mode doesn't mount "
                "/quests); suppressing further quest evaluation for the "
                "life of this process",
                user_id,
            )
            return []
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
