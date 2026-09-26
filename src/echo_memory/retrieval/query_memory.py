"""query_memory: 2-signal retrieval (pgvector + full-text) fused with RRF
(see the design doc's Recommended Approach, v1a section). Both candidate
lists are pre-filtered to active facts (t_invalid IS NULL) before ranking,
not after: see MATHS.local.md §7 for why post-hoc filtering is wrong even
for a plain ranked list, not just for PPR's probability-mass case in v1b."""

import json
import math
import os
import re
import time

from echo_memory.infra.db import GRAPH_NAME as GRAPH
from echo_memory.infra.logging import get_logger, log_query_memory
from echo_memory.retrieval import bm25
from echo_memory.retrieval.fusion import LIST_DEPTH, reciprocal_rank_fusion
from echo_memory.retrieval.fusion import K as RRF_K

DEFAULT_TOP_K = 10

# Facts whose entities seed the traversal. Wider is not obviously better: the
# tenth-ranked fact's neighbours are a long way from the question, and every
# seed adds its whole neighbourhood to a list that then has to outrank the
# content channels.
GRAPH_SEEDS = 5
MAX_TOP_K = 100

# Score floors: a ranker with nothing useful to say shouldn't cast a rank-1
# vote worth as much as a ranker's genuine top match (see MATHS.local.md
# §3). Lowered from an initial 0.3 after it silently hid a real result in
# manual testing: "what database" vs "using SQLite for now" scores 0.281
# with the real embedder, a genuine match, but below 0.3. Query-to-fact
# similarity (short colloquial question vs. a full sentence) runs lower
# than entity-name-to-entity-name similarity (§5's thresholds), so this
# can't reuse those values. Still a placeholder pending real calibration,
# deliberately conservative: hiding a real memory is worse than including
# a mediocre one, which RRF's fusion already discounts by rank anyway.
# Fallback only. The floor that actually runs is measured per store by
# `adaptive_cosine_floor` below; this is what a store too small to measure
# against falls back to.
COSINE_FLOOR = 0.15
TS_RANK_FLOOR = 0.0

# How many random query/fact pairs to sample when measuring the noise floor,
# and how many facts a store needs before measuring is better than guessing.
FLOOR_SAMPLE = 200
FLOOR_MIN_FACTS = 30
# The percentile of that noise distribution to sit above. 95 lets one in twenty
# unrelated facts through, which RRF then discounts by rank.
FLOOR_PERCENTILE = 95

# Maximal Marginal Relevance. 1.0 is pure relevance and reproduces the old
# behaviour; 0.0 is pure novelty and ignores the question. 0.7 keeps relevance
# dominant while breaking up runs of near-identical facts, which is the failure
# being fixed rather than a general preference for variety.
MMR_LAMBDA = 0.7

# Measured floor per scope, keyed by what the measurement depends on:
# {group_id: ((fact_count, max_edge_id), floor)}. Process-local and never
# invalidated by time - see adaptive_cosine_floor for why those are the key.
_FLOOR_CACHE: dict[str, tuple[tuple, float]] = {}


def reset_floor_cache() -> None:
    """Forget every measured floor. For tests that rebuild a scope in place."""
    _FLOOR_CACHE.clear()

# The sample now yields unordered pairs, so a store of FLOOR_MIN_FACTS facts
# gives n(n-1)/2 of them, not n**2. Comparing against the squared count would
# have demanded roughly twice the facts the constant names.
_MIN_PAIRS = FLOOR_MIN_FACTS * (FLOOR_MIN_FACTS - 1) // 2

_logger = get_logger("query_memory")


class ValidationError(Exception):
    pass


def _validate(query: str | None, top_k: int, digest: bool) -> None:
    if not digest and (not query or not query.strip()):
        raise ValidationError("query must not be empty")
    if not isinstance(top_k, int) or top_k < 1:
        raise ValidationError(f"top_k must be a positive integer, got {top_k!r}")
    if top_k > MAX_TOP_K:
        raise ValidationError(f"top_k must be at most {MAX_TOP_K}, got {top_k}")


