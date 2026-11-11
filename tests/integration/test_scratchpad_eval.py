"""Measuring the scratchpad, on a corpus whose answers are known.

`retrieval/scratchpad.py` shipped on an argument from BEAM's paper and no
number of its own. These seed a store shaped like one somebody would have, with
hub entities of degree 12 down to leaf pairs of degree 1, and five facts
superseded so the live view is not simply the write log, and then score the
derived block against `query_memory` at the same token spend.

The real embedding model is used on purpose. A registered-vector embedder makes
the vector channel answer to whatever the test author decided, and the whole
question here is whether a query-independent block can beat a real ranker.

The numbers this produces are written into docs/BENCHMARKS.md. The assertions
below are about the harness rather than about the result: that both sides scored
the same cases, that the spend really was matched, and that a superseded fact
never reaches the block.
"""

from __future__ import annotations

import pytest

from echo_memory.eval import scratchpad_eval
from echo_memory.eval.retrieval import SHAPE_ENTITY_PAIR, SHAPE_PROSE, build_cases
from echo_memory.infra.db import GRAPH_NAME as GRAPH
from echo_memory.infra.db import connect
from echo_memory.ingestion.embeddings import LocalEmbedder
from echo_memory.ingestion.write_episode import write_episode
from echo_memory.retrieval.scratchpad import scratchpad

GROUP = "user:measure:shared"

# Sixty facts over five hub entities and sixteen leaf pairs. Hub degree runs 12,
# 10, 9, 8, 6 and the rest are 1 or 2, because the scratchpad ranks by summed
# endpoint degree and a corpus of uniform degree would make its ordering
# arbitrary and the measurement meaningless.
CORPUS = [
    ("echo-mem", "Apache 2.0", "the engine was relicensed to Apache 2.0 in October"),
    ("echo-mem", "PyPI", "the cloud installs the engine from a published release"),
    ("echo-mem", "DCO", "every commit needs a sign off and the gate checks all of them"),
    ("echo-mem", "Caddy", "TLS terminates at the reverse proxy on the host compose"),
    ("echo-mem", "AGE", "facts are edges in a temporal property graph"),
    ("echo-mem", "units", "capacity is cumulative for writes and monthly for reads"),
    ("echo-mem", "Razorpay", "subscriptions are the only paid path"),
    ("echo-mem", "Vercel", "the dashboard deploys from the main branch"),
    ("echo-mem", "MiniLM", "the default embedding model has an effective rank of 43 of 384"),
    ("echo-mem", "scratchpad", "current belief is derived from the graph rather than generated"),
    ("echo-mem", "erasure", "a removal deletes the edge and its orphan endpoints together"),
    ("echo-mem", "audit log", "every read and write is recorded with the writer version"),

    ("dugout-be", "deploy branch", "the deploy branch is master and never main"),
    ("dugout-be", "Redis", "the first backfill landed in a cache and not in the graph"),
    ("dugout-be", "trial", "the account was cut off for seven days"),
    ("dugout-be", "restatement", "543 writes produced 112 facts by saying the same thing again"),
    ("dugout-be", "two memories", "one account holds a development and a production scope"),
    ("dugout-be", "match reports", "a report names the ground, the toss and the innings total"),
    ("dugout-be", "entity scoping", "facts were filtered on text at four call sites"),
    ("dugout-be", "staging", "staging shares a database with nothing else"),
    ("dugout-be", "api key", "the key did not change when the account moved"),
    ("dugout-be", "causal language", "none of the stored facts use because or therefore"),

    ("Postgres", "port 5433", "the live store listens on 5433 in local development"),
    ("Postgres", "port 5434", "the disposable test database listens on 5434"),
    ("Postgres", "pgvector", "similarity search runs over an HNSW index"),
    ("Postgres", "graphid", "an edge id is not a bigint and casts through text"),
    ("Postgres", "ssl", "the application connects without transport encryption"),
    ("Postgres", "agtype", "a map cannot be cast to text and must be projected"),
    ("Postgres", "alembic", "the migration chain is applied in full on every start"),
    ("Postgres", "vacuum", "a deleted tuple survives on the page until it is reclaimed"),
    ("Postgres", "tsquery", "the lexical channel matches any salient term"),

    ("Razorpay", "customer_id", "passing one breaks checkout with an error page at HTTP 200"),
    ("Razorpay", "recurring", "recurring was already enabled before anyone checked"),
    ("Razorpay", "webhook", "a signature is verified before the event is trusted"),
    ("Razorpay", "subscription", "the plan is 149 dollars for 500000 units"),
    ("Razorpay", "trial", "a trial end date is only honoured on the paid path"),
    ("Razorpay", "Ateleir Tech", "the account is shared between both products"),
    ("Razorpay", "methods", "the available methods endpoint answers before checkout"),

    ("AWS", "echo-mem-own", "our own account is not the default profile"),
    ("AWS", "KMS", "volumes and snapshots are encrypted at rest"),
    ("AWS", "OIDC", "a trust policy subject is immutable once created"),
    ("AWS", "cost", "the measured monthly bill is about 38 dollars"),
    ("AWS", "Lightsail", "the host runs a single compose file"),
    ("AWS", "Route 53", "the apex record points at the reverse proxy"),

    ("Colima", "clock skew", "the virtual machine clock drifts by about 24 milliseconds"),
    ("Docker", "context", "Desktop steals the context and hides every running container"),
    ("BM25", "statistics", "the setting was on while the derived data was absent"),
    ("salience", "base level activation", "frequency and recency combine into one number"),
    ("MMR", "lambda", "relevance stays dominant at 0.7 and variety breaks up runs"),
    ("graph hop", "entity_pair", "always on cost 0.142 of MRR on the shape it helped least"),
    ("LoCoMo", "multihop", "282 questions nobody derived from their own answers"),
    ("LongMemEval", "stratified sample", "ninety questions across five categories"),
    ("Zep", "episodes", "a graph name is not unique and creating twice makes two"),
    ("Mem0", "add", "a write returns pending and search needs an explicit filter"),
    ("cognee", "cognify", "the real write cost is the second step and not the first"),
    ("Letta", "server", "two named models are required before anything runs"),
    ("hook", "stop gate", "the gate fired seven times and produced one fact"),
    ("MCP", "tool description", "an agent reads the description before the schema"),
    ("writer version", "editable checkout", "an editable install stamps a stale version"),
    ("read event", "returned fact ids", "a read records which facts it handed back"),
]

