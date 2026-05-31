"""Write-side protocol for the event-log store.

Reads are intentionally NOT on this protocol — the inspector reads happen
in lingo-ops, which has its own `EventsReadRepository`. Splitting the
read + write protocols keeps the lingo-async writer focused (it never
queries) and makes the prod Dynamo swap a single-class implementation.
"""

from typing import Protocol


class EventsWriteRepository(Protocol):
    def save(
        self,
        *,
        id: str,
        user_id: str,
        event_type: str,
        payload_json: str,
        received_at: str,
        ttl_epoch: int,
    ) -> None: ...

    def update_status(
        self,
        *,
        id: str,
        status: str,
        error_msg: str | None,
        outcomes_json: str,
    ) -> None: ...
