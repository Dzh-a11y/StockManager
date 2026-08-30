"""Error semantics for the swappable data-read layer (P4-1).

Errors are explicit: no swallowed exceptions, no partial success masquerading
as complete data, and shard failures carry the shard identity and the
original exception chain.
"""

from __future__ import annotations


class ReadLayerError(ValueError):
    """Base class for read-layer failures."""


class DatasetUnavailableError(ReadLayerError):
    """Raised when a dataset snapshot is not available locally."""


class AdjustmentMismatchError(ReadLayerError):
    """Raised when the request adjustment does not match the dataset metadata."""


class SnapshotConsistencyError(ReadLayerError):
    """Raised when a dataset version changes during a concurrent read.

    A frozen snapshot must stay identical across all shards; a mismatch means
    the caller cannot trust a mixed-version result, so no partial data is
    returned.
    """


class ShardReadError(ReadLayerError):
    """A shard query failed; carries the shard index and the original error."""

    def __init__(self, shard_index: int, message: str, cause: BaseException) -> None:
        super().__init__(message)
        self.shard_index = shard_index
        self.cause = cause

    def __str__(self) -> str:
        return f"shard {self.shard_index}: {super().__str__()} (caused by {type(self.cause).__name__})"
