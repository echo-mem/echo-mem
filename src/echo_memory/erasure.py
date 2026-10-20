"""Actual deletion, for when supersession is not enough.

GDPR Article 17 and India's DPDP Act both require a data subject's personal
data to be deleted on request. The store's whole model is that nothing is
deleted: a fact that stops being true gets t_invalid set and stays on the
graph, which is correct for memory and insufficient for law. docs/ERASURE.md
is the design record; this is the implementation.

Deleting the edge erases almost nothing. Fact text and content derived from it
live in seven places, and the two most easily missed are lexical_term, which
keeps every lexeme of an erased fact queryable by vocabulary, and the
embedding tables, where inversion recovers a meaningful fraction of the source
text from a 384 dimension vector. Deleting the row and keeping the vector is
obfuscation, not erasure.

ONE TRANSACTION, which is not what the design doc first assumed. The recorded
belief was that a script mixing Cypher with ordinary SQL has no transaction at
all, so erasure was planned as an ordered, resumable sequence whose every
crash state was safe. Measured on 2026-10-20 and the belief was
mis-attributed: `infra.db.connect` returns an autocommit connection, so
nothing is in a transaction unless something opens one. Inside an explicit
`conn.transaction()`, a direct DELETE on the AGE label table and a Cypher
DELETE both roll back cleanly. So erasure is atomic, and the resumption
machinery it would otherwise have needed does not exist.

audit_entry rows are redacted, never deleted, for two reasons the schema
decides rather than the design: trial_observation.audit_entry_id is a NO
ACTION foreign key, so deleting a referenced row errors; and summary is NOT
NULL, so it is replaced with a fixed string rather than nulled. The row stays
as evidence a mutation happened, with its text gone and its ids stripped.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass, field

from echo_memory.infra.db import GRAPH_NAME as GRAPH
from echo_memory.infra.logging import get_logger

_logger = get_logger("erasure")

# The HMAC key for subject_digest. Read from the environment and never
# generated or stored: the point of the digest is that the erasure record does
# not hold the erased name, and a key sitting in the same database lets a dump
# holder recompute every candidate name offline. Unset means counts-only
# evidence, which is honest, rather than a digest that protects nothing.
KEY_ENV = "ECHO_MEMORY_ERASURE_KEY"

# audit_entry.summary is NOT NULL, so it cannot be nulled like the other three
# text columns. A fixed string says a mutation happened here and its
# description is gone, which is the true statement.
REDACTED = "[redacted by erasure]"

_GROUP = """(properties ->> '"group_id"'::agtype)"""
_NAME = """(properties ->> '"name"'::agtype)"""
_RELATION = """(properties ->> '"relation_type"'::agtype)"""


class ErasureError(Exception):
    """The request names something that is not there, or is ambiguous."""


@dataclass
class Plan:
    """What an erasure would remove. Produced without removing anything, so
    `forget` can show it and exit unless the caller confirms."""

    verb: str
    group_id: str
    edge_ids: list[str] = field(default_factory=list)
    node_ids: list[str] = field(default_factory=list)
    subject: str | None = None

    @property
    def n_edges(self) -> int:
        return len(self.edge_ids)

    @property
    def n_nodes(self) -> int:
        return len(self.node_ids)

    def is_empty(self) -> bool:
        return not self.edge_ids and not self.node_ids


def subject_digest(subject: str | None) -> str | None:
    """HMAC of a normalised subject, or None when no key is configured.

    Answers "did you erase X" by recomputation, without the erasure record
    holding X. Names are low entropy, so a leaked key makes this a dictionary
    attack over candidate names; that trade is accepted and documented rather
    than hidden, because the alternative is storing the name, which is worse.
    """
    key = os.environ.get(KEY_ENV, "")
    if not key or not subject:
        return None
    normalised = subject.strip().casefold().encode()
    return hmac.new(key.encode(), normalised, hashlib.sha256).hexdigest()


def _versions_of(conn, group_id: str, edge_id: str) -> list[str]:
    """Every version of the triple this edge belongs to.

    Edge identity is (source, target, relation_type) and supersession keeps the
    old versions, so erasing one version leaves a chain with a hole. A hole
    reads as corruption and marks exactly where something was removed, which
    is the opposite of what an erasure is for. So one version means all of
    them.
    """
    row = conn.execute(
        f"""SELECT start_id::text, end_id::text, {_RELATION}
              FROM {GRAPH}."FACT"
             WHERE id = %s::text::graphid AND {_GROUP} = %s""",
        (edge_id, group_id),
    ).fetchone()
    if row is None:
        raise ErasureError(
            f"no fact {edge_id!r} in this scope. `echo-memory why {edge_id}` "
            f"shows whether it exists elsewhere."
        )
    start, end, relation = row
    rows = conn.execute(
        f"""SELECT id::text
              FROM {GRAPH}."FACT"
             WHERE {_GROUP} = %s
               AND start_id = %s::text::graphid
               AND end_id = %s::text::graphid
               AND {_RELATION} = %s""",
        (group_id, start, end, relation),
    ).fetchall()
    return [r[0] for r in rows]


def _node_by_name(conn, group_id: str, name: str) -> str:
    """The node for a name or one of its aliases, by the same two step lookup
    resolution uses, so `forget --entity` and a write agree on what a name
    means."""
    row = conn.execute(
        f"""SELECT id::text FROM {GRAPH}."Node"
             WHERE {_GROUP} = %s AND lower({_NAME}) = lower(%s) LIMIT 1""",
        (group_id, name),
    ).fetchone()
    if row is None:
        row = conn.execute(
            f"""SELECT id::text FROM {GRAPH}."Node"
                 WHERE {_GROUP} = %s
                   AND EXISTS (
                       SELECT 1 FROM jsonb_array_elements_text(
                           COALESCE((properties ->> '"aliases"'::agtype)::jsonb, '[]'::jsonb)
                       ) AS alias WHERE lower(alias) = lower(%s)
                   ) LIMIT 1""",
            (group_id, name),
        ).fetchone()
    if row is None:
        raise ErasureError(
            f"no entity named {name!r} in this scope. `forget --search {name!r}` "
            f"looks for it in fact text instead."
        )
    return row[0]


def _incident_edges(conn, group_id: str, node_id: str) -> list[str]:
    """Every edge touching this node, in either direction, every version.

    Read off the edge table rather than through Cypher: AGE expands a MATCH
    before filtering, so the pattern form walks every FACT edge in the
    database rather than using the (group_id, start_id) and (group_id, end_id)
    indexes migration 0013 created.
    """
    rows = conn.execute(
        f"""SELECT id::text FROM {GRAPH}."FACT"
             WHERE {_GROUP} = %s
               AND (start_id = %s::text::graphid OR end_id = %s::text::graphid)""",
        (group_id, node_id, node_id),
    ).fetchall()
    return [r[0] for r in rows]


def _orphans(conn, group_id: str, candidates: list[str], erased: list[str]) -> list[str]:
    """Of these nodes, the ones with no edge left once `erased` is gone.

    Only nodes an erased edge touched are considered. A node that was already
    isolated before this erasure was not part of the request, and removing it
    would be this command quietly doing more than it was asked.
    """
    if not candidates:
        return []
    rows = conn.execute(
        f"""SELECT n.id::text
              FROM {GRAPH}."Node" n
             WHERE n.id = ANY(%s::text[]::graphid[])
               AND NOT EXISTS (
                   SELECT 1 FROM {GRAPH}."FACT" f
                    WHERE (f.start_id = n.id OR f.end_id = n.id)
                      AND NOT (f.id = ANY(%s::text[]::graphid[]))
               )""",
        (candidates, erased or ["0"]),
    ).fetchall()
    return [r[0] for r in rows]


def plan(
    conn,
    group_id: str,
    *,
    fact: str | None = None,
    entity: str | None = None,
    scope: bool = False,
) -> Plan:
    """What would be erased. Exactly one of fact, entity or scope."""
    given = [bool(fact), bool(entity), bool(scope)]
    if sum(given) != 1:
        raise ErasureError("pass exactly one of --fact, --entity or --scope")

    if scope:
        edges = [
            r[0]
            for r in conn.execute(
                f"""SELECT id::text FROM {GRAPH}."FACT" WHERE {_GROUP} = %s""",
                (group_id,),
            ).fetchall()
        ]
        nodes = [
            r[0]
            for r in conn.execute(
                f"""SELECT id::text FROM {GRAPH}."Node" WHERE {_GROUP} = %s""",
                (group_id,),
            ).fetchall()
        ]
        return Plan("scope", group_id, edges, nodes, subject=group_id)

    if fact:
        edges = _versions_of(conn, group_id, fact)
        touched = _touched_nodes(conn, edges)
        return Plan("fact", group_id, edges, _orphans(conn, group_id, touched, edges))

    node_id = _node_by_name(conn, group_id, entity or "")
    edges = _incident_edges(conn, group_id, node_id)
    touched = [n for n in _touched_nodes(conn, edges) if n != node_id]
    nodes = [node_id] + _orphans(conn, group_id, touched, edges)
    return Plan("entity", group_id, edges, nodes, subject=entity)


def _touched_nodes(conn, edge_ids: list[str]) -> list[str]:
    if not edge_ids:
        return []
    rows = conn.execute(
        f"""SELECT DISTINCT unnest(ARRAY[start_id::text, end_id::text])
              FROM {GRAPH}."FACT" WHERE id = ANY(%s::text[]::graphid[])""",
        (edge_ids,),
    ).fetchall()
    return [r[0] for r in rows]


def candidates(conn, group_id: str, term: str) -> list[tuple[str, str]]:
    """Facts whose text mentions a term, for a human to confirm.

    The limit this exists to admit: a person named inside the text of a fact
    whose two endpoints are other entities is not reachable structurally.
    "Ayush approved the Razorpay change" is an edge between an approval and
    Razorpay, and erasing the node Ayush does not touch it.

    Finding those needs a text scan, and a text scan must not act on its own.
    A false positive destroys a fact nobody asked to erase and there is no
    undo. So this returns candidates and the caller decides.
    """
    rows = conn.execute(
        f"""SELECT id::text, (properties ->> '"fact"'::agtype)
              FROM {GRAPH}."FACT"
             WHERE {_GROUP} = %s
               AND (properties ->> '"fact"'::agtype) ILIKE %s
             ORDER BY id""",
        (group_id, f"%{term}%"),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def erase(
    conn,
    group_id: str,
    target: Plan,
    *,
    requested_by: str,
    reason: str | None = None,
    request_ref: str | None = None,
) -> dict:
    """Remove everything in the plan, atomically, and record that it happened.

    Returns counts. The erasure_event row holds the same counts, a scope, a
    verb, who asked and a ticket reference, and deliberately no fact text and
    no entity name: a compliance record listing the names of people who asked
    to be forgotten is the thing they asked to not exist.
    """
    if target.is_empty():
        raise ErasureError("nothing to erase")

    edges = target.edge_ids
    nodes = target.node_ids
    redacted = 0

    with conn.transaction():
        if edges:
            conn.execute(
                "DELETE FROM public.fact_embedding WHERE edge_id = ANY(%s::text[]::graphid[])",
                (edges,),
            )
            conn.execute(
                "DELETE FROM public.causal_hint_scan WHERE edge_id = ANY(%s::text[]::graphid[])",
                (edges,),
            )
            # eval_judgement.edge_id is text, not graphid. The id columns are
            # not one type across this schema and a mismatch here is silently
            # false for every row rather than an error.
            conn.execute("DELETE FROM public.eval_judgement WHERE edge_id = ANY(%s)", (edges,))

            # Strip the pointers, keep the rows. These are evidence that a read
            # or an evaluation happened, which the unit meter and criterion 6
            # both count, and an id on its own is not content.
            conn.execute(
                """UPDATE public.eval_run
                      SET returned = COALESCE((
                              SELECT array_agg(x) FROM unnest(returned) x
                               WHERE NOT x = ANY(%s)), '{}')
                    WHERE returned && %s""",
                (edges, edges),
            )
            conn.execute(
                """UPDATE public.read_event
                      SET returned_fact_ids = COALESCE((
                              SELECT array_agg(x) FROM unnest(returned_fact_ids) x
                               WHERE NOT x = ANY(%s)), '{}')
                    WHERE returned_fact_ids && %s""",
                (edges, edges),
            )

            redacted = conn.execute(
                """UPDATE public.audit_entry
                       SET before_fact = NULL,
                           after_fact = NULL,
                           resolution_detail = NULL,
                           summary = %s,
                           affected_edge_ids = COALESCE((
                               SELECT array_agg(x) FROM unnest(affected_edge_ids) x
                                WHERE NOT x::text = ANY(%s)), '{}'::graphid[])
                     WHERE affected_edge_ids && %s::text[]::graphid[]""",
                (REDACTED, edges, edges),
            ).rowcount

        if nodes:
            conn.execute(
                "DELETE FROM public.node_embedding WHERE node_id = ANY(%s::text[]::graphid[])",
                (nodes,),
            )
            # The note is free text an agent wrote and may quote a fact, so it
            # goes. Criterion 6 keeps its counts and loses its narrative, which
            # is the right way round.
            conn.execute(
                """UPDATE public.trial_observation
                      SET note = NULL,
                          node_ids = COALESCE((
                              SELECT array_agg(x) FROM unnest(node_ids) x
                               WHERE NOT x = ANY(%s)), '{}')
                    WHERE node_ids && %s""",
                (nodes, nodes),
            )
            redacted += conn.execute(
                """UPDATE public.audit_entry
                       SET before_fact = NULL,
                           after_fact = NULL,
                           resolution_detail = NULL,
                           summary = %s
                     WHERE affected_node_id = ANY(%s::text[]::graphid[])""",
                (REDACTED, nodes),
            ).rowcount

        # The content itself, last of the deletes.
        if edges:
            conn.execute(
                f"""DELETE FROM {GRAPH}."FACT" WHERE id = ANY(%s::text[]::graphid[])""",
                (edges,),
            )
        if nodes:
            conn.execute(
                f"""DELETE FROM {GRAPH}."Node" WHERE id = ANY(%s::text[]::graphid[])""",
                (nodes,),
            )

        # Drop the scope's corpus statistics rather than decrementing them.
        # Correct decrementing needs the text that has just been destroyed, has
        # to get the arithmetic right, and has to delete any lexical_term row
        # reaching zero or the lexeme lingers as evidence of an erased fact's
        # vocabulary. Deleting them cannot do any of that wrong: null reads as
        # stale to both readers, so the next write rebuilds from the survivors.
        conn.execute("DELETE FROM public.lexical_term WHERE group_id = %s", (group_id,))
        conn.execute("DELETE FROM public.lexical_scope WHERE group_id = %s", (group_id,))

        row = conn.execute(
            """INSERT INTO public.erasure_event
                   (status, completed_at, group_id, verb, subject_digest,
                    n_edges, n_nodes, n_audit_rows, reason, requested_by, request_ref)
               VALUES ('completed', now(), %s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (
                group_id,
                target.verb,
                subject_digest(target.subject),
                target.n_edges,
                target.n_nodes,
                redacted,
                reason,
                requested_by,
                request_ref,
            ),
        ).fetchone()

    result = {
        "erasure_id": row[0],
        "verb": target.verb,
        "group_id": group_id,
        "edges": target.n_edges,
        "nodes": target.n_nodes,
        "audit_rows_redacted": redacted,
    }
    _logger.info("erasure_completed", extra={"erasure": result})
    return result


