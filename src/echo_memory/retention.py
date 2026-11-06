"""How long a scope may keep a fact, and the enforcement of it.

Erasure answers a subject who asks to be forgotten. This is the other half of
the same obligation: GDPR Article 5(1)(e) and India's DPDP Act both require
that data is not kept longer than its purpose needs, which is a standing rule
rather than a request. A store with erasure and no retention can delete on
demand and cannot say why it still holds anything.

## It deletes through erasure, not beside it

`enforce` builds the list of facts past their retention and hands it to
`erasure.erase`. It does not write its own DELETE. Supersession sets
`t_invalid` and KEEPS the fact, which is right for memory and insufficient for
law, and getting actual deletion right means versions, incident edges, orphaned
nodes and a vacuum - all of which `erasure` already does and has been wrong
about before. A second deletion path is a second place to get those wrong, and
the second one is the one nobody tests as hard.

It also means a retention deletion leaves exactly the evidence a subject
request leaves, in `erasure_event`, so "what have you deleted and why" has one
answer rather than two.

## Dry run by default

`enforce(dry_run=True)` is the default and returns the plan without touching
anything. `forget` already works this way and the reason is the same: this
removes customer data permanently, and a function whose default is destruction
is one somebody calls while exploring.

## What age means

Age is measured from `t_valid`, the instant the fact became true, which is the
only time the fact itself carries. That is deliberately not "when we were told"
- a fact backdated into the past is older than the write that recorded it, and
retention is about the data's age rather than our knowledge of it.
"""

from __future__ import annotations

from dataclasses import dataclass

from echo_memory import erasure
from echo_memory.infra.db import GRAPH_NAME as GRAPH
from echo_memory.infra.logging import get_logger

_logger = get_logger("retention")

#: The group_id predicate on an AGE edge, same form erasure uses.
_GROUP = """(properties ->> '"group_id"'::agtype)"""


class RetentionError(Exception):
    """The request names something that cannot be done, and why."""


@dataclass(frozen=True)
class Policy:
    """One scope's retention rule."""

    id: int
    group_id: str
    max_age_days: int
    reason: str
    decided_by: str
    created_at: str

    @property
    def summary(self) -> str:
        return (
            f"{self.max_age_days} days, because {self.reason} "
            f"(decided by {self.decided_by} on {self.created_at})"
        )


def set_policy(conn, group_id: str, max_age_days: int, reason: str,
               decided_by: str) -> Policy:
    """Record a retention rule, retiring any rule it replaces.

    Retiring rather than updating, so "why did we stop deleting after 90 days"
    stays answerable. The unique index allows one live policy per scope, which
    means this has to retire before it inserts: two policies disagreeing about
    how long a scope keeps data is a question with no answer, and the shorter
    one silently winning is behaviour nobody chose.
    """
    if max_age_days <= 0:
        raise RetentionError("retention must be at least one day")
    if not reason.strip():
        raise RetentionError(
            "a policy needs a reason: 'we delete after 90 days' is not a "
            "compliance answer, and nobody can reconstruct the purpose later"
        )
    if not decided_by.strip():
        raise RetentionError("a policy needs a decider; nobody is not an answer")
    conn.execute(
        "UPDATE public.retention_policy SET retired_at = now() "
        "WHERE group_id = %s AND retired_at IS NULL",
        (group_id,),
    )
    row = conn.execute(
        """INSERT INTO public.retention_policy
               (group_id, max_age_days, reason, decided_by)
           VALUES (%s, %s, %s, %s)
        RETURNING id, group_id, max_age_days, reason, decided_by,
                  to_char(created_at, 'YYYY-MM-DD')""",
        (group_id, max_age_days, reason.strip(), decided_by.strip()),
    ).fetchone()
    return Policy(*row)


def policy(conn, group_id: str) -> Policy | None:
    """The live policy for this scope, or None if it has none.

    None is a real and common answer, and it means "we have not decided",
    which is different from "we keep it forever". `enforce` refuses rather
    than treating an absent policy as permission to delete nothing or
    everything.
    """
    row = conn.execute(
        """SELECT id, group_id, max_age_days, reason, decided_by,
                  to_char(created_at, 'YYYY-MM-DD')
             FROM public.retention_policy
            WHERE group_id = %s AND retired_at IS NULL""",
        (group_id,),
    ).fetchone()
    return Policy(*row) if row else None


