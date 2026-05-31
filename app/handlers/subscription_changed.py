"""Handler for ``subscription_changed`` events.

Stub for now. Future home for:
  - Premium-badge fan-out (write a flag onto the user row + invalidate
    profile caches).
  - Churn-watch alerting (notify ops / fire a re-engagement email when
    a paying tier downgrades or churns).
  - Lifetime-grant accounting (track entitlements separately from
    recurring tiers).

We log + no-op so producers can start firing the event before the real
handler lands without messages piling up in the DLQ.
"""

import logging
from typing import Any

from app.contracts.messages import SubscriptionChangedMessage

logger = logging.getLogger("lingo_async.handlers.subscription_changed")


def handle(event: SubscriptionChangedMessage) -> list[dict[str, Any]]:
    logger.info(
        "subscription_changed_stub user_id=%s tier=%s event=%s",
        event.user_id,
        event.tier,
        event.event,
    )
    # Stub: nothing to do yet. Returns empty so the inspector still
    # records that the event was received + dispatched.
    return []
