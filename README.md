# lingo-async

SQS-driven Lambda worker for Open Lingo. Consumes events published by
`lingo-core` (and later `lingo-ops`) on the `lingo-events` SQS queue,
dispatches them to per-event handlers, and runs side-effects: quest
progress evaluation, leaderboard XP updates, and (future) activity-feed
fan-out.

No HTTP. No API Gateway. The Lambda is triggered directly by SQS via an
event source mapping.

For the *why* and the deeper architecture, read:

- [`CLAUDE.md`](./CLAUDE.md) — repo orientation, conventions
- [`DEPLOY.md`](./DEPLOY.md) — build + deploy runbook
- `../lingo-infra/main.tf` — SQS queues, DLQ, Lambda, IAM, event source
  mapping

## Run locally

```bash
pip install -e ".[dev]"
cp .env.example .env       # then edit if you need a non-default prefix
pytest -q                  # the only local validation; there's no server
```

There's no `uvicorn` / no port. The way to exercise a handler locally
is via pytest with a hand-crafted SQS event payload — see
`tests/test_handler.py`.

## Test

```bash
pytest -q
ruff check .
ruff format .
```

## Message contracts

All messages share two envelope fields and a discriminator:

| Field     | Type                  | Notes                                 |
|-----------|-----------------------|---------------------------------------|
| `type`    | string literal        | Discriminator. See table below.       |
| `version` | `Literal[1]`          | Bump (new branch) on contract change. |

Producers serialise to JSON and call `sqs.send_message(MessageBody=...)`
on the `lingo-events` queue. Consumers parse via the
`EventMessage` discriminated union in `app/contracts/messages.py`.

### Supported types

| `type`                | Payload fields                                                                                                                                |
|-----------------------|-----------------------------------------------------------------------------------------------------------------------------------------------|
| `xp_awarded`          | `user_id: str`, `amount: int`, `source: "lesson"\|"review"\|"manual"\|"quest"\|"streak"`                                                       |
| `lesson_completed`    | `user_id: str`, `lesson_id: str`, `score: float`, `perfect: bool`, `attempted_at: datetime` (ISO 8601)                                         |
| `review_completed`    | `user_id: str`, `card_id: str`, `modality: "recognition"\|"production"`, `rating: "again"\|"hard"\|"good"\|"easy"`                              |
| `friend_added`        | `user_id: str`, `friend_id: str`                                                                                                              |
| `subscription_changed`| `user_id: str`, `tier: "free"\|"supporter"\|"patron"\|"lifetime"`, `event: "new"\|"upgraded"\|"downgraded"\|"churned"`                          |

### Example — `xp_awarded`

```json
{
  "type": "xp_awarded",
  "version": 1,
  "user_id": "u-abc-123",
  "amount": 15,
  "source": "lesson"
}
```

### Example — `lesson_completed`

```json
{
  "type": "lesson_completed",
  "version": 1,
  "user_id": "u-abc-123",
  "lesson_id": "ja-greetings-01",
  "score": 0.92,
  "perfect": false,
  "attempted_at": "2026-05-27T18:34:11+00:00"
}
```

### Producer reference

Producers should:
1. Construct an `EventMessage` Pydantic instance (or matching dict).
2. Serialise with `model_dump_json()` (or `json.dumps`).
3. Call `sqs.send_message(QueueUrl=os.environ["EVENTS_QUEUE_URL"],
   MessageBody=body)`.
4. Catch + log on failure. Producers MUST NOT block the primary
   request on SQS publish.

The minimum producer wiring is in
`../lingo-core/app/events/publisher.py`.

## Status

- Handlers: stubs except `xp_awarded` → leaderboard updater (real).
- Quest evaluator: stub (logs + no-ops; needs `lingo_quests` schema).
- DLQ tooling: none yet — add `scripts/dlq-replay.py` when the first
  DLQ message lands.
- Metrics: CloudWatch Logs only.
