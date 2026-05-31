"""Dynamo write-side stub. Not implemented yet — see plan docs for
schema (PK=USER#<id>, SK=<received_at>#<id>, ttl_epoch as TTL attr)."""


class DynamoEventsWriteRepository:
    def __init__(self) -> None:
        pass

    def save(self, **kwargs) -> None:
        raise NotImplementedError("DynamoEventsWriteRepository: pending impl")

    def update_status(self, **kwargs) -> None:
        raise NotImplementedError("DynamoEventsWriteRepository: pending impl")