def _mmr_select(conn, group_id: str, ranked_ids: list[str], top_k: int) -> list[str]:
    """Pick top_k that are relevant AND not near-duplicates of each other.

    OFF BY DEFAULT, because it was measured and it made retrieval worse. Over
    219 cases on the author's store:

        no MMR    R@3 0.703   R@5 0.758   MRR 0.605   1,103 tokens
        MMR 0.7   R@3 0.653   R@5 0.717   MRR 0.594   1,103 tokens

    Five points of R@3 and eleven of MRR, for no token saving at all. The
    saving was the entire argument, and it does not exist: the response returns
    top_k facts either way, so MMR substitutes which facts rather than
    returning fewer. It would only pay if it let the caller ask for a smaller
    top_k, which nothing does.

    Kept rather than deleted because the reasoning is sound and the measurement
    is about THIS store: near-duplicates here are rare, so there is little
    redundancy to trade away and the trade only costs relevance. Splitting
    claim from detail would shorten every embedded text and change that
    profile, and this is worth re-measuring then. Turn it on with use_mmr=True
    and run the eval before believing it.

    The ranked list is scored purely on similarity to the query, so several
    phrasings of one fact all score well and all get selected. The user pays
    for every one of them in injected tokens and learns nothing from the second
    onwards - the same failure the capture queue has, arriving through
    retrieval instead.

    Standard MMR: repeatedly take the candidate maximising
        lambda * rel(d) - (1 - lambda) * max_{s in selected} sim(d, s)
    Relevance is the fused rank, already computed. Similarity between
    candidates comes from the embeddings that are already stored, so this costs
    one extra query and no model calls.

    Falls back to the plain ranked order if the embeddings cannot be read - a
    less diverse answer is much better than no answer.
    """
    if len(ranked_ids) <= 1 or top_k <= 1:
        return ranked_ids[:top_k]

    try:
        rows = conn.execute(
            """SELECT edge_id::text, embedding FROM public.fact_embedding
               WHERE group_id = %s AND edge_id::text = ANY(%s)""",
            (group_id, list(ranked_ids)),
        ).fetchall()
    except Exception:  # noqa: BLE001 - see docstring
        _logger.warning("mmr_skipped", extra={"reason": "embeddings unreadable"})
        return ranked_ids[:top_k]

    # pgvector hands back a Vector, not a list, and it is not iterable.
    vectors = {
        edge_id: (vec.to_list() if hasattr(vec, "to_list") else list(vec))
        for edge_id, vec in rows
    }
    if len(vectors) < len(ranked_ids):
        # Some candidate has no embedding; ranking it against the others would
        # be arbitrary. Not worth a partial reorder.
        return ranked_ids[:top_k]

    # Relevance from position: the list is already in fused-score order, and
    # only the ordering matters to MMR, not the scale.
    relevance = {eid: 1.0 - (i / len(ranked_ids)) for i, eid in enumerate(ranked_ids)}

    def cosine(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b, strict=True))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        return dot / (na * nb) if na and nb else 0.0

    selected: list[str] = [ranked_ids[0]]
    remaining = [e for e in ranked_ids[1:]]
    while remaining and len(selected) < top_k:
        best, best_score = None, None
        for candidate in remaining:
            redundancy = max(
                cosine(vectors[candidate], vectors[chosen]) for chosen in selected
            )
            score = MMR_LAMBDA * relevance[candidate] - (1 - MMR_LAMBDA) * redundancy
            if best_score is None or score > best_score:
                best, best_score = candidate, score
        selected.append(best)
        remaining.remove(best)
    return selected


