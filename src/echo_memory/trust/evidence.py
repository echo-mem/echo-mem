"""The artefact behind a claim, recorded apart from the claim.

A fact here records who wrote it and how they came by it, and neither of those
is evidence. `confidence` says `extracted` or `inferred`, which describes the
writer's process: an agent that inferred something confidently and wrongly
leaves exactly the same row as one that read it off a deploy log.

This holds the thing outside the database that can be gone and looked at. A
deployment, a test run, a human correction, a metric, a document, an
attestation head written into a ticket. `uri` says where it is and
`content_digest` says what it said at the time, which is the difference
between citing a document and citing a document that has since been rewritten.

## Evidence is not verification

Nothing in this module decides anything. Pointing a deploy log at a fact does
not make the fact true - the log says the deploy happened, not that the
decision behind it was right - which is why `states.EVIDENCE_BACKED` is a
separate and weaker state than `VERIFIED`, and why the judgement lives in
`verify` with a named verifier and method behind it. The split is deliberate:
attaching an artefact is cheap and should stay cheap, and anything that turned
a cheap attachment into a trust claim would make `verified` mean nothing.

## Late arrival is the normal case

The test run that confirms a decision happens after the decision was written.
So `link` is a separate call from `record`, and both work long after the write
path has finished. A design that could only attach evidence at write time
would be unable to record the confirmation at all, which is most of what
evidence is.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime

from echo_memory.infra.logging import get_logger

_logger = get_logger("evidence")

#: The kinds an artefact can be, matching the CHECK in migration 0031. Checked
#: here as well as there so a caller with a typo gets the list of kinds back
#: rather than a CheckViolation naming a constraint, which says what is not
#: allowed and never what is.
KINDS = (
    "deployment",
    "test_run",
    "human_correction",
    "metric",
    "external_doc",
    "attestation",
)


class EvidenceError(Exception):
    """The request names something that cannot be done, and why."""


def _new_id() -> str:
    """A fresh evidence id.

    Random rather than a hash of the content, because two reports of the same
    artefact are two observations and collapsing them would lose the second
    reporter and the second time. A deploy log that two agents both cite is
    worth more than one agent citing it twice, and a content-addressed id
    cannot tell those apart.

    Prefixed so an id pasted into a ticket is recognisable as one.
    """
    return "ev_" + uuid.uuid4().hex


def record(conn, group_id: str, kind: str, *, uri: str | None = None,
           detail: dict | None = None, reporter_agent: str,
           reporter_run: str = "", occurred_at: datetime | str | None = None,
           content_digest: str | None = None) -> str:
    """Store one artefact and return its id.

    `occurred_at` is when the artefact came to be and is nullable;
    `received_at` is when we heard, and is not. A test run from last Tuesday
    reported today is evidence about last Tuesday, and a store that kept only
    one of those timestamps would make a stale report look fresh.
    """
    if kind not in KINDS:
        raise EvidenceError(f"unknown evidence kind {kind!r}; one of {list(KINDS)}")

    evidence_id = _new_id()
    conn.execute(
        """INSERT INTO public.evidence
               (id, group_id, kind, uri, content_digest, detail,
                reporter_agent, reporter_run, occurred_at)
           VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s::timestamptz)""",
        (evidence_id, group_id, kind, uri, content_digest,
         json.dumps(detail) if detail is not None else None,
         reporter_agent, reporter_run, occurred_at),
    )
    _logger.info(
        "evidence_recorded",
        extra={"fields": {"group_id": group_id, "kind": kind,
                          "evidence_id": evidence_id,
                          "reporter_agent": reporter_agent}},
    )
    return evidence_id


def link(conn, group_id: str, edge_id: str, evidence_id: str) -> None:
    """Point an artefact at a fact.

    The scope is checked rather than trusted. `fact_evidence` carries its own
    `group_id` for erasure's benefit, which means an insert can name one scope
    while the evidence row belongs to another, and the link would then read
    back under both. Scopes are tenants here, so that is a cross-tenant leak
    with no error attached to it, and the only place to stop it is before the
    insert.

    Linking twice is not an error. Evidence arrives from hooks and retries and
    a second identical link is the same statement, not a new one.
    """
    owner = conn.execute(
        "SELECT group_id FROM public.evidence WHERE id = %s", (evidence_id,)
    ).fetchone()
    if owner is None:
        raise EvidenceError(f"no evidence {evidence_id}")
    if owner[0] != group_id:
        raise EvidenceError(
            f"evidence {evidence_id} belongs to another scope; a link across "
            f"scopes would make it readable from both"
        )
    conn.execute(
        """INSERT INTO public.fact_evidence (edge_id, evidence_id, group_id)
           VALUES (%s::graphid, %s, %s)
           ON CONFLICT DO NOTHING""",
        (edge_id, evidence_id, group_id),
    )


def for_fact(conn, group_id: str, edge_id: str) -> list[dict]:
    """Every artefact linked to one fact, most recently received first.

    Ordered by `received_at` rather than `occurred_at` because the latter is
    nullable, and a sort on a column that is null for human corrections puts
    them wherever the planner feels like.
    """
    rows = conn.execute(
        """SELECT e.id, e.kind, e.uri, e.content_digest, e.detail,
                  e.reporter_agent, e.reporter_run, e.occurred_at,
                  e.received_at, fe.linked_at
             FROM public.fact_evidence fe
             JOIN public.evidence e ON e.id = fe.evidence_id
            WHERE fe.group_id = %s AND fe.edge_id = %s::graphid
            ORDER BY e.received_at DESC, e.id""",
        (group_id, edge_id),
    ).fetchall()
    return [
        {
            "evidence_id": row[0], "kind": row[1], "uri": row[2],
            "content_digest": row[3], "detail": row[4],
            "reporter_agent": row[5], "reporter_run": row[6],
            "occurred_at": row[7], "received_at": row[8], "linked_at": row[9],
        }
        for row in rows
    ]


def count_for(conn, group_id: str, edge_ids: list[str]) -> dict[str, int]:
    """How many artefacts back each of these facts, in one query.

    Every id passed in comes back, with 0 for the facts nothing backs. A
    version that omitted them would be smaller and would hand every caller a
    `.get(edge_id, 0)` to remember, and this codebase has already paid for
    exactly that shape twice: a reader that re-derives a default is a reader
    that gets it wrong somewhere the tests do not look.

    One query and not one per fact, because the read path calls this with
    every candidate it is about to return.
    """
    if not edge_ids:
        return {}
    counts = dict.fromkeys(edge_ids, 0)
    rows = conn.execute(
        """SELECT fe.edge_id::text, count(*)
             FROM public.fact_evidence fe
            WHERE fe.group_id = %s
              AND fe.edge_id = ANY(%s::text[]::graphid[])
            GROUP BY 1""",
        (group_id, list(edge_ids)),
    ).fetchall()
    for edge_id, n in rows:
        counts[edge_id] = int(n)
    return counts
