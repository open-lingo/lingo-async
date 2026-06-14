"""DynamoDB write-side impl for the event log.

The SQLite repo (``app/db/sqlite/events.py``) is the source of truth for
behavior; this mirrors its two methods so the provider can swap backends
by flipping ``EVENT_LOG_BACKEND``.

Schema (mirrored by ``aws_dynamodb_table.events_log`` in lingo-infra):

  PK = "USER#<user_id>"            partition per user — the read side
                                   (lingo-ops inspector) lists a user's
                                   timeline newest-first.
  SK = "EVENT#<received_at>#<id>"  received_at-ordered within the user.
  GSI "EventId-Index": hash = id   point-lookup for update_status() and the
                                   inspector's get_event(id) — neither knows
                                   the (user_id, received_at) key.
  TTL attribute: ttl_epoch         Dynamo's free sweep purges old rows.

``update_status`` only receives ``id`` (the SQS messageId), so it first
resolves the base key via the GSI, then UpdateItems the base item. At our
volume (one query + one update per processed message) this is fine; the
``<unparsed>`` save path still creates a base item so the follow-up update
always finds a row.

boto3 (sync) per the worker's convention — no async to exploit on a
per-message Lambda. Uses the shared module-level resource from
``dynamo_client`` so the client is built once per cold start.
"""

from boto3.dynamodb.conditions import Key

from app.config import settings
from app.db.dynamo_client import get_table

_GSI_NAME = "EventId-Index"


def _events_table_name() -> str:
    return f"{settings.DYNAMODB_TABLE_PREFIX}events_log"


def _pk(user_id: str) -> str:
    return f"USER#{user_id}"


def _sk(received_at: str, id: str) -> str:
    return f"EVENT#{received_at}#{id}"


class DynamoEventsWriteRepository:
    def __init__(self) -> None:
        self._table = get_table(_events_table_name())

    def save(
        self,
        *,
        id: str,
        user_id: str,
        event_type: str,
        payload_json: str,
        received_at: str,
        ttl_epoch: int,
    ) -> None:
        self._table.put_item(
            Item={
                "PK": _pk(user_id),
                "SK": _sk(received_at, id),
                "id": id,
                "user_id": user_id,
                "event_type": event_type,
                "payload_json": payload_json,
                "received_at": received_at,
                "status": "received",
                "outcomes_json": "[]",
                "ttl_epoch": ttl_epoch,
            }
        )

    def update_status(
        self,
        *,
        id: str,
        status: str,
        error_msg: str | None,
        outcomes_json: str,
    ) -> None:
        # Resolve the base (PK, SK) via the id GSI — update_status only has
        # the messageId. save() always runs first for the same id, so the
        # row exists; if a race or a dropped save() means it doesn't, there's
        # nothing to update and we no-op rather than raise (the dispatch loop
        # already recorded the failure for DLQ).
        resp = self._table.query(
            IndexName=_GSI_NAME,
            KeyConditionExpression=Key("id").eq(id),
            Limit=1,
        )
        items = resp.get("Items", [])
        if not items:
            return
        base = items[0]
        self._table.update_item(
            Key={"PK": base["PK"], "SK": base["SK"]},
            UpdateExpression="SET #s = :status, outcomes_json = :outcomes, error_msg = :err",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":status": status,
                ":outcomes": outcomes_json,
                ":err": error_msg,
            },
        )
