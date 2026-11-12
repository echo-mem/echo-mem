"""Events: the validated door machines report outcomes through, and the queue
behind it.

`envelope` is the acceptance policy and is pure - no database, no clock it did
not get as an argument - so the whole of what may be accepted is testable
without a server. `inbox` is the queue and the single writer that applies it.

The split between them is the design: validating is safe at any concurrency,
writing to an AGE graph is not.
"""

from echo_memory.events.envelope import (
    EVENT_TYPES,
    MAX_AGE,
    MAX_CLOCK_SKEW,
    MAX_PAYLOAD_BYTES,
    REQUIRES_VERIFY,
    SCHEMA_VERSION,
    SUPPORTED_VERSIONS,
    Actor,
    Envelope,
    EnvelopeError,
    Scope,
    parse,
)
from echo_memory.events.inbox import (
    DRAIN_LOCK,
    InboxError,
    drain,
    enqueue,
    mark,
    pending,
)

__all__ = [
    "DRAIN_LOCK",
    "EVENT_TYPES",
    "MAX_AGE",
    "MAX_CLOCK_SKEW",
    "MAX_PAYLOAD_BYTES",
    "REQUIRES_VERIFY",
    "SCHEMA_VERSION",
    "SUPPORTED_VERSIONS",
    "Actor",
    "Envelope",
    "EnvelopeError",
    "InboxError",
    "Scope",
    "drain",
    "enqueue",
    "mark",
    "parse",
    "pending",
]
