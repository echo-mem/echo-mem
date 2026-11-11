"""Cross scope retrieval: one ranked answer from several graphs.

The guarantee that matters most here is the negative one. A customer with a
`dev` memory and a `secrets` memory must be able to ask one question and never
see `secrets`, and the hardest case is not a vague query - it is a query that is
an exact match for a fact in the scope they were not granted, because that is
the strongest pull a search can get. test_isolation.py in the cloud repo makes
that point for accounts; this makes it for scopes inside one call.
"""

import pytest

from echo_memory.infra.db import connect
from echo_memory.ingestion.embeddings import LocalEmbedder
from echo_memory.ingestion.write_episode import write_episode
from echo_memory.retrieval.across import (
    MAX_GROUPS,
    ScopeViolation,
    _verify_scope,
    query_across,
)
from echo_memory.retrieval.query_memory import query_memory

SECRET = "the production signing key rotates on the first of every month"


def _write(conn, embedder, group_id, source, target, fact, relation="uses"):
    return write_episode(
        conn, group_id, f"s-{group_id}",
        [{"name": source, "type": "tool"}, {"name": target, "type": "tool"}],
        [{"source": source, "target": target, "relation_type": relation,
          "fact": fact, "confidence": "extracted"}],
        {}, embedder,
    )


# --------------------------------------------------------------- the baseline

def test_one_scope_is_byte_identical_to_query_memory(migrated_db):
    """The whole reason query_across composes query_memory rather than
    replacing it. If this drifts, every number the eval harness has produced
    stops being the baseline for the cross scope path."""
    conn = connect(migrated_db)
    embedder = LocalEmbedder()
    _write(conn, embedder, "g1", "Decision", "Postgres",
           "decided to use Postgres because SQLite could not handle concurrent writes")
    _write(conn, embedder, "g1", "Decision", "Redis", "chose Redis for the job queue")

    direct = query_memory(conn, "g1", "why Postgres", 10, embedder)
    across = query_across(conn, ["g1"], "why Postgres", 10, embedder)

    assert across == direct


def test_one_scope_does_not_gain_a_routing_block(migrated_db):
    """Additive keys appear only when they have something to say, which is the
    rule query_memory already follows for excluded_by_erasure."""
    conn = connect(migrated_db)
    embedder = LocalEmbedder()
    _write(conn, embedder, "g1", "Decision", "Postgres", "we use Postgres")

    result = query_across(conn, ["g1"], "Postgres", 10, embedder)

    assert "routing" not in result


# ------------------------------------------------------------------ isolation

def test_an_unauthorised_scope_never_appears(migrated_db):
    """The strongest pull a search can get: query the secret's own words while
    only authorised for the other scope."""
    conn = connect(migrated_db)
    embedder = LocalEmbedder()
    _write(conn, embedder, "secrets", "Key", "Rotation", SECRET)
    _write(conn, embedder, "dev", "Decision", "Postgres", "we use Postgres in dev")

    result = query_across(conn, ["dev"], SECRET, 10, embedder)

    texts = [f["fact"] for f in result["facts"]]
    assert SECRET not in texts
    assert all("signing key" not in t for t in texts)


def test_a_fact_from_another_scope_is_refused_rather_than_filtered(migrated_db):
    """_verify_scope raises. A filter would turn an authorization bug into a
    quietly thinner answer, which is the failure nobody notices."""
    conn = connect(migrated_db)
    embedder = LocalEmbedder()
    out = _write(conn, embedder, "secrets", "Key", "Rotation", SECRET)
    foreign_edge = out["edges_created"][0]

    with pytest.raises(ScopeViolation) as caught:
        _verify_scope(conn, [str(foreign_edge)], ["dev"])

    assert "authorization defect" in str(caught.value)


def test_verify_scope_passes_for_an_authorised_edge(migrated_db):
    conn = connect(migrated_db)
    embedder = LocalEmbedder()
    out = _write(conn, embedder, "dev", "Decision", "Postgres", "we use Postgres in dev")

    _verify_scope(conn, [str(out["edges_created"][0])], ["dev", "prod"])


# -------------------------------------------------------------------- fusion

def test_facts_from_several_scopes_are_fused_and_attributed(migrated_db):
    conn = connect(migrated_db)
    embedder = LocalEmbedder()
    _write(conn, embedder, "dev", "Deploy", "Staging", "dev deploys run against staging")
    _write(conn, embedder, "prod", "Deploy", "Release", "prod deploys only from the release branch")

    result = query_across(conn, ["dev", "prod"], "how do deploys work", 10, embedder)

    sources = {f["source"] for f in result["facts"]}
    assert sources == {"dev", "prod"}
    assert set(result["routing"]["considered"]) == {"dev", "prod"}


def test_a_weight_changes_the_order(migrated_db):
    """Weights exist because unweighted RRF gives every list's top slot the
    same value. Nothing here decides the number; this only proves the knob is
    connected, which is what two failed expansion attempts lacked."""
    conn = connect(migrated_db)
    embedder = LocalEmbedder()
    _write(conn, embedder, "dev", "Deploy", "Staging", "deploys run against staging")
    _write(conn, embedder, "prod", "Deploy", "Release", "deploys run from the release branch")

    dev_first = query_across(conn, ["dev", "prod"], "deploys", 10, embedder,
                             weights={"dev": 1.0, "prod": 0.01})
    prod_first = query_across(conn, ["dev", "prod"], "deploys", 10, embedder,
                              weights={"dev": 0.01, "prod": 1.0})

    assert dev_first["facts"][0]["source"] == "dev"
    assert prod_first["facts"][0]["source"] == "prod"


def test_an_unreadable_scope_does_not_lose_the_others(migrated_db):
    """A memory that has just been archived should degrade the answer, not fail
    the request."""
    conn = connect(migrated_db)
    embedder = LocalEmbedder()
    _write(conn, embedder, "dev", "Decision", "Postgres", "we use Postgres in dev")

    result = query_across(conn, ["dev", "empty-scope"], "Postgres", 10, embedder)

    assert [f["fact"] for f in result["facts"]] == ["we use Postgres in dev"]


# ----------------------------------------------------------------- validation

def test_a_repeated_scope_is_refused(migrated_db):
    """The same scope twice would vote twice in the fusion and silently double
    its weight."""
    result = query_across(connect(migrated_db), ["g1", "g1"], "q", 10, LocalEmbedder())
    assert "distinct" in result["error"]


def test_too_many_scopes_is_refused(migrated_db):
    many = [f"g{i}" for i in range(MAX_GROUPS + 1)]
    result = query_across(connect(migrated_db), many, "q", 10, LocalEmbedder())
    assert str(MAX_GROUPS) in result["error"]


def test_no_scopes_is_refused(migrated_db):
    result = query_across(connect(migrated_db), [], "q", 10, LocalEmbedder())
    assert "at least one" in result["error"]


def test_a_bad_top_k_is_refused(migrated_db):
    result = query_across(connect(migrated_db), ["g1"], "q", 0, LocalEmbedder())
    assert "positive integer" in result["error"]
