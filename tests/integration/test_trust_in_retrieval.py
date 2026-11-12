"""Verification reaching a read, which is the half that cannot be tested inside
either module alone.

trust/verify.py knows what a state means. query_memory knows how to rank. The
thing that can go wrong is between them: a flag that is read but not applied, a
suppression that happens after truncation so the fact is gone but its slot is
spent, or a reweighting that quietly turns into a filter.
"""

from echo_memory.infra.db import connect
from echo_memory.ingestion.embeddings import LocalEmbedder
from echo_memory.ingestion.write_episode import write_episode
from echo_memory.retrieval.query_memory import query_memory
from echo_memory.trust import states, verify

GROUP = "g-trust"


def _write(conn, embedder, source, target, fact):
    out = write_episode(
        conn, GROUP, f"s-{source}-{target}",
        [{"name": source, "type": "tool"}, {"name": target, "type": "tool"}],
        [{"source": source, "target": target, "relation_type": "uses",
          "fact": fact, "confidence": "extracted"}],
        {}, embedder,
    )
    return str(out["edges_created"][0])


def test_the_flag_off_changes_nothing(migrated_db):
    """Every existing store has no verification rows, so the default must be
    indistinguishable from the behaviour before this shipped."""
    conn = connect(migrated_db)
    embedder = LocalEmbedder()
    _write(conn, embedder, "Decision", "Postgres", "we chose Postgres for storage")

    default = query_memory(conn, GROUP, "what database", 10, embedder)
    explicit_off = query_memory(conn, GROUP, "what database", 10, embedder, trust=False)

    assert default == explicit_off
    assert len(default["facts"]) == 1


def test_a_rejected_fact_is_not_returned(migrated_db):
    conn = connect(migrated_db)
    embedder = LocalEmbedder()
    good = _write(conn, embedder, "Decision", "Postgres", "we chose Postgres for storage")
    bad = _write(conn, embedder, "Decision", "MySQL", "we chose MySQL for storage")

    verify.record(
        conn, GROUP, bad, states.REJECTED,
        verifier_kind=states.HUMAN, verifier_id="reviewer@example.com",
        method="manual review",
    )

    result = query_memory(conn, GROUP, "what database did we choose", 10, embedder,
                          trust=True)

    returned = {f["fact_id"] for f in result["facts"]}
    assert bad not in returned
    assert good in returned


def test_suppression_does_not_cost_a_real_fact_its_slot(migrated_db):
    """The reason suppression happens before truncation. With top_k=1 and the
    rejected fact ranking first, filtering afterwards would return nothing."""
    conn = connect(migrated_db)
    embedder = LocalEmbedder()
    exact = _write(conn, embedder, "Deploy", "Release",
                   "deploys run only from the release branch")
    other = _write(conn, embedder, "Deploy", "Staging",
                   "deploy verification happens against staging first")

    verify.record(
        conn, GROUP, exact, states.REJECTED,
        verifier_kind=states.HUMAN, verifier_id="reviewer", method="manual review",
    )

    result = query_memory(
        conn, GROUP, "deploys run only from the release branch", 1, embedder,
        trust=True,
    )

    assert len(result["facts"]) == 1, "the slot the rejected fact held must be refilled"
    assert result["facts"][0]["fact_id"] == other


def test_a_disputed_fact_is_still_returned(migrated_db):
    """Disputed is authoritative but it is not suppression. A disputed claim a
    reader can see is worth more than a gap they cannot."""
    conn = connect(migrated_db)
    embedder = LocalEmbedder()
    edge = _write(conn, embedder, "Decision", "Postgres", "we chose Postgres for storage")

    verify.record(
        conn, GROUP, edge, states.DISPUTED,
        verifier_kind=states.HUMAN, verifier_id="reviewer", method="manual review",
    )

    result = query_memory(conn, GROUP, "what database", 10, embedder, trust=True)

    assert [f["fact_id"] for f in result["facts"]] == [edge]


def test_an_expired_verification_does_not_hide_the_fact(migrated_db):
    """A lapsed check is not a refutation. stale is advisory, so the fact
    reorders and never vanishes."""
    from datetime import UTC, datetime, timedelta

    conn = connect(migrated_db)
    embedder = LocalEmbedder()
    edge = _write(conn, embedder, "Decision", "Postgres", "we chose Postgres for storage")

    verify.record(
        conn, GROUP, edge, states.VERIFIED,
        verifier_kind=states.HUMAN, verifier_id="reviewer", method="manual review",
        expires_at=datetime.now(UTC) - timedelta(days=1),
    )

    assert verify.state_of(conn, GROUP, edge) == states.STALE
    result = query_memory(conn, GROUP, "what database", 10, embedder, trust=True)
    assert [f["fact_id"] for f in result["facts"]] == [edge]


def test_trust_never_introduces_a_fact_the_query_did_not_match(migrated_db):
    """The restraint salience.py spells out. A trusted fact is not an answer to
    an unrelated question."""
    conn = connect(migrated_db)
    embedder = LocalEmbedder()
    unrelated = _write(conn, embedder, "Lunch", "Pizza", "the team prefers pizza on Fridays")
    _write(conn, embedder, "Decision", "Postgres", "we chose Postgres for storage")

    verify.record(
        conn, GROUP, unrelated, states.VERIFIED,
        verifier_kind=states.HUMAN, verifier_id="reviewer", method="manual review",
    )

    result = query_memory(conn, GROUP, "what database did we choose", 10, embedder,
                          trust=True)

    assert unrelated not in {f["fact_id"] for f in result["facts"]}
