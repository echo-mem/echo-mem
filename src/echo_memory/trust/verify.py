"""Recording that a claim was checked, and reading back what that is worth.

`states` carries the argument for why this is six states and not a score.
This is the table behind them: append only, one row per check, and the current
state of a fact is the newest row for its edge. Nothing updates and nothing
deletes, so "verified on the 3rd, disputed on the 9th, verified again on the
11th" stays answerable, and the question "who changed their mind" has an
answer rather than a last word.

## Expiry degrades, it never hides

A `verified` row whose `expires_at` has passed reads as `STALE`, and the row
is left exactly as it was. Two reasons, and both are load bearing:

- A lapsed check is not a refutation. The fact is still returned, still
  findable, and only ranks lower. `query_memory` states the governing
  principle on COSINE_FLOOR: hiding a real memory is worse than including a
  mediocre one, and expiry is the easiest way to accidentally build a
  mechanism that hides thousands of them on a timer.
- Rewriting the row to say `stale` would destroy the evidence that the check
  ever passed, which is the only thing a reader can use to decide whether to
  re-run it.

A lapsed `rejected` or `disputed` is deliberately left alone. `expires_at`
says how long a positive check is good for; applying it to a refusal would
make suppression quietly expire on a clock, which is a different decision and
not one this module is entitled to make.

## The clock is the database's

`expires_at` is compared against `now()` inside the query rather than a
Python timestamp, and `now=` exists for tests rather than for callers. A few
tens of milliseconds of skew between a container and its host has already
turned "3 days left" into 4 in this project, and a fact flipping between
`verified` and `stale` depending on which process asked would be the same bug
with a worse symptom.

## Reading is batched because the read path is

`states_of` is one query for every candidate a read is about to return.
`state_of` is written in terms of it rather than beside it: a second copy of
this derivation is how a reader goes blind, which this codebase has paid for
twice.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime

from echo_memory.infra.logging import get_logger
from echo_memory.trust import states, vattest

_logger = get_logger("verify")


class VerificationError(Exception):
    """The request names something that cannot be done, and why."""


def record(conn, group_id: str, edge_id: str, state: str, *,
           verifier_kind: str, verifier_id: str, method: str,
           policy_id: str | None = None,
           evidence_ids: Sequence[str] = (),
           scope: dict | None = None,
           expires_at: datetime | str | None = None,
           detail: str | None = None) -> int:
    """Append a verification and attest it. Returns the new row's id.

    The state and the verifier kind are checked here and not only by the
    CHECK constraints, because a CheckViolation names a constraint and never
    the vocabulary it was enforcing. A caller that wrote `"VERIFIED"` deserves
    to be told the six values.

    Any of the six states may be recorded. The authoritative and advisory
    split is not a restriction on what a verifier may write - a rule
    legitimately records `evidence_backed` - it is a restriction on what
    retrieval may do with the result, and it belongs where that decision is
    made.

    The attestation is in the same transaction as the row. A verification that
    is attested in a second step is attested for the rows nobody is
    suspicious about.
    """
    if state not in states.ALL_STATES:
        raise VerificationError(
            f"unknown trust state {state!r}; one of {list(states.ALL_STATES)}"
        )
    if verifier_kind not in states.VERIFIER_KINDS:
        raise VerificationError(
            f"unknown verifier kind {verifier_kind!r}; one of "
            f"{list(states.VERIFIER_KINDS)}"
        )

    row = conn.execute(
        """INSERT INTO public.fact_verification
               (edge_id, group_id, state, verifier_kind, verifier_id, method,
                policy_id, evidence_ids, scope, expires_at, detail)
           VALUES (%s::graphid, %s, %s, %s, %s, %s, %s, %s, %s::jsonb,
                   %s::timestamptz, %s)
           RETURNING id""",
        (edge_id, group_id, state, verifier_kind, verifier_id, method,
         policy_id, list(evidence_ids),
         json.dumps(scope) if scope is not None else None,
         expires_at, detail),
    ).fetchone()
    verification_id = int(row[0])
    vattest.attest(conn, group_id, verification_id)
    _logger.info(
        "verification_recorded",
        extra={"fields": {
            "group_id": group_id, "edge_id": edge_id, "state": state,
            "verifier_kind": verifier_kind, "verifier_id": verifier_id,
            "verification_id": verification_id,
            "n_evidence": len(list(evidence_ids)),
        }},
    )
    return verification_id


def states_of(conn, group_id: str, edge_ids: Sequence[str], *,
              now: datetime | str | None = None) -> dict[str, str]:
    """The current trust state of each of these facts, in ONE query.

    Every id passed in comes back, `UNVERIFIED` for the facts with no row.
    That is almost every fact in any real store - everything written before
    this existed - so a version that omitted them would hand every caller a
    default to remember, and a caller that re-derives a default is a caller
    that gets it wrong where the tests do not look.

    One query and not one per fact: this runs over every candidate on the read
    path, and an N-query version of it would make the trust channel the most
    expensive thing in a read.
    """
    if not edge_ids:
        return {}
    current = dict.fromkeys(edge_ids, states.UNVERIFIED)
    rows = conn.execute(
        # DISTINCT ON with the id descending is the newest row per edge, which
        # is what the (edge_id, id DESC) index in migration 0032 exists for.
        """SELECT DISTINCT ON (edge_id)
                  edge_id::text, state,
                  (state = %s AND expires_at IS NOT NULL
                   AND expires_at <= coalesce(%s::timestamptz, now())) AS lapsed
             FROM public.fact_verification
            WHERE group_id = %s
              AND edge_id = ANY(%s::text[]::graphid[])
            ORDER BY edge_id, id DESC""",
        (states.VERIFIED, now, group_id, list(edge_ids)),
    ).fetchall()
    for edge_id, state, lapsed in rows:
        current[edge_id] = states.STALE if lapsed else state
    return current


def state_of(conn, group_id: str, edge_id: str, *,
             now: datetime | str | None = None) -> str:
    """One fact's current trust state, `UNVERIFIED` if it has never been
    checked. Delegates to `states_of` so there is exactly one place where a
    newest row becomes a state."""
    return states_of(conn, group_id, [edge_id], now=now)[edge_id]


def summary(conn, group_id: str, edge_ids: Sequence[str]) -> dict[str, int]:
    """How many of these facts are in each state, for a response's
    `trust_summary`.

    Every state in `ALL_STATES` gets a count, including the zeros. Six small
    integers cost nothing and mean a reader can compare two answers without
    first working out whether a missing key is a zero or an older version of
    this field.
    """
    counts = dict.fromkeys(states.ALL_STATES, 0)
    for state in states_of(conn, group_id, edge_ids).values():
        counts[state] = counts.get(state, 0) + 1
    return counts


def suppressed(conn, group_id: str, edge_ids: Sequence[str]) -> set[str]:
    """Which of these facts must not appear in an answer at all.

    Exactly the `rejected` ones, because `states.SUPPRESSED` has exactly one
    member and it is the one an authorised verifier had to choose on purpose.
    Nothing computed can land here: a rule that could suppress in bulk would
    be a denial of service on your own memory.
    """
    current = states_of(conn, group_id, edge_ids)
    return {
        edge_id for edge_id, state in current.items()
        if states.suppresses(state)
    }


def ranked(conn, group_id: str, edge_ids: Sequence[str]) -> list[str]:
    """`edge_ids` reordered by trust weight, most trusted first.

    Only the ids passed in, and every one of them. This enters retrieval as
    one more weighted list for RRF to fuse over the set the content channels
    already found, which is the restraint `salience` spells out at length: the
    graph hop introduced candidates and cost -0.142 MRR on the shape it was
    meant to help least, because anything that can add facts to an answer can
    add wrong ones. A trust signal is an even worse place to relax that -
    "this fact is verified" is not an answer to an unrelated question.

    Stable within a weight, so facts this has nothing to say about keep the
    order they arrived in and a scope that has never verified anything gets
    its input back unchanged.
    """
    if not edge_ids:
        return []
    current = states_of(conn, group_id, edge_ids)
    return sorted(
        edge_ids, key=lambda eid: -states.weight(current[eid])
    )
