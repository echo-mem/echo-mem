"""The queue between a machine reporting something and the graph believing it.

Same shape as `ingestion/capture.py`, and for a related reason. There, a hook
notices that a memory file changed and queues it, because extraction needs a
model and the server does not call one. Here, a reporter says a deploy
finished and the event is queued, because applying it needs a graph write and
a graph write cannot be done concurrently. Both split "we have heard about
this and cannot forget it" from "we have acted on it", and both make the first
half cheap enough to be safe.

Three functions and a state change:

    enqueue  one INSERT, idempotent on (group_id, event_id)
    pending  what is waiting
    drain    the single writer, applying events one at a time
    mark     the state change, with what it created or why it did not

Nothing here parses an event. `events.envelope.parse` does that, at the door,
before `enqueue` is called - so a malformed event never reaches this table and
the inbox holds only things that were acceptable when they arrived. What the
drain can still refuse is an event that was well formed and turned out to be
inapplicable: a decision that does not exist, a payload field that names
nothing. That distinction is why `rejected` carries an error string.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from echo_memory.events.envelope import (
    APPLIED,
    INBOX_STATES,
    PENDING,
    REJECTED,
    Envelope,
    parse,
)
from echo_memory.infra import graphlock
from echo_memory.infra.db import GRAPH_NAME
from echo_memory.infra.logging import get_logger

_logger = get_logger("event_inbox")

#: The advisory lock the drain holds, keyed per GRAPH and not per scope.
#:
#: `write_episode` locks `episode|<group_id>`, which serialises two writers
#: into one scope and is correct for what it is. It is not enough here. Apache
#: AGE keeps its label catalogue per graph, so two writers in DIFFERENT scopes
#: of the same graph take different locks, proceed concurrently, and AGE
#: composes table names out of memory that is being written underneath it:
#:
#:     relation "echo_memory.-775.1utterance" does not exist
#:
#: Four benchmark shards writing four scopes at once killed three of the four
#: that way, and the run still looked complete from outside because each shard
#: reported its own success. A hosted ingress accepting events from every
#: customer at once is that condition permanently, which is what makes the
#: drain the single writer that makes a multi-tenant door safe. The lock is
#: how "single" is enforced rather than hoped for.
#:
#: Transaction scoped, so there is no unlock path to forget: it releases on
#: commit or rollback either way.
DRAIN_LOCK = "echo_memory|graph|drain"

#: How many events one drain applies. A bound rather than "everything", so a
#: backlog is worked off in batches that each commit, instead of one
#: transaction holding the graph lock for an unbounded time.
DEFAULT_LIMIT = 100


class InboxError(Exception):
    """A caller asked for something the inbox cannot do: an unknown state, or
    a drain with no apply function."""


def enqueue(conn, group_id: str, envelope: Envelope) -> dict:
    """Record an event for the drain to apply. Returns whether it was new.

    A duplicate is NOT an error, and this is the one thing about this function
    worth being firm about. A CI system that times out waiting for a response
    cannot know whether the first call landed. Its only safe behaviour is to
    retry, and if the retry fails it has to choose between dropping the event
    and recording it twice. `event_id` exists so it has to do neither, and
    returning an error on the second call would throw that away: the caller
    would start treating a successful de-duplication as a failure and retry
    again.

    So the second call returns `accepted` false and `duplicate` true, and both
    mean the same thing to the reporter - the event is in the inbox, stop
    sending it. The row is left exactly as it was, including its state: a
    replay of an event that already applied must not reopen it as pending.

    "The same event" means the same id IN THIS SCOPE. The conflict target is
    the composite key migration 0037 widened it to, and that is a tenancy
    boundary rather than a detail: on the global key, scope B sending an id
    scope A had used got `duplicate: true` - the one answer that means stop
    retrying - while A kept the row, so one tenant could silence another's
    reporting and the refusal itself disclosed that A had used the id. The
    envelope docstring recommends ids derived from the reporter's own run
    ("deploy-{commit}-{env}"), which makes that collision ordinary.
    """
    row = conn.execute(
        """
        INSERT INTO public.event_inbox
            (event_id, group_id, event_type, schema_version, envelope, occurred_at)
        VALUES (%s, %s, %s, %s, %s::jsonb, %s)
        ON CONFLICT (group_id, event_id) DO NOTHING
        RETURNING event_id
        """,
        (
            envelope.event_id,
            group_id,
            envelope.event_type,
            envelope.schema_version,
            json.dumps(envelope.as_dict()),
            envelope.occurred_at,
        ),
    ).fetchone()
    accepted = row is not None
    _logger.info(
        "event_enqueued",
        extra={
            "group_id": group_id,
            "event_id": envelope.event_id,
            "event_type": envelope.event_type,
            "accepted": accepted,
        },
    )
    return {
        "event_id": envelope.event_id,
        "accepted": accepted,
        "duplicate": not accepted,
    }


def pending(conn, *, limit: int = DEFAULT_LIMIT) -> list[dict]:
    """Events waiting to be applied, oldest first by when we heard.

    Ordered on `received_at` rather than `occurred_at`, deliberately. A
    reporter's clock is its own and the envelope only bounds how far it may
    drift; ordering on a value the sender controls would let a reporter with a
    slow clock jump the queue, and would make the drain's order depend on
    something this database cannot verify. `received_at` is ours.

    Not scoped to a group, because neither is the drain. One graph, one
    writer, one queue - a per-scope queue would need a per-scope drain, which
    is the concurrency this whole design exists to avoid.
    """
    rows = conn.execute(
        """SELECT event_id, group_id, event_type, schema_version, envelope,
                  occurred_at, received_at, attempts
             FROM public.event_inbox
            WHERE state = %s
            ORDER BY received_at, event_id
            LIMIT %s""",
        (PENDING, limit),
    ).fetchall()
    return [
        {
            "event_id": r[0],
            "group_id": r[1],
            "event_type": r[2],
            "schema_version": r[3],
            "envelope": r[4],
            "occurred_at": r[5],
            "received_at": r[6],
            "attempts": r[7],
        }
        for r in rows
    ]


def mark(
    conn,
    group_id: str,
    event_id: str,
    state: str,
    *,
    error: str | None = None,
    edge_ids: list[str] | None = None,
) -> None:
    """Move an event to a terminal state, recording what it created or why it
    did not.

    `group_id` is required and is half the key. Since 0037 an `event_id` is
    unique only within a scope, so an UPDATE on the id alone would mark every
    scope's event of that name - one reporter's rejection landing on another
    tenant's pending row. The drain reads the group off the inbox ROW and
    passes it back here, which is the same source the row was written from.

    `applied_edge_ids` is set rather than appended. An event applies once; if
    it is ever re-applied after a restore, what the second attempt created is
    the truth about the graph as it now stands, and keeping both would make a
    reconciliation count every fact twice. This project has already had a
    reconciliation over all history explain a tampered fact by finding the
    write that tampered with it, and the thing that made that possible was a
    record of what each change touched.
    """
    if state not in INBOX_STATES:
        raise InboxError(f"unknown inbox state {state!r}, expected one of {list(INBOX_STATES)}")
    conn.execute(
        """UPDATE public.event_inbox
              SET state = %s, processed_at = now(), error = %s,
                  applied_edge_ids = %s
            WHERE group_id = %s AND event_id = %s""",
        (state, error, edge_ids, group_id, event_id),
    )


def _count_attempt(conn, group_id: str, event_id: str) -> None:
    """Increment attempts, outside whatever savepoint the apply runs in.

    Outside on purpose. An apply that raises rolls its savepoint back, and if
    the counter were inside it the rollback would also undo the record that it
    had been tried - so an event failing forever would read as an event nobody
    had touched.

    Keyed on the pair for the reason `mark` is: counting an attempt against
    every scope that happens to share the id would make `attempts` say that a
    reporter had been retried when nobody had touched its event.
    """
    conn.execute(
        """UPDATE public.event_inbox SET attempts = attempts + 1
            WHERE group_id = %s AND event_id = %s""",
        (group_id, event_id),
    )


def drain(
    conn,
    *,
    limit: int = DEFAULT_LIMIT,
    apply: Callable[[Any, str, Envelope], dict] | None = None,
) -> dict:
    """Apply pending events, one at a time, as the graph's single writer.

    `apply` is called as `apply(conn, group_id, envelope)`, and the group comes
    from the inbox ROW rather than from the envelope. That is not an
    inconsistency: `envelope.parse` refuses an event carrying a group_id at
    all, because which graph a call reaches is derived from the credential and
    never named by the caller. The door wrote the group it derived into the
    row; the drain reads it back from there. An applier needing anything else
    bound - an embedder, a provider - binds it with `functools.partial` before
    handing the function over, so this signature stays the one thing the
    inbox has to know.

    The whole drain runs in one transaction holding DRAIN_LOCK, which is keyed
    per graph: see the note on that constant for the AGE label corruption that
    makes a per-scope lock insufficient and for the four-shard run where three
    shards died of it. One transaction rather than one per event, so that the
    lock cannot be released and re-taken between events - which would let a
    second drain interleave with this one and put back exactly the concurrency
    the lock is here to remove.

    Each event then runs inside its own savepoint. Without one, the first
    event whose apply raised would abort the enclosing transaction and lose
    every event already applied in this batch, plus the marks recording them.
    With one, a failure is contained: the savepoint rolls back, the event is
    marked `rejected` with the error, and the drain carries on.

    A hard failure - the connection dying, the lock being lost - still rolls
    the whole batch back, marks included, which leaves every event in it
    `pending`. That is the correct outcome and the reason the marks are in the
    same transaction as the writes they describe: an event marked `applied`
    whose facts were rolled back would be a write that returned without
    storing, which is the failure mode this project has paid for three times.

    `rejected` is terminal, and `attempts` is counted without anything
    re-queueing on it. That is deliberate for now: the failures this path
    produces are inapplicable payloads, which do not become applicable by being
    retried. A genuinely transient failure - the store unreachable mid-batch -
    takes the whole transaction down instead, which leaves every event in it
    `pending` and retryable. Adding a retry state before anything needs one
    would mean guessing which failures deserve it.
    """
    if apply is None:
        raise InboxError(
            "drain needs an apply function; it is the only thing that knows how "
            "to turn an event into graph writes. See intelligence.outcomes.apply."
        )

    applied: list[str] = []
    rejected: list[dict] = []

    with conn.transaction():
        # The same 64 bit key graphlock uses for the graph wide gate, so the
        # drain and an operator running a bulk ingest under that gate contend
        # with each other rather than running concurrently while both believe
        # they are the single writer.
        conn.execute(
            "SELECT pg_advisory_xact_lock(%s::bigint)",
            (graphlock.lock_key("graph", GRAPH_NAME),),
        )
        # Read inside the lock, not before it. A list gathered outside would be
        # a list another drain may already have applied.
        for event in pending(conn, limit=limit):
            event_id, group_id = event["event_id"], event["group_id"]
            _count_attempt(conn, group_id, event_id)
            try:
                with conn.transaction():
                    # Re-parsed from the stored envelope rather than handed to
                    # apply as a raw dict, so the applier works on the same
                    # validated type whatever put the row there - including a
                    # row inserted by hand during an incident.
                    #
                    # `now` is pinned to the event's own instant, which turns
                    # off the clock-skew and replay-window checks and leaves
                    # the shape checks on. Those two were the door's job and
                    # passed there. Re-applying them here would reject an event
                    # for having sat in a backlog, which is this drain's
                    # failure and not the reporter's, and the backlog is
                    # exactly when a drain matters.
                    envelope = parse(event["envelope"], now=event["occurred_at"])
                    result = apply(conn, group_id, envelope)
                    # Inside the savepoint with the writes it describes, so the
                    # two cannot disagree. A mark that committed while its facts
                    # rolled back would be an `applied` row naming edges that
                    # are not in the graph, and a reconciliation reading it
                    # would conclude the facts had been deleted.
                    edge_ids = [str(e) for e in (result or {}).get("edge_ids") or []]
                    mark(conn, group_id, event_id, APPLIED, edge_ids=edge_ids)
                applied.append(event_id)
            except Exception as e:  # noqa: BLE001
                # Caught broadly and recorded, which is right here and wrong
                # almost everywhere else: one reporter sending one inapplicable
                # event must not stop the queue for every other customer on the
                # graph. The error text is kept so the reporter can be told
                # what was wrong instead of being left to guess from silence.
                reason = f"{type(e).__name__}: {e}"
                mark(conn, group_id, event_id, REJECTED, error=reason)
                rejected.append({"event_id": event_id, "error": reason})
                _logger.warning(
                    "event_rejected",
                    extra={
                        "group_id": group_id,
                        "event_id": event_id,
                        "event_type": event["event_type"],
                        "error": reason,
                    },
                )

    _logger.info(
        "inbox_drained",
        extra={"applied": len(applied), "rejected": len(rejected)},
    )
    return {
        "applied": applied,
        "rejected": rejected,
        "processed": len(applied) + len(rejected),
    }