def vacuum(conn, group_id: str) -> None:
    """Reclaim the pages the deletes left behind.

    Postgres does not zero a page on DELETE; the tuple survives until VACUUM
    and the change survives in WAL. Runs after the transaction has committed,
    because VACUUM cannot run inside one. A failure here leaves the data
    logically erased and physically recoverable by someone with the raw files,
    so it is reported rather than swallowed.
    """
    for table in (
        f'{GRAPH}."FACT"',
        f'{GRAPH}."Node"',
        "public.fact_embedding",
        "public.node_embedding",
        "public.audit_entry",
        "public.causal_hint_scan",
        "public.trial_observation",
    ):
        conn.execute(f"VACUUM {table}")


def excluded_after(conn, group_id: str, as_of: int | None) -> dict | None:
    """What an as_of answer is missing because it was erased, or None.

    An erasure that completed AFTER the as_of moment removed facts that
    existed at that moment, so those are the ones that make the answer
    thinner. An erasure before it removed facts that were already gone by
    then and changes nothing about what was believed.

    This is the honesty cost of erasure, paid out loud. A query over a window
    spanning an erasure returns less than it would have, with no trace of why;
    silently returning a thinner past turns the store's most defensible
    property into one nobody can rely on. The whole reason to have
    bi-temporality is that a reader can trust it.

    Only completed erasures count. A row left 'started' removed an unknown
    amount and must not be reported as a clean number.
    """
    if as_of is None:
        return None
    rows = conn.execute(
        """SELECT completed_at, n_edges
             FROM public.erasure_event
            WHERE group_id = %s
              AND status = 'completed'
              AND completed_at > to_timestamp(%s)
            ORDER BY completed_at""",
        (group_id, as_of),
    ).fetchall()
    if not rows:
        return None
    total = sum(r[1] for r in rows)
    when = ", ".join(r[0].date().isoformat() for r in rows)
    return {
        "n_facts": total,
        "erasures": [{"at": r[0].isoformat(), "n_facts": r[1]} for r in rows],
        "note": (
            f"This answer excludes {total} fact(s) erased on {when}, after the "
            f"as_of moment. They existed then and have since been deleted on "
            f"request, so this is not the whole of what was believed."
        ),
    }


def history(conn, group_id: str) -> list[dict]:
    """Every erasure recorded for this scope, newest first.

    The operator surface for two things. One, answering "has anything been
    erased here" without reading the table by hand. Two, the restore runbook:
    a dump taken before an erasure still contains the erased data and
    restoring it brings the data back, so after any restore these have to be
    replayed. Nobody derives that at 2am, so it has to be readable.

    Includes rows left 'started', which are crashed erasures that removed an
    unknown amount, because those are exactly the ones an operator needs to
    see rather than the ones to hide.
    """
    rows = conn.execute(
        """SELECT id, status, completed_at, requested_at, verb,
                  n_edges, n_nodes, n_audit_rows, reason, requested_by, request_ref
             FROM public.erasure_event
            WHERE group_id = %s
            ORDER BY requested_at DESC""",
        (group_id,),
    ).fetchall()
    return [
        {
            "id": r[0],
            "status": r[1],
            "completed_at": r[2],
            "requested_at": r[3],
            "verb": r[4],
            "n_edges": r[5],
            "n_nodes": r[6],
            "n_audit_rows": r[7],
            "reason": r[8],
            "requested_by": r[9],
            "request_ref": r[10],
        }
        for r in rows
    ]
