"""HTTP client for callbacks into lingo-core.

Single-shot calls, short timeout. No connection pool — the worker batch
size is small (≤10) and each call is independent. If volume ever spikes
to the point this matters, switch to a module-level ``httpx.Client``.
"""

import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger("lingo_async.http")

_TIMEOUT = 5.0


class LingoCoreClient:
    def __init__(self) -> None:
        self._base = settings.LINGO_CORE_URL.rstrip("/")
        self._headers = {"Authorization": f"Bearer {settings.INTERNAL_SERVICE_TOKEN}"}

    def list_quests(self, user_id: str) -> dict[str, Any]:
        client = httpx.Client()
        try:
            resp = client.get(
                f"{self._base}/api/core/v1/quests/_internal/list",
                params={"user_id": user_id},
                headers=self._headers,
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            return resp.json()
        finally:
            client.close()

    def bump_progress(self, quest_id: str, *, user_id: str, delta: int) -> dict[str, Any]:
        client = httpx.Client()
        try:
            resp = client.post(
                f"{self._base}/api/core/v1/quests/_internal/{quest_id}/progress",
                json={"user_id": user_id, "delta": delta},
                headers=self._headers,
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            return resp.json()
        finally:
            client.close()

    def add_xp(
        self,
        *,
        user_id: str,
        amount: int,
        learning_language_id: str | None,
        leaderboard_opt_in: bool,
    ) -> dict[str, Any]:
        """Credit XP + write to the leaderboard via lingo-core. Used by
        the xp_awarded handler for events that came from a *non-lingo-core*
        producer (today: synthetic admin events from lingo-ops). Real
        lesson XP is credited inline by lingo-core's progress router
        before the event publishes, so we'd be double-counting if we
        called this for those — see the source-based gate in the handler.
        """
        client = httpx.Client()
        try:
            resp = client.post(
                f"{self._base}/api/core/v1/users/_internal/xp/add",
                json={
                    "user_id": user_id,
                    "amount": amount,
                    "learning_language_id": learning_language_id,
                    "leaderboard_opt_in": leaderboard_opt_in,
                },
                headers=self._headers,
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            return resp.json()
        finally:
            client.close()