def adaptive_cosine_floor(conn, group_id: str) -> float:
    """The similarity an unrelated fact actually scores in THIS store.

    A fixed 0.15 was measured to sit inside the noise rather than above it:
    unrelated-fact similarity in this store runs 0.084-0.163 with sd
    0.078-0.135, so a floor at 0.15 admits roughly half of everything. That is
    why an abstract question returns cron bugs - the floor was never filtering.

    Measuring beats picking a better constant. The right value depends on the
    embedder, the length of the facts and the subject matter, all of which
    differ per store and drift as one grows.

    **This is a heuristic, not a calibrated false-admission rate**, and the gap
    between those two was overstated until a reviewer took the derivation
    apart. Three assumptions stand between the 95th percentile of this sample
    and "5% of unrelated facts get through":

      - Random pairs are not unrelated pairs. Two facts from one project, a
        repeated constraint and its paraphrase all enter the sample and pull
        the percentile up. Nothing here excludes them; only a fact against
        itself is excluded.
      - Fact-to-fact similarity is not query-to-fact similarity. The floor is
        applied to a query scored against a fact, and calibrated on a fact
        scored against a fact. Short identifiers and natural-language questions
        do not have to behave alike.
      - An empirical quantile is not a future rate. It carries sampling error,
        and _vector_candidates admits score >= floor, so ties land inside.

    What it does deliver is a floor that moves with the store instead of a
    constant measured once on somebody else's data, which is what 0.15 was.

    **The sample is stable, not random.** It used to be `ORDER BY random()`,
    which redrew on every call - so the floor moved between 0.408 and 0.431 on
    one store, the admitted candidate set moved with it, and identical queries
    returned different answers. Three of ten evaluation questions changed their
    result list across three identical runs, which made every number measured
    through this function irreproducible and put floor-sampling noise inside
    the difference between two configurations being compared. Ordering by a
    hash of the id draws the same arbitrary subset every time: still arbitrary,
    no longer different on each call.

    Falls back to COSINE_FLOOR on a store too small to measure - under
    FLOOR_MIN_FACTS the sample is mostly noise about noise.

    Memoised per scope for the life of the process. The sample is fixed now, so
    recomputing returns the same number at a cost worth 41% of a query - the
    cross join dominated everything else the read path does.

    The cache key is the scope's fact count and highest edge id, not a timer.
    Those are what the answer depends on: graph ids only ever increase, and a
    fact is never edited in place - supersession writes a new edge - so a store
    whose count and maximum id both match is the store that was measured.
    `reset_floor_cache()` exists for tests, which are the one place that can
    rebuild a scope from nothing and land on the same pair by construction.
    """
    fingerprint = conn.execute(
        # graphid has no max(), and no direct cast to bigint either - it goes
        # through text. Same trap that once turned a targeted DELETE into a
        # full one.
        "SELECT count(*), coalesce(max(edge_id::text::bigint), 0) "
        "FROM public.fact_embedding WHERE group_id = %s",
        (group_id,),
    ).fetchone()
    cached = _FLOOR_CACHE.get(group_id)
    if cached is not None and cached[0] == fingerprint:
        return cached[1]

    row = conn.execute(
        """
        WITH sampled AS (
            SELECT edge_id, embedding FROM public.fact_embedding
            WHERE group_id = %s ORDER BY md5(edge_id::text) LIMIT %s
        ), pairs AS (
            SELECT -(a.embedding <#> b.embedding) AS sim
            FROM sampled a, sampled b
            -- Every unordered pair once, and never a fact against itself,
            -- which scores 1.0 and would drag the percentile up.
            --
            -- Comparing edge_id rather than the vectors is what makes that
            -- sentence true. `a.embedding <> b.embedding` counted each
            -- unordered pair TWICE - a cross join yields (a,b) and (b,a), so
            -- 50 facts gave 2450 rows where 1225 exist - and it dropped two
            -- distinct facts that happen to embed identically, which are
            -- precisely the near-duplicate pairs the percentile should see.
            WHERE a.edge_id < b.edge_id
        )
        SELECT count(*), percentile_cont(%s) WITHIN GROUP (ORDER BY sim)
        FROM pairs
        """,
        (group_id, FLOOR_SAMPLE, FLOOR_PERCENTILE / 100.0),
    ).fetchone()

    if not row or not row[0] or row[1] is None:
        _FLOOR_CACHE[group_id] = (fingerprint, COSINE_FLOOR)
        return COSINE_FLOOR
    n_pairs, percentile = row
    # n_pairs counts unordered pairs, so FLOOR_MIN_FACTS facts clear this bar.
    if n_pairs < _MIN_PAIRS:
        _FLOOR_CACHE[group_id] = (fingerprint, COSINE_FLOOR)
        return COSINE_FLOOR
    # A lower bound, and the reason given for it here used to be backwards: it
    # said a store of near-identical facts would measure a floor near zero.
    # Under cosine, near-identical facts score near ONE, so such a store
    # measures a very high percentile and this max() does nothing. The case it
    # actually guards is the opposite - a store whose facts are mutually
    # dissimilar, or a sample degenerate enough to put the quantile under a
    # value already known to be too permissive.
    floor = max(COSINE_FLOOR, float(percentile))
    _FLOOR_CACHE[group_id] = (fingerprint, floor)
    return floor


# Held as a constant so the test that checks this query is still indexable
# runs EXPLAIN on the same text the function executes, rather than on a copy
# that can drift away from it. See tests/integration/test_vector_index.py.
VECTOR_CANDIDATE_SQL = f"""
        SELECT fe.edge_id::text, -(fe.embedding <#> %s::vector) AS score
        FROM public.fact_embedding fe
        JOIN {GRAPH}."FACT" f ON f.id = fe.edge_id
        WHERE fe.group_id = %s
          AND (f.properties ->> '"t_invalid"'::agtype) IS NULL
        ORDER BY fe.embedding <#> %s::vector
        LIMIT %s
        """


def _vector_candidates(
    conn, group_id: str, embedding: list[float], limit: int, floor: float | None = None,
    scores: dict[str, float] | None = None,
) -> list[str]:
    """Ids, best first. Pass `scores` to also collect the cosine similarity
    this already computed and then threw away - the one number in this system
    a caller can threshold on, because unlike a fusion score it means the same
    thing from one query to the next."""
    # ORDER BY the distance and NOTHING else. pgvector's HNSW index can only
    # answer `ORDER BY <distance> LIMIT n`; a second sort key makes the whole
    # clause unindexable, and the planner falls back to reading every
    # embedding in the scope and sorting them.
    #
    # There WAS a second key here - `, fe.edge_id`, added so two facts at an
    # identical distance always resolved the same way. It cost the index.
    # Measured on a real 38,169 fact scope: 52.47ms doing the full scan
    # against 3.85ms using the index, and the scan is O(n), so ten times the
    # facts is half a second on every query. The determinism it bought is
    # still wanted and is now applied below, where it costs nothing.
    rows = conn.execute(VECTOR_CANDIDATE_SQL, (embedding, group_id, embedding, limit)).fetchall()
    if floor is None:
        floor = adaptive_cosine_floor(conn, group_id)
    # The tiebreak, moved out of SQL. Sorting by (-score, edge_id) reproduces
    # what the old ORDER BY produced - best first, ties resolved by id - on a
    # list of at most LIST_DEPTH rows, which is free. Python's sort is stable,
    # so rows that were already ordered by the index keep that order.
    ranked = sorted(
        ((edge_id, float(score)) for edge_id, score in rows if score >= floor),
        key=lambda row: (-row[1], row[0]),
    )
    if scores is not None:
        scores.update(dict(ranked))
    return [edge_id for edge_id, _ in ranked]



