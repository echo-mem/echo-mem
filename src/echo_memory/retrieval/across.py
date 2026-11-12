"""One ranked answer assembled from several scopes.

`query_memory` takes one group_id and has since the first migration. That is
the right primitive and it is not changing. What was missing is the thing a
customer with a `dev` memory and a `prod` memory actually wants: ask once, get
the answer from whichever holds it.

The two halves of this already existed and had never been joined.
`reciprocal_rank_fusion` has always been generic over N weighted ranked lists of
opaque ids - it does not care that today's lists are channels rather than
scopes. And the hosted overview has always enumerated an account's groups and
looped a query per group, but only ever to COUNT them. So this is a fan out and
one fusion, over a pipeline that is already measured.

Doing it that way rather than teaching query_memory about several scopes is a
deliberate choice, and the reason is measurement. A single-scope answer here is
byte identical to what query_memory returns - there is a test - so every number
the eval harness has ever produced is still the baseline. A rewritten ranker
would have thrown that away and needed re-tuning from scratch.

## Where the weight question actually lands

Unweighted RRF gives every list's top slot the same value, 1/(k+1), whoever
produced it. For scopes that is not obviously right: your own `prod` memory's
best hit and a rarely-used `scratch` memory's best hit arrive worth the same.

That is not a new problem. It is EXACTLY the problem that made two separate
attempts at graph expansion fail in this codebase, documented in fusion.py: a
list whose first entry is a fact the query did not ask for cost -0.142 MRR on
entity_pair while buying +0.003 on the shape it was built for. The lesson was
that the fix is a weight, and that nothing but the eval decides the number.

So weights are a parameter here with a default of 1.0, which is plain RRF and
what a single-scope caller already gets. `routing.py` can propose them and the
harness can score them. This module does not choose.

## The second boundary check

Cloud resolves which memories a principal may read before calling this, and
that is the control that matters. This module then verifies, against the
database, that every fact it is about to return belongs to one of the group_ids
it was given.

That is not redundant. It is the check that holds when the first one is wrong,
and the first one is a list built by string concatenation in another repository.
One indexed query over the ids already in hand is a cheap place to find out that
an authorization bug exists, and the alternative to finding out here is finding
out from a customer.
"""

from __future__ import annotations

import time

from echo_memory.infra.db import GRAPH_NAME as GRAPH
from echo_memory.infra.logging import get_logger
from echo_memory.retrieval import envelope as context_envelope
from echo_memory.retrieval.fusion import K as RRF_K
from echo_memory.retrieval.fusion import reciprocal_rank_fusion
from echo_memory.retrieval.query_memory import (
    DEFAULT_TOP_K,
    MAX_TOP_K,
    ValidationError,
    query_memory,
)

#: How many scopes one call may fan out over.
#:
#: Eight, and it is a latency bound rather than a capability claim. Each scope
#: is a full query_memory - two indexed candidate queries, an HNSW probe and a
#: fusion - so the cost is linear in this number and a caller that asks for
#: forty gets a slow answer rather than a better one. A customer who genuinely
#: needs more should be routing (see routing.py), not fanning out.
MAX_GROUPS = 8

_logger = get_logger("query_across")


class ScopeViolation(Exception):
    """A fact came back that does not belong to any scope this call was
    authorised for.

    Raised rather than filtered. A filter would turn an authorization bug into
    a quietly thinner answer, which is the failure mode nobody notices; this
    way the request fails, somebody looks, and no cross-tenant row is returned
    in the meantime.
    """


def _verify_scope(conn, edge_ids: list[str], allowed: list[str]) -> None:
    """Refuse unless every edge belongs to one of `allowed`.

    Reads group_id off the edge table rather than through Cypher, for the same
    reason _graph_candidates does: an unbound MATCH expands every FACT edge in
    the database before filtering.

    The projection is compared raw. `->>` returns the real text, so there is
    nothing to decode, and a third hand-rolled decoder here would be the
    fourth copy of the one infra.db.agtype_str exists to replace - which is
    for the Cypher path and would be wrong on this one. Stripping quotes was
    worse than useless: it could only ever turn an authorised group id into an
    unauthorised one and fail a read that should have succeeded.
    """
    if not edge_ids:
        return
    rows = conn.execute(
        f"""SELECT e.id::text, (e.properties ->> '"group_id"'::agtype)
              FROM {GRAPH}."FACT" e
             WHERE e.id = ANY(SELECT unnest(%s::text[])::graphid)""",
        ([str(i) for i in edge_ids],),
    ).fetchall()
    permitted = set(allowed)
    stray = [
        (edge_id, str(gid))
        for edge_id, gid in rows
        if str(gid) not in permitted
    ]
    if stray:
        raise ScopeViolation(
            f"{len(stray)} fact(s) outside the authorised scopes reached the "
            f"fusion: {stray[:3]}. Authorised: {sorted(permitted)}. This is an "
            f"authorization defect in the caller, not a retrieval miss."
        )


