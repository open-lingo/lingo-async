# CLAUDE.md — `lingo-async` (Open Lingo async event worker)

SQS-driven AWS Lambda worker. No HTTP. No API Gateway. Receives events
published by `lingo-core` (and later `lingo-ops`) on the `lingo-events`
queue, dispatches to per-event handlers, and applies side-effects
(quest progress, leaderboard updates, future activity-feed fan-out).

## Why this is a separate repo (2026-05-27)

- **Different invocation model** — event-driven, not request/response.
  Bundling a worker into the user-facing Lambda would push request
  latency through SQS regardless of caller intent.
- **Different scaling profile** — bursty (lesson-completion fan-in at
  peak hours), batchable (SQS event source mapping pulls up to 10 at a
  time). The user-facing Lambda would need different concurrency
  reservations.
- **Different failure semantics** — partial-batch-failure pattern with
  automatic DLQ after 3 retries. Routes vs. handlers want different
  retry/observability shapes.
- **Different release cadence** — handler logic changes when quest /
  leaderboard / activity-feed contracts change; user features change on
  the product cadence.

The producers (lingo-core, lingo-ops) stay HTTP-shaped. This repo owns
everything downstream of `sqs.send_message`.

## Critical orientation

- **Message contracts:** `app/contracts/messages.py` — Pydantic v2
  discriminated union. Producers MUST mirror these shapes; bumping the
  version is a contract change requiring producer + consumer to ship in
  lockstep.
- **Sibling producers:** `../lingo-core/app/events/publisher.py` and
  (future) `../lingo-ops/app/events/publisher.py`. The queue URL comes
  from `EVENTS_QUEUE_URL` env on the producer side.
- **Infra:** `../lingo-infra/main.tf` — SQS queues, DLQ, Lambda, IAM,
  event source mapping. Look here when wiring a new event type that
  needs IAM table access.

## Stack

- Python 3.13 (sync-first; Lambda runtime doesn't reward async here —
  one boto3 client call per side-effect, no concurrency to exploit)
- Pydantic v2 for message validation
- boto3 (NOT aioboto3) — sync clients keep cold-start lean
- Linting: Ruff (E/F/I/UP), line-length 200, target-version py313
- Testing: pytest + pytest-asyncio (`asyncio_mode = "auto"`)

## Source layout

```
app/
├── __init__.py
├── handler.py            # lambda_handler — SQS event → dispatch loop
├── config.py             # Pydantic Settings (AWS_REGION, DYNAMODB_TABLE_PREFIX, LOG_LEVEL)
├── contracts/
│   ├── __init__.py
│   └── messages.py       # discriminated union of EventMessage types
├── handlers/
│   ├── __init__.py
│   ├── xp_awarded.py
│   ├── lesson_completed.py
│   ├── review_completed.py
│   ├── friend_added.py
│   └── subscription_changed.py
├── quests/
│   ├── __init__.py
│   └── evaluator.py      # stub — real impl needs lingo_quests schema
├── leaderboard/
│   ├── __init__.py
│   └── updater.py        # real: UpdateItem on lingo_social_leaderboard
└── db/
    ├── __init__.py
    └── dynamo_client.py  # module-level boto3 resource singleton
```

## Conventions

- **Entry point:** `app.handler.lambda_handler` — the only Lambda-facing
  symbol. AWS hands us `{Records: [...]}`; we return
  `{batchItemFailures: [{itemIdentifier: <messageId>}]}` for partial-
  batch retry.
- **Dispatch by `type`:** the discriminated union resolves the concrete
  message class; we look up the handler in a module-level table keyed on
  the type string. Adding a new type means: schema + handler + table
  entry + test.
- **Side-effect modules** (`quests/`, `leaderboard/`) own their own
  Dynamo calls. Handlers wire them together — they're orchestration,
  not data access.
- **External clients** (boto3): instantiate at module level via
  `db/dynamo_client.py`. Never per-request.