def _graph_candidates(
    conn, group_id: str, seed_edge_ids: list[str], limit: int
) -> list[str]:
    """Facts one hop from the facts a query already found.

    The graph has been stored since the first migration and the read path has
    never walked it: retrieval is a vector list and a lexical list fused, and
    AGE holds a structure nothing reads. This is the third list.

    A question like "how are X and Y related?" has no single fact as its
    answer, and the multihop shape of the eval scores exactly that at MRR 0.212
    against 0.672-0.970 for the one-hop shapes. Expanding from what was found
    is the cheapest thing that could close it: if a query retrieves a fact
    about X, the facts sharing X's node are the next place the answer can be.

    Ranked by the seed they came from, so a neighbour of the best-ranked fact
    outranks a neighbour of the fifth. Seeds themselves are excluded - they are
    already in the other lists, and re-ranking a fact against itself is what
    reciprocal rank fusion is for.

    Neighbours of ONE seed all carry that seed's rank, so their order among
    themselves is a second decision and it used to be made by accident: the
    rows arrived in whatever order Postgres produced, Python's stable sort kept
    it, and the result was truncated at LIST_DEPTH. That is unspecified output
    feeding a measurement - it can move between runs, plans or a vacuum. They
    are ordered by edge id within a seed now. Edge id is arbitrary as a
    relevance signal and that is the point: it is arbitrary and FIXED, so a
    rerun of the eval scores the same thing twice. A relevance-based tiebreak
    would be better and is a different change, one that has to be measured.
    """
    if not seed_edge_ids:
        return []
    # Read off the edge table rather than through Cypher. `MATCH (n)-[n2:FACT]-(m)`
    # leaves both the pattern and the relationship unbound, so AGE expands every
    # FACT edge in the database against every seed before applying the filter.
    # Measured 2026-09-21 on a 376 fact scope in a 1,751 edge database: 58,356ms
    # with graph_hops=1 against 44ms without, which is 1,300x for a hop that is
    # supposed to be the cheap way to answer a two fact question.
    #
    # That is the same defect already fixed twice in this codebase, in
    # neighbourhood._adjacent and neighbourhood._endpoints, and its cost here
    # was hidden because the feature it makes unusable is off by default.
    #
    # fact_group_start_idx and fact_group_end_idx, both (group_id, start_id) and
    # (group_id, end_id), have existed since migration 0013. This is the query
    # they were built for.
    rows = conn.execute(
        f"""WITH seeds AS (
                SELECT e.id AS seed, e.start_id, e.end_id
                FROM {GRAPH}."FACT" e
                WHERE e.id = ANY(SELECT unnest(%s::text[])::graphid)
            ),
            ends AS (
                SELECT seed, start_id AS node FROM seeds
                UNION ALL
                SELECT seed, end_id AS node FROM seeds
            )
            SELECT ends.seed::text, n2.id::text
            FROM ends
            JOIN {GRAPH}."FACT" n2
              ON (n2.start_id = ends.node OR n2.end_id = ends.node)
            WHERE (n2.properties ->> '"group_id"'::agtype) = %s
              AND (n2.properties ->> '"t_invalid"'::agtype) IS NULL""",
        ([str(i) for i in seed_edge_ids], group_id),
    ).fetchall()

    order = {edge_id: i for i, edge_id in enumerate(seed_edge_ids)}
    found: dict[str, int] = {}
    for seed, edge_id in rows:
        neighbour = str(edge_id)
        if neighbour in order:
            continue
        rank = order.get(str(seed), len(order))
        if neighbour not in found or rank < found[neighbour]:
            found[neighbour] = rank
    return [
        e for e, _ in sorted(found.items(), key=lambda kv: (kv[1], int(kv[0])))
    ][:limit]


def _any_term_tsquery(terms: list[str]) -> tuple[str, list[str]]:
    """An OR-of-terms tsquery, built by OR-ing per-term plainto_tsquery calls.

    websearch_to_tsquery ANDs every term, which is right when the query is a
    deliberate search and wrong when it is a whole sentence somebody typed at
    an agent: "is chat-module-api dev or prod" requires every one of chat,
    modul, api, dev and prod to appear in the same fact, and a fact that says
    exactly the right thing still misses because the hostname tokenises as one
    token and never yields a bare 'api'. Measured, not assumed - that prompt
    matched nothing against a fact written to answer it.

    Each term still goes through plainto_tsquery rather than being pasted into
    a to_tsquery string, so user text is never interpreted as tsquery syntax.
    That is the rule the design doc's security review set and it survives here:
    the OR is composed from sanitised pieces, not from raw input."""
    placeholders = " || ".join(["plainto_tsquery('english', %s)"] * len(terms))
    return f"({placeholders})", terms


