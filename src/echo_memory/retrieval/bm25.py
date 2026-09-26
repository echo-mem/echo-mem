"""BM25 for the lexical channel, computed in SQL.

ts_rank counts term occurrences and nothing else. It has no inverse document
frequency, so a word in every fact counts as much as one in three; no term
frequency saturation, so repetition scales linearly; and no length
normalisation, so a long fact is punished for being long.

What that cost, measured on 324 real prompts: the rank-3 and rank-4 scores
were identical in 59% of them. The prompt hook injects three facts, so which
fact a user saw was decided by Postgres scan order. A tiebreak on id was
added at the time; it made the arbitrary ordering reproducible rather than
right.

It matters more than it looks, because reciprocal rank fusion reads RANK
POSITION and never the underlying score. A channel whose ordering is a coin
flip hands the fusion a coin flip, and every number downstream inherits it.
And this is the channel with the better recall: on a customer's own
calibration, ten questions with every fact judged against every question,
lexical-only scored R@3 0.640 against the vector channel's 0.460.

Why not an extension. pg_search (ParadeDB) is AGPL-3.0, which is a question
nobody wants to answer on a sales call for a BSL product that ships its own
Postgres image. pg_textsearch is memory resident and young. And a self-hosted
user may bring their own database, where neither exists. BM25 is four lines of
arithmetic over statistics Postgres can already produce, so it is computed
here rather than installed.
"""

import os

from echo_memory.infra.db import GRAPH_NAME as GRAPH
from echo_memory.infra.logging import get_logger

_logger = get_logger("bm25")

# Saturation and length normalisation, at the values the literature settled
# on. Not tuned here, and deliberately: this codebase has been burned once by
# fitting a constant to a handful of observations, and k1/b are the two
# parameters most often tuned on noise. They are exposed so a store with
# unusual facts can move them, and left alone otherwise.
K1 = float(os.environ.get("ECHO_MEMORY_BM25_K1", "1.2"))
B = float(os.environ.get("ECHO_MEMORY_BM25_B", "0.75"))

# How far the corpus may drift before the statistics are worth recomputing.
# Document frequency is a ratio over the whole scope, so one more fact moves
# it by nothing; a fifth more facts can move which terms are rare.
STALE_RATIO = float(os.environ.get("ECHO_MEMORY_BM25_STALE_RATIO", "0.2"))

# Below this, inverse document frequency is describing noise rather than the
# corpus, and ts_rank's crudeness costs little because there is nothing to
# discriminate between.
MIN_DOCS = int(os.environ.get("ECHO_MEMORY_BM25_MIN_DOCS", "50"))

# The fewest writes between rebuilds, whatever the proportional tolerance
# says. Its own constant rather than a second use of MIN_DOCS, which was the
# first thing tried and made the floor dominate: at MIN_DOCS 50 a scope of 61
# facts needed 50 writes to drift rather than the 12 its tolerance implies,
# so every scope under a few hundred facts was governed by the floor and not
# by the ratio at all.
#
# Ten writes of a fifty fact scope is a five millisecond rebuild. The floor
# exists to stop a tiny scope rebuilding on every write, not to hold back a
# real one.
MIN_REBUILD_WRITES = int(os.environ.get("ECHO_MEMORY_BM25_MIN_REBUILD_WRITES", "10"))

# How far a scope may grow past its statistics before they are rebuilt, as a
# fraction of the size they were built at.
#
# Checked on every write, which is affordable because the check is arithmetic
# on two integers the database already maintains rather than a count of the
# scope. See _stale.
#
# This replaced a fixed rebuild interval, and the reason is worth keeping. A
# constant interval cannot bound a proportional drift: a scope rebuilt at 500
# facts goes stale at 600 and was not rebuilt until 1000, so the ranker
# switched itself off for four fifths of the writes while its setting said it
# was on. Shrinking the interval narrows that window without closing it.
#
# None of its own tests saw this. Running the thing end to end against an
# empty database did, at 62 facts: refreshed at 49, drift 0.265 against a
# tolerance of 0.2, BM25 silently off.

_FACT_TEXT = """(f.properties ->> '"fact"'::agtype)"""
_ACTIVE = """(f.properties ->> '"t_invalid"'::agtype) IS NULL"""
_SCOPED = """(f.properties ->> '"group_id"'::agtype) = %s"""