# Rewrites of five of the above, same endpoints and relation, different text, so
# each supersedes its original. A store whose live view equals its write log
# would let the scratchpad be measured on data no reader would ever see.
SUPERSEDED = [
    ("Postgres", "port 5433", "the live store listens on 5433 and the suite must never touch it"),
    ("echo-mem", "Razorpay", "subscriptions are the only paid path and a new account gets nothing"),
    ("AWS", "cost", "the measured monthly bill is about 38 dollars, covered 3.8 times over"),
    ("dugout-be", "trial", "the account was cut off for seven days and reads were blocked too"),
    ("Razorpay", "subscription", "the plan is 149 dollars for 500000 units, raised from 99"),
]


def _write(conn, embedder, source, target, text) -> None:
    write_episode(
        conn, GROUP, "s1",
        [{"name": source, "type": "thing"}, {"name": target, "type": "thing"}],
        [{"source": source, "target": target, "relation_type": "relates",
          "fact": text, "confidence": "extracted"}],
        None, embedder, agent_id="claude-code", assume_new=True,
    )


@pytest.fixture
def seeded(migrated_db):
    """The corpus, written once for every test in this module.

    One embedder instance across all of it: the model costs about six seconds
    to load and rebuilding it per write would dominate the run.
    """
    embedder = LocalEmbedder()
    with connect(migrated_db) as conn:
        for source, target, text in CORPUS:
            _write(conn, embedder, source, target, text)
        for source, target, text in SUPERSEDED:
            _write(conn, embedder, source, target, text)
        yield conn, embedder


def test_the_corpus_is_what_it_claims_to_be(seeded):
    conn, _ = seeded
    total, live = conn.execute(
        f"""SELECT count(*),
                   count(*) FILTER (
                       WHERE (e.properties ->> '"t_invalid"'::agtype) IS NULL)
              FROM {GRAPH}."FACT" e
             WHERE (e.properties ->> '"group_id"'::agtype) = %s""",
        (GROUP,),
    ).fetchone()

    assert total == len(CORPUS) + len(SUPERSEDED)
    assert live == len(CORPUS)
    assert len(build_cases(conn, GROUP)) == len(CORPUS)


