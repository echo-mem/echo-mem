"""The shape of a machine reported event, and what makes one safe to accept.

Capability A needs outcomes from things that are not agents: a CI run, a deploy,
a test suite. None of them speaks MCP, and none of them should be handed a key
that can write arbitrary facts.

So events arrive as a validated envelope, are stored, and are applied later.
That "later" is not a performance choice. It is forced, and the reason is worth
stating precisely because it is the one thing about this design that looks like
over-engineering and is not:

    write_episode takes a per-scope advisory lock as the first statement in its
    transaction, so two writers into ONE scope serialise correctly - there is a
    test. But Apache AGE's label catalogue is per GRAPH, not per scope. Two
    writers in different scopes of the same graph take different locks and
    proceed concurrently, and under load AGE then builds table names out of
    garbage memory: `relation "echo_memory.-775.1utterance" does not exist`.
    Observed, repeatedly, when four benchmark shards wrote four scopes at once;
    three of the four died.

A multi-tenant HTTP endpoint that writes straight through is precisely that
condition. So the door and the write are separated: the door validates and
INSERTs one row, which is safe at any concurrency, and a single writer per graph
drains the inbox.

## What the envelope is for

Four properties, each of which is a thing that goes wrong otherwise:

    event_id      idempotency. A CI system that retries on a timeout must not
                  record the deployment twice, and it cannot know whether the
                  first call landed.
    occurred_at   replay protection, together with MAX_AGE. A captured request
                  replayed next month must not re-assert a stale outcome as new.
    actor         attribution. An outcome with no reporter cannot be weighed,
                  and `agent_id` is bound to the credential rather than sent.
    schema_version  so the drain can refuse what it does not understand instead
                  of guessing at a field that moved.

`scope` names a memory and a solo/shared scope, never a group id. A caller that
could name a group id could name somebody else's; see cloud tenancy.py.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

#: Bumped when a field's meaning changes, never for an addition. The drain
#: refuses a version it does not know rather than reading a familiar-looking
#: field out of an unfamiliar envelope.
SCHEMA_VERSION = 1

#: Everything this version can apply.
SUPPORTED_VERSIONS = frozenset({1})

#: 64 KiB. Large enough for a test report summary, small enough that a hostile
#: caller cannot fill the inbox with one request. The payload is stored as
#: jsonb, so this bounds a row rather than a buffer.
MAX_PAYLOAD_BYTES = 64 * 1024

#: How far ahead of our clock a reporter may claim to be. A CI runner with a
#: drifting clock is normal; an hour of drift is not.
MAX_CLOCK_SKEW = timedelta(minutes=15)

#: How old an event may be and still be applied. This is the replay window: a
#: captured request is useless after it, and a genuine backfill goes through an
#: operator path that records that it is a backfill rather than pretending the
#: event just happened.
MAX_AGE = timedelta(days=7)

#: What a reporter may say happened. A closed set, because the drain branches on
#: it and a type it has never seen has no handler - and defaulting an unknown
#: type to "record it as a fact" is how an event channel becomes an arbitrary
#: write endpoint.
DECISION_RECORDED = "decision.recorded"
ACTION_EXECUTED = "action.executed"
OUTCOME_OBSERVED = "outcome.observed"
DEPLOYMENT_COMPLETED = "deployment.completed"
TEST_COMPLETED = "test.completed"
EVIDENCE_ATTACHED = "evidence.attached"
CLAIM_VERIFIED = "claim.verified"

EVENT_TYPES = frozenset({
    DECISION_RECORDED, ACTION_EXECUTED, OUTCOME_OBSERVED,
    DEPLOYMENT_COMPLETED, TEST_COMPLETED, EVIDENCE_ATTACHED, CLAIM_VERIFIED,
})

#: Event types that may only be sent by a credential holding trust.verify.
#: Separating these from the reporting types is the mechanism by which a
#: reporter cannot elevate its own claims: it can attach evidence all day and
#: cannot say the evidence was sufficient.
REQUIRES_VERIFY = frozenset({CLAIM_VERIFIED})

#: event_id shape. Caller supplied so that the caller can make it deterministic
#: from its own run - "deploy-{commit}-{env}" is idempotent across retries in a
#: way a uuid generated per attempt is not.
_EVENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")

PENDING, APPLIED, REJECTED, QUARANTINED = (
    "pending", "applied", "rejected", "quarantined")
INBOX_STATES = (PENDING, APPLIED, REJECTED, QUARANTINED)


class EnvelopeError(Exception):
    """The envelope cannot be accepted, and the message says which rule it
    broke. Returned to the caller as a 400: a reporter that is sending
    something malformed needs to know what, or it will keep sending it."""


@dataclass(frozen=True)
class Actor:
    """Who reported this. `agent` is the agent.key string, bound to the
    credential by the hosted service and never read from the payload."""

    agent: str
    run_id: str = ""
    credential_id: str = ""

    def as_dict(self) -> dict:
        return {"agent": self.agent, "run_id": self.run_id,
                "credential_id": self.credential_id}


@dataclass(frozen=True)
class Scope:
    """Which memory, and who inside the account. Never a group id."""

    memory: str | None = None
    scope: str = "shared"

    def as_dict(self) -> dict:
        return {"memory": self.memory, "scope": self.scope}


@dataclass(frozen=True)
class Envelope:
    event_id: str
    event_type: str
    occurred_at: datetime
    actor: Actor
    scope: Scope
    payload: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    def as_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "occurred_at": self.occurred_at.isoformat(),
            "actor": self.actor.as_dict(),
            "scope": self.scope.as_dict(),
            "payload": self.payload,
            "provenance": self.provenance,
        }


def _parse_when(raw) -> datetime:
    if isinstance(raw, datetime):
        when = raw
    elif isinstance(raw, str):
        try:
            when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as e:
            raise EnvelopeError(f"occurred_at is not an ISO 8601 instant: {raw!r}") from e
    else:
        raise EnvelopeError(f"occurred_at must be an ISO 8601 string, got {type(raw).__name__}")
    # Naive timestamps are refused rather than assumed UTC. A reporter in
    # another timezone sending a naive instant would have its events land hours
    # out, and the replay window would silently stop protecting anything.
    if when.tzinfo is None:
        raise EnvelopeError(
            "occurred_at must carry a timezone offset; a naive instant from a "
            "reporter in another zone would land hours out and quietly widen "
            "the replay window"
        )
    return when


def parse(raw: dict, *, now: datetime | None = None) -> Envelope:
    """Validate an inbound event and return it, or raise saying which rule it
    broke.

    Pure: no database, no clock except the one passed in. That is what lets the
    whole acceptance policy be tested without a server, and it is why `now` is a
    parameter rather than a call to utcnow inside.
    """
    if not isinstance(raw, dict):
        raise EnvelopeError(f"an event must be an object, got {type(raw).__name__}")
    now = now or datetime.now(timezone.utc)

    version = raw.get("schema_version", SCHEMA_VERSION)
    if version not in SUPPORTED_VERSIONS:
        raise EnvelopeError(
            f"schema_version {version!r} is not one this version can apply "
            f"({sorted(SUPPORTED_VERSIONS)}). Refused rather than read with "
            f"the fields this version happens to know."
        )

    event_id = str(raw.get("event_id") or "")
    if not _EVENT_ID.match(event_id):
        raise EnvelopeError(
            "event_id must be 8 to 128 characters of letters, digits, dot, "
            "colon, underscore or hyphen. It is the idempotency key, so make it "
            f"deterministic from your own run rather than random. Got {event_id!r}"
        )

    event_type = raw.get("event_type")
    if event_type not in EVENT_TYPES:
        raise EnvelopeError(
            f"event_type must be one of {sorted(EVENT_TYPES)}, got {event_type!r}"
        )

    when = _parse_when(raw.get("occurred_at"))
    if when > now + MAX_CLOCK_SKEW:
        raise EnvelopeError(
            f"occurred_at is {when - now} in the future, beyond the "
            f"{MAX_CLOCK_SKEW} allowed for clock drift"
        )
    if when < now - MAX_AGE:
        raise EnvelopeError(
            f"occurred_at is {now - when} old, beyond the {MAX_AGE} replay "
            f"window. A genuine backfill goes through the operator path, which "
            f"records that it is a backfill instead of asserting it as new."
        )

    actor_raw = raw.get("actor") or {}
    if not isinstance(actor_raw, dict) or not str(actor_raw.get("agent") or "").strip():
        raise EnvelopeError("actor.agent is required: an outcome with no reporter cannot be weighed")

    payload = raw.get("payload") or {}
    if not isinstance(payload, dict):
        raise EnvelopeError(f"payload must be an object, got {type(payload).__name__}")
    size = len(json.dumps(payload).encode("utf-8"))
    if size > MAX_PAYLOAD_BYTES:
        raise EnvelopeError(f"payload is {size} bytes, over the {MAX_PAYLOAD_BYTES} limit")

    scope_raw = raw.get("scope") or {}
    if not isinstance(scope_raw, dict):
        raise EnvelopeError("scope must be an object naming a memory and a scope")
    which = scope_raw.get("scope", "shared")
    if which not in ("solo", "shared"):
        raise EnvelopeError(f'scope.scope must be "solo" or "shared", got {which!r}')
    # Refused, not stripped: a group id arriving here means the caller believes
    # it may name one, and the honest answer is that it may not.
    if "group_id" in scope_raw:
        raise EnvelopeError(
            "scope must not contain group_id. Which graph a call reaches is "
            "derived from the credential, never named by the caller."
        )

    return Envelope(
        event_id=event_id,
        event_type=event_type,
        occurred_at=when,
        actor=Actor(
            agent=str(actor_raw["agent"]).strip(),
            run_id=str(actor_raw.get("run_id") or ""),
            credential_id=str(actor_raw.get("credential_id") or ""),
        ),
        scope=Scope(memory=scope_raw.get("memory"), scope=which),
        payload=payload,
        provenance=raw.get("provenance") or {},
        schema_version=version,
    )