def _validate(group_ids: list[str], top_k: int) -> None:
    if not isinstance(group_ids, (list, tuple)) or not group_ids:
        raise ValidationError("query_across needs at least one group_id")
    if len(set(group_ids)) != len(group_ids):
        # Refused rather than deduplicated: the same scope twice would get two
        # votes in the fusion, which silently doubles its weight.
        raise ValidationError(
            f"group_ids must be distinct; a repeated scope would vote twice in "
            f"the fusion and silently double its weight. Got {group_ids}"
        )
    if len(group_ids) > MAX_GROUPS:
        raise ValidationError(
            f"query_across takes at most {MAX_GROUPS} scopes, got "
            f"{len(group_ids)}. Route to the ones worth asking rather than "
            f"fanning out; see retrieval/routing.py"
        )
    for gid in group_ids:
        if not isinstance(gid, str) or not gid.strip():
            raise ValidationError(f"every group_id must be a non-empty string, got {gid!r}")
    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k < 1:
        raise ValidationError(f"top_k must be a positive integer, got {top_k!r}")
    if top_k > MAX_TOP_K:
        raise ValidationError(f"top_k must be at most {MAX_TOP_K}, got {top_k}")


def query_across(
    conn,
    group_ids: list[str],
    query: str | None,
    top_k: int = DEFAULT_TOP_K,
    embedder=None,
    *,
    weights: dict[str, float] | None = None,
    rrf_k: int | None = None,
    with_context: bool = False,
    **single_scope_kwargs,
) -> dict:
    """Facts from several scopes, fused into one ranked answer.

    `group_ids` must already be the scopes the caller is authorised for. This
    function does not know what a principal is and must not learn: the hosted
    service resolves authorization and passes strings, which is the property
    that keeps a tenancy decision in one place.

    With a single group_id the result is byte identical to query_memory, which
    is pinned by a test. That is what makes every existing measurement still
    the baseline.

    Returns the usual {"facts": [...]} with each fact carrying `source`, the
    group_id it came from. Callers that must not leak the group id shape - the
    hosted service - map it to a memory name before it leaves the building.
    """
    start = time.perf_counter()
    try:
        _validate(list(group_ids), top_k)
    except ValidationError as e:
        return {"error": str(e)}

    single = len(group_ids) == 1

    per_scope: dict[str, dict] = {}
    ranked_lists: list[list[str]] = []
    list_weights: list[float] = []
    errors: dict[str, str] = {}
    exclusions: dict[str, dict] = {}

    for gid in group_ids:
        # Depth matters more than k in this fusion, and a scope that only
        # returns top_k cannot contribute a fact that ranks 11th in its own
        # scope but 3rd overall. Ask each scope for more than the caller wants.
        depth = top_k if single else min(MAX_TOP_K, max(top_k * 3, 30))
        result = query_memory(
            conn, gid, query, depth, embedder, **single_scope_kwargs
        )
        if "error" in result:
            # One unreadable scope must not lose the others. Recorded and
            # reported rather than raised: a memory that has just been archived
            # should degrade the answer, not fail the request.
            errors[gid] = result["error"]
            continue
        if result.get("excluded_by_erasure") is not None:
            exclusions[gid] = result["excluded_by_erasure"]
        facts = result.get("facts") or []
        if single:
            # Nothing to fuse, and nothing to add. Returned before `source` is
            # attached, because attaching it would mutate the dicts
            # query_memory produced and the single-scope answer would stop
            # being the single-scope answer. That is what the byte-identical
            # test caught, and it is the sort of difference that would
            # otherwise show up as an unexplained delta in a benchmark months
            # later.
            #
            # The context block is the one exception, and it has to be: a
            # single scope read is the COMMON case, so an envelope that only
            # appeared on the fan out would mark almost nothing. It is added
            # only when the caller asked for it, which is what keeps the
            # default answer byte identical.
            if errors:
                result["unreadable_scopes"] = errors
            if with_context:
                block = context_envelope.describe(result.get("facts") or [])
                if block is not None:
                    result["context"] = block
            return result
        for f in facts:
            f["source"] = gid
            per_scope[str(f["fact_id"])] = f
        ranked_lists.append([str(f["fact_id"]) for f in facts])
        list_weights.append(1.0 if weights is None else float(weights.get(gid, 1.0)))

    if not ranked_lists:
        out: dict = {"facts": []}
        if errors:
            out["unreadable_scopes"] = errors
        return out

    fused = reciprocal_rank_fusion(
        ranked_lists,
        k=rrf_k if rrf_k is not None else RRF_K,
        weights=list_weights,
    )
    order = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))
    chosen = [fact_id for fact_id, _ in order[:top_k]]

    # Defence in depth, against the database, on the ids actually leaving.
    _verify_scope(conn, chosen, list(group_ids))

    facts = [per_scope[fact_id] for fact_id in chosen if fact_id in per_scope]

    result = {"facts": facts}
    considered = [g for g in group_ids if g not in errors]
    result["routing"] = {
        "considered": considered,
        "weights": {g: (1.0 if weights is None else float(weights.get(g, 1.0)))
                    for g in considered},
    }
    if errors:
        result["unreadable_scopes"] = errors

    # Erasure exclusions are per scope, so an as_of answer spanning scopes has
    # to report every scope that had one or it understates what is missing. The
    # whole reason to have bi-temporality is that a reader can trust it, and a
    # silently thinner past is worse than a smaller one that admits it.
    if exclusions:
        result["excluded_by_erasure"] = exclusions

    if with_context:
        block = context_envelope.describe(facts)
        if block is not None:
            result["context"] = block

    _logger.info(
        "query_across",
        extra={"fields": {
            "n_scopes": len(group_ids),
            "n_unreadable": len(errors),
            "n_results": len(facts),
            "duration_ms": round((time.perf_counter() - start) * 1000, 1),
        }},
    )
    return result