def test_a_superseded_fact_never_reaches_the_block(seeded):
    """The property the whole derived-scratchpad argument rests on: a generated
    summary of a store that changed its mind carries every version unless a
    model notices, and this carries the surviving one because the others have a
    t_invalid."""
    conn, _ = seeded
    block = scratchpad(conn, GROUP, limit=scratchpad_eval.PAD_DEPTH)
    rewritten = {(source, target) for source, target, _ in SUPERSEDED}
    superseded_texts = {
        text for source, target, text in CORPUS if (source, target) in rewritten
    }

    assert len(block) == len(CORPUS)
    assert superseded_texts
    assert not ({f["fact"] for f in block} & superseded_texts)


def test_the_block_is_ordered_by_centrality(seeded):
    conn, _ = seeded
    block = scratchpad(conn, GROUP, limit=10)

    centralities = [f["centrality"] for f in block]
    assert centralities == sorted(centralities, reverse=True)
    # The highest-degree hub has to be in the first few, or "most central
    # first" is not what this is doing.
    assert any(f["source"] == "echo-mem" or f["target"] == "echo-mem" for f in block[:3])


@pytest.mark.parametrize("shape", [SHAPE_ENTITY_PAIR, SHAPE_PROSE])
def test_the_measurement_is_matched_and_paired(seeded, shape, capsys):
    """Both arms score the same cases in the same order at the same spend.

    Without the first of those `compare`'s paired bootstrap is invalid, and
    without the last the comparison is between a big context and a small one
    rather than between two ways of choosing what goes in it.
    """
    conn, embedder = seeded
    measured = scratchpad_eval.measure(conn, GROUP, embedder, shape=shape)

    with capsys.disabled():
        print()
        print(scratchpad_eval.render(measured))

    base, var = measured["baseline"], measured["variant"]
    assert measured["cases"] == len(CORPUS)
    assert base.cases == var.cases == len(CORPUS)
    assert len(base.reciprocal_ranks) == len(var.reciprocal_ranks)
    assert measured["scratchpad_tokens"] <= measured["budget_tokens"]
    assert var.mean_tokens <= base.mean_tokens
    # The budget has to bite, or the block is the whole store and the question
    # being answered is not the one asked.
    assert measured["scratchpad_dropped"] > 0
    for key in ("mrr", "recall_at_10", "presence"):
        assert set(measured[key]) == {"delta", "low", "high", "significant"}
        assert measured[key]["low"] <= measured[key]["delta"] <= measured[key]["high"]


def test_the_sweep_says_how_much_context_the_block_needs(seeded, capsys):
    """The single-budget answer is not a property of the scratchpad alone. A
    query-independent block of k facts out of n, asked questions spread over
    the whole corpus, holds the answer about k/n of the time whatever its
    ordering, so what decides the comparison is how many facts the budget buys.
    """
    conn, embedder = seeded
    swept = scratchpad_eval.sweep(conn, GROUP, embedder)

    with capsys.disabled():
        print()
        print(scratchpad_eval.render_sweep(swept))

    facts = [arm["scratchpad_facts"] for arm in swept["arms"]]
    assert facts == sorted(facts)
    assert facts[-1] > facts[0]
    # One baseline, reused: the arms differ only in budget, and rescoring
    # query_memory per arm would spend one query per case per budget to get
    # the same numbers back.
    assert all(arm["variant"].cases == swept["baseline"].cases for arm in swept["arms"])


def test_a_scope_with_nothing_in_it_says_so_rather_than_scoring_zero(seeded):
    """An empty measurement and a measurement of zero are different claims, and
    only one of them is evidence about the scratchpad."""
    conn, embedder = seeded
    measured = scratchpad_eval.measure(conn, "user:nobody:shared", embedder)

    assert measured["cases"] == 0
    assert "no cases" in measured["error"]
    assert "no measurement" in scratchpad_eval.render(measured)