- **Datetime:** use `datetime.now(UTC)`, never `datetime.utcnow()`.
- **Imports:** top-level only.
- **Logging:** `lingo_async.handler`, `lingo_async.quests`,
  `lingo_async.leaderboard` — separate namespace from lingo-core /
  lingo-ops so the shared log aggregator can filter cleanly.

## Partial-batch-failure pattern

The event source mapping is configured with
`FunctionResponseTypes = ["ReportBatchItemFailures"]`. We return only
the message IDs that failed; SQS keeps those in flight until next
retry. Successful messages auto-delete.

If we returned `{}` (empty failures) but raised an exception, ALL
messages in the batch would be marked failed and retried — defeating
the point. Catch per-message exceptions in the dispatch loop; never let
one bad message poison the batch.

After `maxReceiveCount = 3`, SQS moves the message to
`lingo-events-dlq`. Don't drain the DLQ by hand without inspecting the
messages first — a Dynamo schema drift or a malformed-producer bug
shows up there before it shows up anywhere else.

## What's missing (do NOT assume working in features)

- **Quest evaluator is a stub** — logs the event and no-ops. Real impl
  needs the `lingo_quests` table schema, which lives in lingo-core
  (`app/quests/`). Pull the protocol over when wiring the real thing.
- **`subscription_changed` handler is a stub** — placeholder for future
  premium-tier badge logic / churn alerting.
- **No DLQ poll/replay tooling** — when the first DLQ message lands,
  add a `scripts/dlq-replay.py` that pulls from the DLQ, re-pushes to
  the main queue.
- **No metrics emission** — CloudWatch Logs only. Add embedded-metric-
  format output once we have a dashboard to feed.

## Patterns to follow

- **Idempotency:** SQS guarantees at-least-once delivery, not exactly-
  once. Side-effects must be safe to retry. The leaderboard updater
  uses `ADD` (atomic increment) — bad on duplicate. We accept this for
  now; if dedupe becomes critical, add a seen-message-id check against
  a short-TTL Dynamo table.
- **Schema versions:** every message carries `version: Literal[1]`.
  When a contract changes, bump to `version: 2` and add a new handler
  branch — never silently extend `version: 1` with new fields, since
  in-flight messages from the prior producer build will still be
  `version: 1`.
- **Don't let handlers swallow exceptions.** The dispatch loop catches;
  handlers raise. Swallowed errors disappear into stdout and the
  message succeeds — debugging from CloudWatch becomes guesswork.

## Dev loop

```bash
# install
pip install -e ".[dev]"

# test
pytest -q

# lint
ruff check .
ruff format .

# build Lambda zip
./scripts/build-zip.sh
```

No local server — there's no HTTP surface. To smoke-test handler logic
locally, write a test that hand-crafts an SQS event payload and calls
`app.handler.lambda_handler(event, None)`.

## Environment

3 env vars via `.env` or Pydantic Settings:
- `AWS_REGION` — only matters for local pytest; Lambda runtime sets
  this automatically (it's a reserved env var on AWS Lambda).
- `DYNAMODB_TABLE_PREFIX` — must match the lingo-infra prefix
  (`lingo_`).
- `LOG_LEVEL` — `INFO` in Lambda, `DEBUG` locally.

DynamoDB tables read/written: `lingo_users`, `lingo_social_leaderboard`,
`lingo_quests` (future).

## Don't

- **Don't use `datetime.utcnow()`** — deprecated in py3.13.
- **Don't add a FastAPI router** — there's no HTTP here. If you find
  yourself reaching for one, the work belongs in lingo-core, not here.
- **Don't share code with lingo-core.** Vendor what you need (currently
  nothing). Independent release cadence is the whole point.
- **Don't pre-instantiate boto3 clients per-request.** Module-level
  only; cold-start is the only meaningful latency.
- **Don't add AI attribution to commits.**
- **Don't drain the DLQ without inspecting it.** The DLQ is the canary;
  silencing it without root-cause analysis hides real bugs.