def history(conn, group_id: str) -> list[dict]:
    """Every policy this scope has had, newest first, retired ones included."""
    rows = conn.execute(
        """SELECT max_age_days, reason, decided_by,
                  to_char(created_at, 'YYYY-MM-DD'),
                  coalesce(to_char(retired_at, 'YYYY-MM-DD'), '')
             FROM public.retention_policy
            WHERE group_id = %s ORDER BY created_at DESC""",
        (group_id,),
    ).fetchall()
    keys = ("max_age_days", "reason", "decided_by", "from", "retired")
    return [dict(zip(keys, r, strict=True)) for r in rows]


def due(conn, group_id: str, max_age_days: int) -> list[str]:
    """Edge ids whose facts are older than the retention allows.

    Measured from `t_valid`, the instant the fact became true. Facts already
    invalidated are included: a superseded fact is still stored and still
    carries whatever it said, so leaving it out would let a store satisfy a
    retention policy while keeping every old version of everything.
    """
    cutoff_seconds = max_age_days * 86400
    rows = conn.execute(
        f"""
        SELECT id::text
          FROM {GRAPH}."FACT"
         WHERE {_GROUP} = %s
           AND (properties ->> '"t_valid"'::agtype)::bigint
               < (extract(epoch FROM now())::bigint - %s)
        """,
        (group_id, cutoff_seconds),
    ).fetchall()
    return [r[0] for r in rows]


@dataclass
class Enforcement:
    """What enforcement did, or would do."""

    group_id: str
    policy: Policy
    edge_ids: list[str]
    dry_run: bool
    erased: int = 0
    nodes_erased: int = 0

    @property
    def n_due(self) -> int:
        return len(self.edge_ids)

    def is_empty(self) -> bool:
        return not self.edge_ids


def enforce(conn, group_id: str, *, dry_run: bool = True,
            requested_by: str = "retention", ticket: str | None = None) -> Enforcement:
    """Delete what this scope's policy no longer permits it to keep.

    Dry run by default. The caller has to ask for the destructive version, in
    the same shape `forget` uses, because the alternative is a function whose
    default is permanent deletion of customer data.

    Refuses when there is no policy. An absent policy means nobody has decided
    how long this scope keeps data, and inventing a default here would be this
    module deciding a compliance question on the operator's behalf - in the
    direction of deleting their data.
    """
    live = policy(conn, group_id)
    if live is None:
        raise RetentionError(
            f"{group_id} has no retention policy, so there is nothing to "
            f"enforce. Set one with a reason and a decider; an absent policy "
            f"means nobody has decided, which is not the same as forever."
        )
    edge_ids = due(conn, group_id, live.max_age_days)
    out = Enforcement(
        group_id=group_id, policy=live, edge_ids=edge_ids, dry_run=dry_run
    )
    if dry_run or not edge_ids:
        return out

    # Through erasure, not beside it: the cascade across embeddings, audit
    # entries, read events and eval rows is its job, and it writes the
    # erasure_event that makes this provable afterwards.
    #
    # The plan is built here rather than by `erasure.plan`, which selects by
    # fact, entity or whole scope and has no notion of age. That leaves one
    # thing it would have done for a subject request and this has to do
    # itself: an entity whose last fact just went is left behind as a bare
    # name, and a name is often the personal data. Reusing erasure's own two
    # helpers rather than copying the SQL, because a second copy of an
    # orphan query is a second thing to get wrong.
    touched = erasure._touched_nodes(conn, edge_ids)
    orphans = erasure._orphans(conn, group_id, touched, edge_ids)
    plan = erasure.Plan(
        verb="retention", group_id=group_id, edge_ids=edge_ids, node_ids=orphans
    )
    # The reason carries the POLICY, not the fact. erasure_event holds counts,
    # a scope, a verb and who asked, and deliberately no fact text - a
    # compliance record listing what was deleted is the thing retention is
    # supposed to have removed.
    erasure.erase(
        conn, group_id, plan,
        requested_by=requested_by,
        reason=f"retention policy {live.id}: {live.max_age_days} days, {live.reason}",
        request_ref=ticket,
    )
    out.erased = len(edge_ids)
    out.nodes_erased = len(orphans)

    # Postgres keeps the deleted tuple until a VACUUM, so without this the
    # facts are logically gone and physically readable by anyone with the raw
    # files. A retention policy exists to say "we no longer hold this", and
    # that claim is false until the pages are reclaimed. It cannot run inside
    # a transaction, which is why it is here rather than inside `erase`, and
    # `connect` is autocommit so this is outside one. `forget` does the same.
    erasure.vacuum(conn, group_id)

    _logger.info(
        "retention_enforced",
        extra={"fields": {
            "group_id": group_id, "policy_id": live.id,
            "max_age_days": live.max_age_days, "erased": out.erased,
            "nodes_erased": out.nodes_erased,
        }},
    )
    return out