def refresh(conn, group_id: str) -> dict:
    """Recompute the corpus statistics for one scope.

    One scope at a time, because a full recompute across every tenant grows
    with the service rather than with the store that changed.

    The lexeme table is replaced rather than merged: a term that no longer
    appears anywhere has to lose its row, and working that out incrementally
    costs more than recomputing a scope that is, by construction, one
    customer's memory.
    """
    conn.execute("DELETE FROM public.lexical_term WHERE group_id = %s", (group_id,))
    conn.execute(
        f"""
        INSERT INTO public.lexical_term (group_id, lexeme, doc_count)
        SELECT %s, t.lexeme, count(*)
          FROM {GRAPH}."FACT" f,
               LATERAL unnest(to_tsvector('english', {_FACT_TEXT})) AS t
         WHERE {_SCOPED} AND {_ACTIVE}
         GROUP BY t.lexeme
        """,
        (group_id, group_id),
    )
    row = conn.execute(
        f"""
        SELECT count(*), coalesce(avg(len), 0)
          FROM (
            SELECT (SELECT coalesce(sum(cardinality(t.positions)), 0)
                      FROM unnest(to_tsvector('english', {_FACT_TEXT})) AS t) AS len
              FROM {GRAPH}."FACT" f
             WHERE {_SCOPED} AND {_ACTIVE}
          ) lengths
        """,
        (group_id,),
    ).fetchone()
    doc_count, avg_len = int(row[0]), float(row[1])
    # The write counter is stamped from group_state rather than passed in, so
    # a rebuild triggered from anywhere - the write path, reindex, a test -
    # records the same thing.
    conn.execute(
        """
        INSERT INTO public.lexical_scope
               (group_id, doc_count, avg_doc_length, refreshed_at, refreshed_docs,
                refreshed_write_count)
        VALUES (%s, %s, %s, now(), %s,
                coalesce((SELECT write_episode_count FROM public.group_state
                           WHERE group_id = %s), 0))
        ON CONFLICT (group_id) DO UPDATE
           SET doc_count = EXCLUDED.doc_count,
               avg_doc_length = EXCLUDED.avg_doc_length,
               refreshed_at = now(),
               refreshed_docs = EXCLUDED.refreshed_docs,
               refreshed_write_count = EXCLUDED.refreshed_write_count
        """,
        (group_id, doc_count, avg_len, doc_count, group_id),
    )
    _logger.info(
        "bm25_refresh",
        extra={"group_id": group_id, "docs": doc_count, "avg_len": round(avg_len, 2)},
    )
    return {"docs": doc_count, "avg_doc_length": avg_len}


def usable(conn, group_id: str) -> bool:
    """Whether this scope has statistics worth ranking with.

    False is a normal answer and the caller falls back to ts_rank, which is
    what every version of this code did until now. A scope that has never
    been refreshed, is too small to have a corpus, or has grown past
    STALE_RATIO since its last rebuild gets the old behaviour rather than a
    wrong one: stale inverse document frequency does not fail loudly, it
    quietly ranks against a corpus that no longer exists.
    """
    row = _staleness_row(conn, group_id).fetchone()
    if row is None or row[0] is None or int(row[0]) < MIN_DOCS:
        return False
    return not _grown_past_tolerance(row)


