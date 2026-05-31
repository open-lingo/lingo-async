from typing import Any

from app.contracts.messages import SubscriptionChangedMessage


def handle(event: SubscriptionChangedMessage) -> list[dict[str, Any]]:
    # Stub: nothing to do yet. Returns empty so the inspector still
    # records that the event was received + dispatched.
    return []