# Words too common to carry signal, on top of Postgres's own stopwords. A
# prompt is full of them and each one drags in unrelated facts.
_NOISE_TERMS = frozenset(
    ["the", "a", "an", "is", "are", "was", "were", "be", "do", "does", "did", "can", "could", "should", "would", "will", "what", "why", "how", "when", "where", "who", "which", "this", "that", "these", "those", "and", "or", "not", "for", "from", "with", "about", "into", "you", "your", "we", "our", "it", "its", "me", "my", "please", "help", "need", "want"]
)
MAX_TERMS = 12


def prompt_terms(prompt: str) -> list[str]:
    """The words worth searching for in a typed prompt."""
    seen, terms = set(), []
    for raw in re.split(r"[^\w.\-/]+", prompt.lower()):
        word = raw.strip("-./")
        if len(word) < 3 or word in _NOISE_TERMS or word in seen:
            continue
        seen.add(word)
        terms.append(word)
    return terms[:MAX_TERMS]


# Whether the lexical channel ranks by BM25 or by ts_rank. Off by default
# until the eval says otherwise: this changes which facts a query returns, and
# a change to retrieval that ships on a plausible story rather than a measured
# one is how the graph hop came to be on for months at -0.142 MRR.
LEXICAL_BM25 = os.environ.get("ECHO_MEMORY_LEXICAL_BM25", "").lower() in ("1", "true", "yes")

# Whether an unspecified graph_hops asks the router or stays off. Off by
# default for the same reason BM25 is: this changes which facts a query
# returns, and the hop it turns on is the one measured at -0.142 MRR when it
# was on for everybody.
ROUTE_EXPANSION = os.environ.get("ECHO_MEMORY_ROUTE_EXPANSION", "").lower() in (
    "1", "true", "yes",
)


def needs_expansion(conn, group_id: str, query: str) -> bool:
    """Whether this question needs a second fact to answer it.

    The first version of this router asked "does the query name two
    entities", and measuring it is what showed the question was wrong.
    entity_pair cases name two entities AND are answered by the single edge
    between them, so routing them to expansion did precisely the thing the
    ablation says costs -0.142 MRR. Naming two things is not the signal.

    Being UNCONNECTED is. If the two named entities already share an edge,
    one fact answers the question and its neighbours are noise. If the store
    holds both and no edge joins them, then whatever relates them is a path,
    and a path is the one thing the content channels cannot retrieve: they
    rank facts by resemblance to the query, and the middle of a chain
    resembles the question least.

    That is also what the eval measured. Expansion helped exactly one shape,
    multihop, raising recall@10 from 0.635 to 0.769, and multihop cases are
    built from two facts that do not share an edge.

    Exact lexical containment for the name match, not embedding similarity. A
    near match would make the router its own retrieval problem with its own
    threshold to calibrate, and this system already has one threshold whose
    confidence interval includes chance.
    """
    words = sorted(set(prompt_terms(query)))
    if len(words) < 2:
        return False
    rows = conn.execute(
        f"""SELECT n.id
              FROM {GRAPH}."Node" n
             WHERE (n.properties ->> '"group_id"'::agtype) = %s
               AND EXISTS (
                   SELECT 1 FROM unnest(%s::text[]) AS w
                    WHERE position(w IN lower(n.properties ->> '"name"'::agtype)) > 0
               )
             LIMIT 8""",
        (group_id, words),
    ).fetchall()
    if len(rows) < 2:
        return False
    node_ids = [str(r[0]) for r in rows]
    (connected,) = conn.execute(
        f"""SELECT count(*)
              FROM {GRAPH}."FACT" e
             WHERE (e.properties ->> '"group_id"'::agtype) = %s
               AND (e.properties ->> '"t_invalid"'::agtype) IS NULL
               AND e.start_id::text = ANY(%s)
               AND e.end_id::text = ANY(%s)""",
        (group_id, node_ids, node_ids),
    ).fetchone()
    return int(connected) == 0


def _lexical_any_candidates(
    conn, group_id: str, query: str, limit: int, use_bm25: bool | None = None
) -> list[str]:
    """Lexical retrieval that matches ANY salient term, ranked. Used by the
    prompt-time recall path; the main query path keeps AND semantics, which is
    correct for a deliberate query and is what v1a's retrieval was tested on."""
    terms = prompt_terms(query)
    if not terms:
        return []

    # BM25 when the scope has statistics worth ranking with, ts_rank when it
    # does not. Falling back rather than refusing, because a scope too small
    # or too freshly grown for corpus statistics still has to answer, and
    # ts_rank is what it has always answered with.
    if use_bm25 is None:
        use_bm25 = LEXICAL_BM25
    if use_bm25 and bm25.usable(conn, group_id):
        return [edge_id for edge_id, _ in bm25.candidates(conn, group_id, terms, limit)]
    tsquery, params = _any_term_tsquery(terms)
    rows = conn.execute(
        f"""
        SELECT f.id::text,
               ts_rank(to_tsvector('english', f.properties ->> '"fact"'::agtype),
                        {tsquery}) AS score
        FROM {GRAPH}."FACT" f
        WHERE (f.properties ->> '"group_id"'::agtype) = %s
          AND (f.properties ->> '"t_invalid"'::agtype) IS NULL
          AND to_tsvector('english', f.properties ->> '"fact"'::agtype) @@ {tsquery}
        -- Tie broken by id, arbitrary but fixed. ts_rank has no IDF and no
        -- length normalisation, so scores collapse onto a few values and
        -- exact ties at the cut are the common case, not the exception: on
        -- 324 real prompts the rank-3 and rank-4 scores were identical in 59%
        -- of them, and the hook injects three. Which fact a user sees was
        -- decided by Postgres scan order.
        --
        -- Third instance of the same defect class in this codebase - the
        -- traversal channel and the audit log had it too - and the same fix:
        -- not a better order, just a stated one, so a rerun scores the thing
        -- it scored last time.
        ORDER BY score DESC, f.id
        LIMIT %s
        """,
        (*params, group_id, *params, limit),
    ).fetchall()
    return [edge_id for edge_id, score in rows if score > TS_RANK_FLOOR]


