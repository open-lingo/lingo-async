"""Settings for the async worker.

Sparse on purpose — there are no upstream API credentials here. The only
configurable surface is AWS region, the Dynamo table prefix (must match
the lingo-infra prefix), and log level.
"""

from pathlib import Path

from pydantic_settings import BaseSettings

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    # AWS Lambda sets AWS_REGION automatically at runtime; this default
    # only matters for local pytest / scripts.
    AWS_REGION: str = "us-west-1"

    # Must match the prefix in lingo-infra. Different value ⇒ writes land
    # in a parallel set of tables that no other service reads — silently
    # broken.
    DYNAMODB_TABLE_PREFIX: str = "lingo_"

    # INFO in Lambda; DEBUG locally for verbose handler tracing.
    LOG_LEVEL: str = "INFO"

    # "sqlite" | "dynamodb" | "" (= disabled)
    EVENT_LOG_BACKEND: str = ""
    EVENT_LOG_SQLITE_PATH: str = "/tmp/lingo-events.sqlite"

    LINGO_CORE_URL: str = "http://localhost:8000"
    INTERNAL_SERVICE_TOKEN: str = ""

    # ── async→core callback resilience (see app/http/lingo_core_client.py) ──
    # Bounded in-process retries on transient core 5xx / network errors. We
    # keep this small: the SQS event source mapping already gives us coarse
    # retries (maxReceiveCount → DLQ), so the in-process layer only smooths
    # over a brief core blip. Too many retries × the Lambda batch size = a
    # retry storm against a struggling core, which is exactly what H3 guards
    # against — bound it, back off, then give up and let SQS requeue.
    CORE_CLIENT_MAX_RETRIES: int = 2  # total attempts = 1 + this
    CORE_CLIENT_BACKOFF_BASE_S: float = 0.2  # exponential: base * 2**attempt
    CORE_CLIENT_BACKOFF_MAX_S: float = 2.0

    # Circuit breaker: once core has tripped us this many times in a row,
    # stop trying for COOLDOWN seconds. A consecutive run of core 5xx means
    # core is down, not flaky — hammering it (even with backoff) just adds
    # load. Open circuit ⇒ fail fast, let the message return to the queue.
    CORE_CLIENT_BREAKER_THRESHOLD: int = 5
    CORE_CLIENT_BREAKER_COOLDOWN_S: float = 30.0

    model_config = {"env_file": str(_PROJECT_ROOT / ".env"), "env_file_encoding": "utf-8"}


settings = Settings()


def users_table_name() -> str:
    return f"{settings.DYNAMODB_TABLE_PREFIX}users"


def social_leaderboard_table_name() -> str:
    return f"{settings.DYNAMODB_TABLE_PREFIX}social_leaderboard"


def quests_table_name() -> str:
    return f"{settings.DYNAMODB_TABLE_PREFIX}quests"
