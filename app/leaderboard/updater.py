"""Leaderboard updater — writes XP increments to lingo_social_leaderboard.

Bucket scheme (matches lingo-infra/main.tf social_leaderboard comments):
  weekly:  ``{lang}#{YYYY}-W{ww}``    e.g. ``ja#2026-W21``
  monthly: ``{lang}#{YYYY}-{MM}``     e.g. ``ja#2026-05``

Key layout:
  ``PK = BUCKET#<bucket_str>     SK = USER#<user_id>``

Each XP-awarded event fires TWO UpdateItem calls — one for the user's
weekly bucket and one for the monthly bucket. Both use atomic ``ADD xp
:n`` so duplicate deliveries from SQS (which guarantees at-least-once)
over-count; we accept that for now (see CLAUDE.md § Patterns).

The TTL attribute is set so old buckets self-purge per
[[project-social-design]] (30-day retention on inactivity). We set
``ttl = bucket_end + 30 days`` epoch seconds.

User language lookup: we read the user's ``learning`` settings from the
``lingo_users`` table item (PK=USER#<id>, SK=PROFILE) and pick out
``learningLanguageId``. If the user has opted out of the leaderboard
(``settings.social.show_on_leaderboard == False``) we skip the write —
the producer is supposed to gate on this too, but a defence-in-depth
check here protects against a producer regression.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from botocore.exceptions import ClientError

from app.config import social_leaderboard_table_name, users_table_name
from app.contracts.messages import XpAwardedMessage
from app.db.dynamo_client import get_table

logger = logging.getLogger("lingo_async.leaderboard")

# 30-day TTL grace per the social-design memo. Long enough that a user
# coming back two weeks after the bucket closed can still see their
# old standing if we add such a UI; short enough to keep the table cool.
_TTL_GRACE = timedelta(days=30)


def update_leaderboard(event: XpAwardedMessage) -> None:
    """Apply ``event.amount`` to the user's weekly + monthly buckets.

    Reads the user row to discover ``learningLanguageId`` and the opt-in
    flag, then issues two UpdateItem calls. Logs + swallows on missing
    user / opted-out / language unset — these are not retryable errors.
    Re-raises on transient Dynamo failures so SQS retries the message.
    """
    if event.amount <= 0:
        return

    user_info = _load_user_leaderboard_info(event.user_id)
    if user_info is None:
        logger.info("leaderboard_skip user_id=%s reason=user_not_found", event.user_id)
        return

    lang, opted_in = user_info
    if not opted_in:
        logger.debug("leaderboard_skip user_id=%s reason=opted_out", event.user_id)
        return
    if not lang:
        logger.debug("leaderboard_skip user_id=%s reason=no_language", event.user_id)
        return

    now = datetime.now(UTC)
    weekly_bucket, weekly_ttl = _weekly_bucket(lang, now)
    monthly_bucket, monthly_ttl = _monthly_bucket(lang, now)

    _add_xp(weekly_bucket, event.user_id, event.amount, weekly_ttl)
    _add_xp(monthly_bucket, event.user_id, event.amount, monthly_ttl)

    logger.info(
        "leaderboard_add user_id=%s lang=%s amount=%d buckets=[%s,%s]",
        event.user_id,
        lang,
        event.amount,
        weekly_bucket,
        monthly_bucket,
    )


def _load_user_leaderboard_info(user_id: str) -> tuple[str, bool] | None:
    """Return (learning_language_id, opted_in_to_leaderboard) for the user, or
    None if the user row doesn't exist.

    The lingo_users table is a single-table layout (see lingo-core
    `app/db/dynamo/user.py`). The profile row is PK=USER#<id>, SK=PROFILE
    and carries the ``settings`` blob inline. We only need two paths out
    of it.
    """
    table = get_table(users_table_name())
    try:
        resp = table.get_item(
            Key={"PK": f"USER#{user_id}", "SK": "PROFILE"},
            ProjectionExpression="settings",
        )
    except ClientError as exc:
        # Surface as retryable — transient throttles fit here too.
        logger.warning("leaderboard_user_read_failed user_id=%s err=%s", user_id, exc)
        raise

    item: dict[str, Any] | None = resp.get("Item")
    if not item:
        return None

    settings_blob = item.get("settings") or {}
    learning = settings_blob.get("learning") or {}
    social = settings_blob.get("social") or {}

    lang = learning.get("learningLanguageId") or settings_blob.get("learningLanguage") or ""
    opted_in = bool(social.get("show_on_leaderboard"))
    return str(lang), opted_in


def _weekly_bucket(lang: str, now: datetime) -> tuple[str, int]:
    """Return (bucket_str, ttl_epoch) for the ISO week containing ``now``.

    ISO week format ``YYYY-Www`` (e.g. 2026-W21). End-of-week = the
    Sunday at 23:59:59 UTC of the ISO week; TTL = end-of-week +
    ``_TTL_GRACE``.
    """
    iso = now.isocalendar()
    bucket = f"{lang}#{iso.year:04d}-W{iso.week:02d}"
    # Monday of the ISO week:
    monday = datetime.fromisocalendar(iso.year, iso.week, 1).replace(tzinfo=UTC)
    end_of_week = monday + timedelta(days=7) - timedelta(seconds=1)
    ttl = int((end_of_week + _TTL_GRACE).timestamp())
    return bucket, ttl


def _monthly_bucket(lang: str, now: datetime) -> tuple[str, int]:
    """Return (bucket_str, ttl_epoch) for the calendar month containing
    ``now``."""
    bucket = f"{lang}#{now.year:04d}-{now.month:02d}"
    if now.month == 12:
        first_of_next = datetime(now.year + 1, 1, 1, tzinfo=UTC)
    else:
        first_of_next = datetime(now.year, now.month + 1, 1, tzinfo=UTC)
    end_of_month = first_of_next - timedelta(seconds=1)
    ttl = int((end_of_month + _TTL_GRACE).timestamp())
    return bucket, ttl


def _add_xp(bucket: str, user_id: str, amount: int, ttl_epoch: int) -> None:
    """UpdateItem ADD xp +n, SET ttl on the leaderboard row."""
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
            "leaderboard_write_failed bucket=%s user_id=%s err=%s", bucket, user_id, exc
        )
        raise