def _digest_candidates(conn, group_id: str, limit: int) -> list[str]:
    """No query text to rank against: a digest is "catch me up," not "answer
    this," so it's the most recently valid active facts, chronological, not
    relevance-ranked. See the CEO plan's scope decision #2 (session-start
    context digest, opt-in, no auto-injection).

    t_valid is second-granularity, so two facts written within the same
    second tie on it; id(e) DESC breaks the tie deterministically by
    creation order (AGE assigns ids monotonically per label)."""
    rows = conn.execute(
        f"""SELECT * FROM cypher('{GRAPH}', $$
            MATCH ()-[e:FACT {{group_id: $gid}}]->()
            WHERE e.t_invalid IS NULL
            RETURN id(e)
            ORDER BY e.t_valid DESC, id(e) DESC
            LIMIT $limit
        $$, %s) AS (edge_id agtype)""",
        (json.dumps({"gid": group_id, "limit": limit}),),
    ).fetchall()
    return [str(edge_id) for (edge_id,) in rows]


def _agtype_str(v):
    return str(v).strip('"') if v is not None else None


def _fetch_facts(conn, edge_ids: list[str]) -> dict[str, dict]:
    """Batch-fetch fact/confidence/causal_hint/provenance/agent_id/project for a
    set of edge ids via one Cypher UNWIND query, not N+1 lookups.

    `agent_id` is returned because `record_recall_save` asks the caller for
    `written_by`, and until 2026-08-28 the only documented source for that value
    was "visible on every query_memory result as agent_id" - a field this
    function did not return. An agent following the instruction found nothing
    and had to guess or skip, which is one of three reasons the v1a cross-tool
    criterion had never once been satisfied. It has been on the edge and indexed
    (`fact_group_agent_idx`) since migration 0003; only the read path was
    missing."""
    if not edge_ids:
        return {}
    rows = conn.execute(
        f"""SELECT * FROM cypher('{GRAPH}', $$
            UNWIND $ids AS eid
            MATCH ()-[e:FACT]->() WHERE id(e) = eid
            RETURN id(e), e.fact, e.confidence, e.causal_hint, e.provenance,
                   e.agent_id, e.project
        $$, %s) AS (edge_id agtype, fact agtype, confidence agtype,
                     causal_hint agtype, provenance agtype, agent_id agtype,
                     project agtype)""",
        (json.dumps({"ids": [int(i) for i in edge_ids]}),),
    ).fetchall()
    return {
        str(edge_id): {
            "fact_id": str(edge_id),
            "fact": _agtype_str(fact),
            "confidence": _agtype_str(confidence),
            "causal_hint": _agtype_str(causal_hint),
            "provenance": _provenance(provenance, agent_id, project),
        }
        for edge_id, fact, confidence, causal_hint, provenance, agent_id, project in rows
    }


def _provenance(raw, agent_id, project) -> dict | None:
    """Attribution belongs inside `provenance` rather than beside it: session and
    episode already live there, and a caller asking "where did this come from?"
    should find every part of the answer in one place."""
    out = json.loads(str(raw)) if raw is not None else {}
    if not isinstance(out, dict):
        out = {"episode": out}
    out["agent_id"] = _agtype_str(agent_id)
    out["project"] = _agtype_str(project)
    return out


def _missing_similarity(
    conn, group_id: str, embedding: list[float], edge_ids: list[str]
) -> dict[str, float]:
    """Cosine for facts the vector channel never scored.

    The lexical channel computes no similarity, so a fact only it found
    arrived with score null. That is an implementation detail - which channel
    happened to retrieve it - leaking into a field callers read as relevance.

    Fixed by computing the number instead of omitting it. At most top_k rows,
    by primary key, with the query embedding already in hand.
    """
    if not edge_ids:
        return {}
    rows = conn.execute(
        """SELECT fe.edge_id::text, -(fe.embedding <#> %s::vector)
             FROM public.fact_embedding fe
            WHERE fe.group_id = %s
              AND fe.edge_id = ANY(SELECT unnest(%s::text[])::graphid)""",
        (embedding, group_id, list(edge_ids)),
    ).fetchall()
    return {str(edge_id): float(score) for edge_id, score in rows}


