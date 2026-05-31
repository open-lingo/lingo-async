"""Leaderboard updater — writes XP increments to lingo_social_leaderboard.

Reads ``learning_language_id`` + ``leaderboard_opt_in`` from the event
itself (the producer fills both at publish time), so no per-event user
lookup is needed. This matches the "events are self-contained" model.

Bucket scheme:
  weekly:  ``{lang}#{YYYY}-W{ww}``
  monthly: ``{lang}#{YYYY}-{MM}``

Returns a list of action dicts capturing which buckets were written;
the dispatch loop merges these into the event-log outcomes.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from botocore.exceptions import ClientError

from app.config import social_leaderboard_table_name
from app.contracts.messages import XpAwardedMessage
from app.db.dynamo_client import get_table

logger = logging.getLogger("lingo_async.leaderboard")

_TTL_GRACE = timedelta(days=30)


def _now() -> datetime:
    """Indirection so tests can fix the clock."""
    return datetime.now(UTC)


def update_leaderboard(event: XpAwardedMessage) -> list[dict[str, Any]]:
    if event.amount <= 0:
        return []
    if not event.leaderboard_opt_in:
        logger.debug("leaderboard_skip user_id=%s reason=opted_out", event.user_id)
        return []
    lang = (event.learning_language_id or "").strip()
    if not lang:
        logger.debug("leaderboard_skip user_id=%s reason=no_language", event.user_id)
        return []

    now = _now()
    weekly_bucket, weekly_ttl = _weekly_bucket(lang, now)
    monthly_bucket, monthly_ttl = _monthly_bucket(lang, now)

    actions: list[dict[str, Any]] = []
    _add_xp(weekly_bucket, event.user_id, event.amount, weekly_ttl)
    actions.append({
        "bucket": weekly_bucket, "xp_added": event.amount, "scope": "weekly",
    })
    _add_xp(monthly_bucket, event.user_id, event.amount, monthly_ttl)
    actions.append({
        "bucket": monthly_bucket, "xp_added": event.amount, "scope": "monthly",
    })

    logger.info(
        "leaderboard_add user_id=%s lang=%s amount=%d buckets=[%s,%s]",
        event.user_id, lang, event.amount, weekly_bucket, monthly_bucket,
    )
    return actions


def _weekly_bucket(lang: str, now: datetime) -> tuple[str, int]:
    iso = now.isocalendar()
    bucket = f"{lang}#{iso.year:04d}-W{iso.week:02d}"
    monday = datetime.fromisocalendar(iso.year, iso.week, 1).replace(tzinfo=UTC)
    end_of_week = monday + timedelta(days=7) - timedelta(seconds=1)
    return bucket, int((end_of_week + _TTL_GRACE).timestamp())


def _monthly_bucket(lang: str, now: datetime) -> tuple[str, int]:
    bucket = f"{lang}#{now.year:04d}-{now.month:02d}"
    if now.month == 12:
        first_of_next = datetime(now.year + 1, 1, 1, tzinfo=UTC)
    else:
        first_of_next = datetime(now.year, now.month + 1, 1, tzinfo=UTC)
    end_of_month = first_of_next - timedelta(seconds=1)
    return bucket, int((end_of_month + _TTL_GRACE).timestamp())


def _add_xp(bucket: str, user_id: str, amount: int, ttl_epoch: int) -> None:
    table = get_table(social_leaderboard_table_name())
    try:
        table.update_item(
            Key={"PK": f"BUCKET#{bucket}", "SK": f"USER#{user_id}"},
            UpdateExpression="ADD xp :amount SET #ttl = :ttl",
            ExpressionAttributeNames={"#ttl": "ttl"},
            ExpressionAttributeValues={":amount": amount, ":ttl": ttl_epoch},
        )
    except ClientError as exc:
        logger.warning(
            "leaderboard_write_failed bucket=%s user_id=%s err=%s",
            bucket, user_id, exc,
        )
        raise
