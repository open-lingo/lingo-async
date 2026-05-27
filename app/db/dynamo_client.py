"""Shared boto3 DynamoDB resource for the async worker.

Module-level instantiation matters: Lambda reuses the warm container
across invocations, so the boto3 client is built once at cold start and
amortised across many messages. Per-message client construction would
add ~100ms to every dispatch.

We expose the resource-level interface (``Table``) rather than the
low-level client because handlers use named expression attributes
heavily (``UpdateExpression``, ``ExpressionAttributeNames``), which the
resource layer types more ergonomically.
"""

import boto3

from app.config import settings

_resource = boto3.resource("dynamodb", region_name=settings.AWS_REGION)


def get_table(name: str):
    """Return a Dynamo Table handle. Cheap — no network on this call."""
    return _resource.Table(name)