def _annotate(
    facts: list[dict], similarity: dict[str, float],
    vector_ids: set[str], lexical_ids: set[str],
) -> None:
    """Say why each fact is in the answer, in place.

    rank        position in this answer, 1 first. THIS is the ordering to
                trust and the one to truncate by. It is the fused result of
                every channel, which is the system's whole opinion; score is
                one input to it, and re-sorting by a single input discards
                the fusion.
    score       cosine similarity to the query. Present on every fact of
                every answer the MCP surface can ask for, and it means the
                same thing for every fact whatever channel found it.
                Diagnostic, NOT a correctness gate: a fact found by exact
                keyword match can be the right answer at a low semantic
                score.

                Absent in exactly one place, and not one a tool caller can
                reach: the UserPromptSubmit hook runs lexical_only in a fresh
                process precisely to avoid a 6.2 second model load, so there
                is no embedding to compare against and no honest number to
                give. Omitted rather than faked.
    matched     which channels found it, as information rather than quality.

    The null that used to appear here caused real harm and the shape of it is
    worth keeping. score was omitted for lexical-only facts, a customer sorted
    by score with nulls last, and every fact full text search had found went
    to the back of the list - where a character budget truncated it and the
    model filled the gap by inventing something. The fact it dropped was
    correct and was in the store.

    Their own calibration says why that is backwards. Over ten questions with
    every fact judged against every question: lexical-only scored R@3 0.640
    against vector-only's 0.460, and beat the fused ranking outright on two of
    the ten. The channel that was being demoted has the better recall.

    Absent on a digest too, which ranks nothing against nothing.
    """
    for position, fact in enumerate(facts, start=1):
        edge_id = fact["fact_id"]
        matched = [
            name for name, ids in (("vector", vector_ids), ("lexical", lexical_ids))
            if edge_id in ids
        ]
        if not matched:
            continue
        fact["rank"] = position
        if edge_id in similarity:
            fact["score"] = round(similarity[edge_id], 4)
        fact["matched"] = matched