def candidates(conn, group_id: str, terms: list[str], limit: int) -> list[tuple[str, float]]:
    """(edge_id, bm25 score), best first, for facts matching any query term.

    Scored only over rows that matched, which is what makes the per-document
    half free: a fact's term frequencies and its length come out of its own
    tsvector, and there is no reason to compute either for a fact the query
    never touched.

    The IDF form is Lucene's, ln(1 + (N - df + 0.5)/(df + 0.5)), rather than
    the textbook ln((N - df + 0.5)/(df + 0.5)). The textbook one goes negative
    for a term in more than half the documents, which would mean a fact
    scoring WORSE for containing a query term than for omitting it. On a
    memory store, where one scope is one subject and common words are common,
    that case is the rule rather than the exception.

    A term absent from the statistics table gets df 0, which the formula
    turns into the largest IDF it can produce. That is the right answer: a
    term the corpus has never seen is maximally discriminating, and the
    common reason for it is a scope that grew since the last refresh.
    """
    if not terms:
        return []
    rows = conn.execute(
        f"""
        WITH scope AS (
            SELECT doc_count, greatest(avg_doc_length, 1) AS avg_len
              FROM public.lexical_scope WHERE group_id = %s
        ),
        query AS (
            -- Each term lemmatised the same way the documents were, so
            -- "returned" finds the lexeme stored for "returning". Taking
            -- only the first lexeme keeps one row per term the caller
            -- supplied, which is what the statistics are keyed by.
            SELECT DISTINCT (SELECT t.lexeme
                               FROM unnest(to_tsvector('english', term)) AS t
                              LIMIT 1) AS lexeme
              FROM unnest(%s::text[]) AS term
        ),
        weighted AS (
            SELECT q.lexeme,
                   ln(1 + ((SELECT doc_count FROM scope) - coalesce(lt.doc_count, 0) + 0.5)
                          / (coalesce(lt.doc_count, 0) + 0.5)) AS idf
              FROM query q
              LEFT JOIN public.lexical_term lt
                     ON lt.group_id = %s AND lt.lexeme = q.lexeme
             WHERE q.lexeme IS NOT NULL
        ),
        matched AS (
            SELECT f.id,
                   t.lexeme,
                   cardinality(t.positions)::double precision AS tf,
                   (SELECT coalesce(sum(cardinality(t2.positions)), 0)
                      FROM unnest(to_tsvector('english', {_FACT_TEXT})) AS t2)::double precision
                     AS doc_len
              FROM {GRAPH}."FACT" f,
                   LATERAL unnest(to_tsvector('english', {_FACT_TEXT})) AS t
             WHERE {_SCOPED} AND {_ACTIVE}
               AND t.lexeme IN (SELECT lexeme FROM weighted)
        )
        SELECT m.id::text,
               sum(w.idf * (m.tf * ({K1} + 1))
                   / (m.tf + {K1} * (1 - {B} + {B} * m.doc_len
                                     / (SELECT avg_len FROM scope)))) AS score
          FROM matched m
          JOIN weighted w ON w.lexeme = m.lexeme
         GROUP BY m.id
         -- Ordered by a score that now discriminates. The id tiebreak stays
         -- for the genuine ties that remain, which are rare here rather than
         -- the majority case they were under ts_rank.
         ORDER BY score DESC, m.id
         LIMIT %s
        """,
        (group_id, list(terms), group_id, group_id, limit),
    ).fetchall()
    return [(str(edge_id), float(score)) for edge_id, score in rows]


def _grown_past_tolerance(row) -> bool:
    """Whether a scope has outgrown its statistics, from two integers.

    ONE predicate, used by both usable() and refresh_if_due(), and that is
    the point rather than tidiness. When they were separate conditions they
    disagreed, and the disagreement was a window in which the statistics were
    declared untrustworthy but not yet rebuilt - so the ranker fell back to
    ts_rank while its setting said BM25, silently, for as long as the two
    thresholds differed. That bug has now been introduced twice in one day,
    in two different shapes, which is what a shared predicate prevents.

    group_state.write_episode_count is already incremented inside every
    write's transaction, so growth is arithmetic rather than a scan. Writes
    over-estimate facts, since a call can carry several or be refused
    outright, and over-estimating rebuilds slightly early - the harmless
    direction.

    MIN_REBUILD_WRITES stops a small scope rebuilding on every write, where a
    proportional tolerance is a couple of facts wide.
    """
    if row is None:
        return True
    refreshed_docs, refreshed_write_count, live_write_count = row
    if refreshed_docs is None or refreshed_write_count is None:
        return True
    growth = max(0, int(live_write_count) - int(refreshed_write_count))
    return growth > max(STALE_RATIO * int(refreshed_docs), MIN_REBUILD_WRITES)


def _staleness_row(conn, group_id: str):
    return conn.execute(
        """SELECT ls.refreshed_docs, ls.refreshed_write_count,
                  coalesce(gs.write_episode_count, 0)
             FROM public.lexical_scope ls
             LEFT JOIN public.group_state gs ON gs.group_id = ls.group_id
            WHERE ls.group_id = %s""",
        (group_id,),
    )


def refresh_if_due(conn, group_id: str, write_count: int) -> bool:
    """Rebuild a scope's statistics when they have stopped describing it.

    Called from inside write_episode's transaction, which already holds this
    scope's advisory lock, so two writers cannot rebuild at once.

    Checked on every write, because the check is two integers rather than a
    scan, and rebuilt only when it says stale. So a quiet scope pays nothing
    and a growing one is never left ranking against a corpus it has outgrown
    - which is the window a fixed interval could not close.

    A scope below MIN_DOCS is left alone by _stale, which treats statistics
    built over too small a corpus as never trustworthy. There is nothing
    there to be rare among.
    """
    if write_count < MIN_REBUILD_WRITES:
        return False
    if not _grown_past_tolerance(_staleness_row(conn, group_id).fetchone()):
        return False
    refresh(conn, group_id)
    return True
