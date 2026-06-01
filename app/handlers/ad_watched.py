"""Handler for ``ad_watched`` — fired by lingo-core after a rewarded ad
watch is credited. v1 is intentionally a no-op fanout: lingot crediting
happens inline on the producer side. The event still flows through so
the inspector / event log records it, and so a future quest evaluator
(e.g. "watch 3 ads") has a hook to attach to.
"""

import logging
from typing import Any

from app.contracts.messages import AdWatchedMessage

logger = logging.getLogger("lingo_async.ad_watched")


def handle(event: AdWatchedMessage) -> list[dict[str, Any]]:
    logger.debug(
        "ad_watched user_id=%s placement=%s lingots=%d",
        event.user_id,
        event.placement,
        event.lingots_awarded,
    )
    return [
        {
            "handler": "ad_watched",
            "actions": [
                {
                    "user_id": event.user_id,
                    "placement": event.placement,
                    "lingots_awarded": event.lingots_awarded,
                }
            ],
        }
    ]
