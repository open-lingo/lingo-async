# lingo-async Lambda deployment runbook

First-deploy runbook. Subsequent deploys collapse to "rebuild + push
zip"; the Terraform side is one-shot.

Companion infrastructure: `../lingo-infra/main.tf` (SQS queues, DLQ,
IAM, Lambda, event source mapping).

---

## 1. Build the deploy zip

```bash
cd lingo-async
./scripts/build-zip.sh
```

Output: `dist/lingo-async.zip` (targets ARM64 / Python 3.13).

The script pulls `manylinux2014_aarch64` wheels — your dev box doesn't
need to be ARM. No web framework is bundled; this is an event-driven
worker with no HTTP surface.

## 2. Apply the Terraform

From `lingo-infra/`:

```bash
cd ../lingo-infra
terraform init
terraform plan  -var "lingo_async_zip_path=../lingo-async/dist/lingo-async.zip"
terraform apply -var "lingo_async_zip_path=../lingo-async/dist/lingo-async.zip"
```

Creates:

- `aws_sqs_queue.lingo_events` — main queue (visibility 60s, retention 4d)
- `aws_sqs_queue.lingo_events_dlq` — dead-letter queue (retention 14d)
- `aws_iam_role.lingo_async_lambda` + 2 policy attachments (basic
  execution + `lingo-async-lambda-extras`)
- `aws_iam_policy.lingo_async_lambda_extras` — SQS receive/delete on the
  queue + Dynamo Get/UpdateItem on `lingo_users`,
  `lingo_social_leaderboard`, `lingo_quests`
- `aws_lambda_function.lingo_async` — Python 3.13, ARM, 512 MB, 30 s
- `aws_lambda_event_source_mapping.lingo_async` — SQS → Lambda, batch 10,
  partial-batch failure reporting enabled

The plan prints `lingo_events_queue_url` — that's the value producers
need as `EVENTS_QUEUE_URL`.

## 3. Wire the queue URL into producers

```bash
# lingo-core Lambda env var (AWS console → Lambda → lingo-core →
# Configuration → Environment variables):
EVENTS_QUEUE_URL=https://sqs.<region>.amazonaws.com/<account>/lingo-events
```

`lingo-core` no-ops the publish when this var is unset, so local dev
without SQS still works.

Producer IAM also needs `sqs:SendMessage` on the queue — see the
`sqs:SendMessage` block on the lingo-ops policy in `main.tf` and the
note about lingo-core's IAM (currently not Terraform-managed; the
maintainer adds the perm by hand).

## 4. Smoke-test

Manually publish a message to the queue:

```bash
QUEUE_URL=$(cd ../lingo-infra && terraform output -raw lingo_events_queue_url)

aws sqs send-message \
  --queue-url "$QUEUE_URL" \
  --message-body '{"type":"xp_awarded","version":1,"user_id":"smoke","amount":1,"source":"manual"}'
```

Tail the Lambda logs:

```bash
aws logs tail /aws/lambda/lingo-async --follow
```

You should see one `dispatch:xp_awarded` log line, then a leaderboard
updater attempt (which will fail with "user not found" for the bogus
`smoke` user — that's expected). The message should NOT appear in the
DLQ for a known-handler type with valid envelope; if it does, check
the per-message exception in the logs.

## 5. Subsequent deploys

```bash
cd lingo-async
./scripts/build-zip.sh -f lingo-async    # builds + pushes
```

Or skip Terraform after first deploy:

```bash
aws lambda update-function-code \
  --function-name lingo-async \
  --zip-file fileb://dist/lingo-async.zip
```

`source_code_hash` drifts after a manual `update-function-code`, but
the next `terraform apply` re-converges without recreating the
function (the `ignore_changes` block on the Lambda resource matches
the lingo-ops pattern).

## DLQ inspection

When a message lands in `lingo-events-dlq`:

```bash
DLQ_URL=$(cd ../lingo-infra && terraform output -raw lingo_events_dlq_url)

# Peek without consuming (Sets visibility briefly):
aws sqs receive-message \
  --queue-url "$DLQ_URL" \
  --max-number-of-messages 10 \
  --visibility-timeout 5
```

Inspect the bodies, fix the producer or handler bug, then either drain
the DLQ (after the fix is deployed) or replay messages back to the main
queue. There's no `dlq-replay.py` script yet — see CLAUDE.md.

## Rollback

```bash
aws lambda list-versions-by-function --function-name lingo-async
aws lambda update-alias --function-name lingo-async \
  --name $alias --function-version $previous_version_number
```

For Terraform-managed state rollback (queues, IAM, event source
mapping), `terraform apply` from an earlier commit on `lingo-infra`.
