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

    model_config = {"env_file": str(_PROJECT_ROOT / ".env"), "env_file_encoding": "utf-8"}


settings = Settings()


def users_table_name() -> str:
    return f"{settings.DYNAMODB_TABLE_PREFIX}users"


def social_leaderboard_table_name() -> str:
    return f"{settings.DYNAMODB_TABLE_PREFIX}social_leaderboard"


def quests_table_name() -> str:
    return f"{settings.DYNAMODB_TABLE_PREFIX}quests"