def query_memory(
    conn, group_id: str, query: str | None, top_k: int, embedder,
    digest: bool = False, lexical_only: bool = False,
    *, use_mmr: bool = False, floor: float | None = None, vector_only: bool = False,
    rrf_k: int | None = None, graph_hops: int | None = None,
    lexical_bm25: bool | None = None, route_expansion: bool | None = None,
) -> dict:
    """top_k has no default here: DEFAULT_TOP_K=10 is applied at the MCP tool
    schema layer (PR5), which is the natural place to declare it, rather
    than baking a default into every internal caller of this function.

    digest=True ignores query (may be None) and returns the most recently
    valid active facts instead of ranking against a query string: an opt-in
    "catch me up" convenience for session start, explicitly invoked, never
    auto-triggered (see the CEO plan's scope decision #2).

    lexical_bm25 chooses how the lexical channel ranks: BM25 over per scope
    corpus statistics, or ts_rank. None means the ECHO_MEMORY_LEXICAL_BM25
    default. It is a keyword here for the same reason the others are - so the
    harness can score it against what it replaces, rather than it shipping on
    a plausible story.

    use_mmr, floor, vector_only and rrf_k exist so the eval harness can ablate
    one change at a time and show which actually helped. Keyword only, and
    defaulting to the shipping behaviour, so no caller gets a different answer
    by accident. Nothing in the MCP surface exposes them - a knob the calling
    agent can turn is a knob that ends up load-bearing.

    What the eval said about each of them, over 219 cases on the author's
    store, is recorded where each is implemented. In short: the ANY-term
    lexical channel is the one clear win (MRR 0.571 vector-only to 0.605
    hybrid), the adaptive floor is quality-neutral and cuts tokens ~5%, MMR
    made things worse and is off, and rrf_k really is low-leverage - 0.606 to
    0.613 across k from 5 to 100.

    lexical_only=True drops the vector signal and ranks on Postgres full-text
    search alone. It exists for one caller: the UserPromptSubmit hook, which
    runs in a fresh process on every prompt and therefore cannot afford to load
    the embedding model - measured at 6.2 seconds of cold start (see
    cli/benchmark.py). FTS needs no model, so that path costs milliseconds.

    The tradeoff is real and worth stating: lexical matching finds facts that
    share words with the prompt and misses ones that only share meaning, which
    is exactly what the vector signal is for. Recall here is deliberately worse
    than a full query_memory call. It is the difference between some relevant
    memory arriving automatically and none arriving at all, not between good
    retrieval and bad."""
    start = time.perf_counter()
    try:
        _validate(query, top_k, digest)
    except ValidationError as e:
        log_query_memory(
            _logger, group_id, 0, 0, 0, (time.perf_counter() - start) * 1000, error=str(e)
        )
        return {"error": str(e)}

    similarity: dict[str, float] = {}
    query_embedding: list[float] | None = None
    if digest:
        ranked_ids = _digest_candidates(conn, group_id, top_k)
        vector_ids, lexical_ids = [], []
    elif lexical_only:
        vector_ids = []
        lexical_ids = _lexical_any_candidates(
            conn, group_id, query, LIST_DEPTH, use_bm25=lexical_bm25
        )
        ranked_ids = lexical_ids[:top_k]
    else:
        embedding = query_embedding = embedder.embed(query)
        vector_ids = _vector_candidates(
            conn, group_id, embedding, LIST_DEPTH, floor=floor, scores=similarity
        )
        # ANY-term, not websearch_to_tsquery's implicit AND.
        #
        # The AND form requires every non-stopword term of the query to appear
        # in the fact. "deploy branch policy" against a fact about the deploy
        # branch matches nothing, because "policy" is absent. Measured on six
        # realistic questions against twenty real facts, exactly one returned
        # anything - so RRF was fusing a populated vector list with an empty
        # lexical one and reproducing the vector ordering exactly. That is also
        # why k=60 and LIST_DEPTH=50 read as low-leverage: they have never had
        # two lists to fuse.
        #
        # This function already existed for the hook path, thirty lines above,
        # where the same behaviour had been found and worked around. The tool
        # path kept the version that does not work, and the pair looked
        # deliberate.
        lexical_ids = [] if vector_only else _lexical_any_candidates(
            conn, group_id, query, LIST_DEPTH, use_bm25=lexical_bm25
        )
        k = rrf_k if rrf_k is not None else RRF_K
        content = reciprocal_rank_fusion([vector_ids, lexical_ids], k=k)
        lists = [vector_ids, lexical_ids]
        # Routed, not configured. graph_hops=None asks the router; an explicit
        # 0 or 1 overrides it, which is what the harness needs to score the
        # router against always-on and always-off.
        hops = graph_hops
        if hops is None:
            routing = ROUTE_EXPANSION if route_expansion is None else route_expansion
            hops = 1 if routing and needs_expansion(conn, group_id, query) else 0
        if hops:
            # Seeded from the two content channels fused, so the expansion
            # follows what the query actually matched rather than whatever the
            # vector list alone happened to put first.
            #
            # That also means this list is NOT independent of the other two,
            # and reciprocal rank fusion is happiest when its inputs are.
            # Everything here is derived from the content channels' own top
            # results, so a fact both retrieves and neighbours gets counted
            # twice.
            #
            # Measured properly on 2026-09-22, once the hop stopped costing 58
            # seconds and a full ablation became something anybody would run.
            # It is not a wash and it is not close:
            #
            #   shape           shipping   + hop   dMRR      95% CI
            #   entity_pair        0.641   0.499   -0.142   [-0.182, -0.104]
            #   entity_single      0.690   0.591   -0.099   [-0.135, -0.062]
            #   prose              0.974   0.849   -0.125   [-0.158, -0.094]
            #   multihop           0.196   0.200   +0.003   [-0.022, +0.028]
            #
            # So it stays off by default, and now for a reason rather than a
            # suspicion. On questions one fact answers, neighbours of that fact
            # dilute the ranking: three intervals clear of zero, in the same
            # direction, is not noise.
            #
            # What it does buy is on the shape it was built for, and it is not
            # in MRR: multihop recall@10 rises 0.635 to 0.769. It finds the
            # second fact and ranks it badly. That is a real result and the
            # reason this code is kept rather than deleted, but it is an
            # argument for turning the hop on per question, once something can
            # tell a multi hop question from a single hop one, rather than for
            # turning it on for everybody.
            seeds = sorted(content, key=content.get, reverse=True)[:GRAPH_SEEDS]
            lists.append(_graph_candidates(conn, group_id, seeds, LIST_DEPTH))
        fused = reciprocal_rank_fusion(lists, k=k) if hops else content
        by_score = sorted(fused, key=fused.get, reverse=True)
        # Diversify before truncating, not after: the point is to choose which
        # top_k, and slicing first throws away the candidates MMR would swap in.
        ranked_ids = (
            _mmr_select(conn, group_id, by_score[:LIST_DEPTH], top_k)
            if use_mmr else by_score[:top_k]
        )

    facts_by_id = _fetch_facts(conn, ranked_ids)
    facts = [facts_by_id[edge_id] for edge_id in ranked_ids if edge_id in facts_by_id]
    # Score whatever the vector channel did not, so every returned fact has
    # one. Only on the query path: a digest ranks nothing and has no query to
    # be similar to.
    if query_embedding is not None:
        unscored = [f["fact_id"] for f in facts if f["fact_id"] not in similarity]
        similarity.update(
            _missing_similarity(conn, group_id, query_embedding, unscored)
        )
    _annotate(facts, similarity, set(vector_ids), set(lexical_ids))

    log_query_memory(
        _logger,
        group_id,
        len(vector_ids),
        len(lexical_ids),
        len(facts),
        (time.perf_counter() - start) * 1000,
    )
    return {"facts": facts}
