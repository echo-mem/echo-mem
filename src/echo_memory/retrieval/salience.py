"""How often a fact has actually been recalled, which the store already knows.

Migration 0021 began recording which fact ids every read returned, in
September, so that a claimed cross-tool recall save could be checked against
evidence rather than taken on the caller's word. Retrieval has never looked at
it. On this author's store that is 1,016 reads naming 442 distinct facts, with
one fact returned 167 times and a long tail returned once - and a fact
recalled 167 times ranks today exactly like a fact recalled never.

This is ACT-R's base-level activation, which is the one piece of cognitive
architecture worth copying here because the data to compute it is already on
disk. The formula is the literature's:

    B(fact) = ln( sum over past retrievals k of (now - t_k) ** -d )

Frequency and recency in one number, and it is not the same as either: ten
recalls last year decay below three this week, which is the behaviour a memory
wants and a counter does not have.

What it is NOT is a relevance signal, and that distinction is the whole design
here. A popular fact is not an answer to an unrelated question, so this never
introduces a candidate. It reorders facts the content channels already found,
entering the fusion as one more ranked list over that same set. The worst it
can do is move a fact the query already matched.

That restraint is deliberate and it is the second attempt at this idea in the
project. The graph hop introduced candidates and cost -0.142 MRR on the shape
it was supposed to help least, because anything that can add facts to an
answer can add wrong ones.
"""

import os

from echo_memory.infra.logging import get_logger

_logger = get_logger("salience")

# ACT-R's decay rate. 0.5 is the value the literature uses almost everywhere
# and is not tuned here: this codebase has been burned once by fitting a
# constant to a handful of observations, and a decay exponent chosen on 442
# facts would be exactly that again.
DECAY = float(os.environ.get("ECHO_MEMORY_SALIENCE_DECAY", "0.5"))

# Retrievals closer than this count as one. Without it a single query that
# returns a fact, followed by the hook returning it again a second later,
# reads as two independent recalls, and a tight agent loop can manufacture
# activation by asking the same question repeatedly.
COOLDOWN_SECONDS = int(os.environ.get("ECHO_MEMORY_SALIENCE_COOLDOWN", "60"))

# A retrieval this recent has age zero in whole seconds, and 0 ** -0.5 is a
# division by zero. One second is the smallest honest age.
MIN_AGE_SECONDS = 1


def rank(conn, group_id: str, edge_ids: list[str]) -> list[str]:
    """`edge_ids` reordered by base-level activation, most active first.

    Only the ids passed in, and every one of them comes back: this is a
    reordering, not a filter. Facts never recalled keep their relative order
    at the end, so a scope with no read history returns the input unchanged
    and the fusion sees a list identical to the one it already had.
    """
    if not edge_ids:
        return []
    rows = conn.execute(
        """
        WITH retrievals AS (
            -- One row per fact per retrieval, with near-simultaneous ones
            -- collapsed: date_bin buckets by the cooldown so a burst counts
            -- once rather than once per read.
            SELECT DISTINCT
                   f AS edge_id,
                   date_bin(
                       make_interval(secs => %s),
                       r.at,
                       TIMESTAMPTZ 'epoch'
                   ) AS bucket
              FROM public.read_event r, unnest(r.returned_fact_ids) AS f
             WHERE r.group_id = %s
               AND r.returned_fact_ids IS NOT NULL
               AND f = ANY(%s)
        )
        SELECT edge_id,
               sum(power(
                   greatest(extract(epoch FROM (now() - bucket)), %s), -%s
               )) AS activation
          FROM retrievals
         GROUP BY edge_id
        """,
        (COOLDOWN_SECONDS, group_id, list(edge_ids), MIN_AGE_SECONDS, DECAY),
    ).fetchall()
    activation = {str(edge_id): float(score) for edge_id, score in rows}
    if not activation:
        return list(edge_ids)
    # Stable on the incoming order, so facts with equal activation - which is
    # every fact never recalled - keep the ranking the content channels gave
    # them rather than being shuffled by a sort that knows nothing about them.
    return sorted(edge_ids, key=lambda e: -activation.get(e, 0.0))
