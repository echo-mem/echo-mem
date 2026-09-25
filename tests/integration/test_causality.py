"""trace_cause: the traversal that makes causal typing worth recording.

The claim this feature has to earn is that echo-mem answers "why", not only
"what looks similar". A flat ranked list cannot: the answer to why a thing
happened is a chain, and a chain is structure.

So these tests assert structure. That a link is followed in the direction
causality runs rather than the direction a sentence was phrased in, that an
untyped fact is not dragged into a chain, that a contradiction is reported
and not walked, and that an empty answer says why it is empty rather than
looking like "no causes exist"."""

from __future__ import annotations

import pytest
from fake_embedder import REFERENCE, VectorEmbedder, unit_vector_at_angle

from echo_memory.infra.db import connect
from echo_memory.ingestion.write_episode import write_episode
from echo_memory.retrieval.causality import MAX_HOPS, trace_cause

GROUP = "g1"

SQLITE = "SQLite concurrent writes"
SWITCH = "switch to Postgres"
REWRITE = "the pool rewrite"
OUTAGE = "the Friday outage"

# Every string the store will embed, each at a known angle in the plane of the
# first two axes, so every similarity here is the cosine of an angle rather
# than a model's opinion. The four entity names are deliberately far apart:
# anchoring keeps everything within ANCHOR_MARGIN of the best match, so names
# that sit close together would anchor together and the tests would be
# measuring the fixture instead of the walk.
#
#   SWITCH    0 degrees     SQLITE   66.4    REWRITE  120    OUTAGE  160
#   closest pair is SQLITE/REWRITE at 0.59, well outside the margin
VECTORS = {
    SWITCH: REFERENCE,
    SQLITE: unit_vector_at_angle(0.40),
    REWRITE: unit_vector_at_angle(-0.50),
    OUTAGE: unit_vector_at_angle(-0.94),
    "SQLite could not take the write concurrency": unit_vector_at_angle(0.41),
    "so the store moved to Postgres": unit_vector_at_angle(0.42),
    "which made the pool rewrite necessary": unit_vector_at_angle(0.43),
    "the outage had nothing to do with it": unit_vector_at_angle(0.44),
    "an unrelated note about the pool": unit_vector_at_angle(0.45),
}


def _embedder():
    # write_episode embeds "<source> <target>. <fact>"; fake_embedder falls
    # back to the tail after ". " when the composed string is unregistered,
    # so only the fact texts and the entity names need vectors of their own.
    return VectorEmbedder(dict(VECTORS))


def _entity(name):
    return {"name": name, "type": "thing"}


def _write(conn, embedder, source, target, fact, hint=None, session="s1"):
    payload = {
        "source": source, "target": target, "relation_type": "relates",
        "fact": fact, "confidence": "extracted",
    }
    if hint is not None:
        payload["causal_hint"] = hint
    return write_episode(
        conn, GROUP, session, [_entity(source), _entity(target)], [payload],
        {}, embedder, assume_new=True,
    )


def _chain_facts(chains):
    return [[link["fact"] for link in chain] for chain in chains]


def test_a_chain_is_assembled_across_separately_written_facts(migrated_db):
    """Three sessions, three facts, one chain. No single write knew about the
    others, which is the case the graph exists for."""
    conn = connect(migrated_db)
    embedder = _embedder()
    _write(conn, embedder, SQLITE, SWITCH,
           "SQLite could not take the write concurrency", "led_to", session="s1")
    _write(conn, embedder, SWITCH, REWRITE,
           "which made the pool rewrite necessary", "led_to", session="s2")

    traced = trace_cause(conn, GROUP, "switch to Postgres", embedder)

    assert _chain_facts(traced["causes"]) == [
        ["SQLite could not take the write concurrency"]
    ]
    assert _chain_facts(traced["effects"]) == [
        ["which made the pool rewrite necessary"]
    ]


def test_a_chain_runs_further_than_one_hop_and_reads_outwards(migrated_db):
    """The point of a chain is the second link. Anchored on the rewrite, the
    answer to "why" is the switch first and SQLite behind it, in that order -
    nearest cause first, because that is the order the question is asked in.

    Neither write knew about the other. One session recorded the database
    limit, another recorded the rewrite, and the chain is the graph's."""
    conn = connect(migrated_db)
    embedder = _embedder()
    _write(conn, embedder, SQLITE, SWITCH,
           "SQLite could not take the write concurrency", "led_to", session="s1")
    _write(conn, embedder, SWITCH, REWRITE,
           "which made the pool rewrite necessary", "led_to", session="s2")

    traced = trace_cause(conn, GROUP, REWRITE, embedder, direction="upstream")

    assert [a["name"] for a in traced["anchors"]] == [REWRITE]
    assert _chain_facts(traced["causes"]) == [
        ["which made the pool rewrite necessary"],
        ["which made the pool rewrite necessary",
         "SQLite could not take the write concurrency"],
    ]


def test_max_hops_stops_the_walk(migrated_db):
    conn = connect(migrated_db)
    embedder = _embedder()
    _write(conn, embedder, SQLITE, SWITCH,
           "SQLite could not take the write concurrency", "led_to")
    _write(conn, embedder, SWITCH, REWRITE,
           "which made the pool rewrite necessary", "led_to")

    traced = trace_cause(conn, GROUP, REWRITE, embedder, direction="upstream", max_hops=1)

    assert _chain_facts(traced["causes"]) == [
        ["which made the pool rewrite necessary"]
    ]


def test_direction_follows_the_hint_not_the_arrow(migrated_db):
    """"A led_to B" and "B caused_by A" are one claim written from opposite
    ends, and a caller uses whichever fits the sentence it just read. Stored
    as an arrow they point opposite ways; the hint is what makes them agree.

    Here the arrow runs SWITCH -> SQLITE, which is backwards, and caused_by
    says so. A walk that trusted the arrow would file the cause as an effect.
    """
    conn = connect(migrated_db)
    embedder = _embedder()
    _write(conn, embedder, SWITCH, SQLITE,
           "SQLite could not take the write concurrency", "caused_by")

    traced = trace_cause(conn, GROUP, "switch to Postgres", embedder)

    assert _chain_facts(traced["causes"]) == [
        ["SQLite could not take the write concurrency"]
    ]
    assert traced["effects"] == []


def test_an_untyped_fact_is_not_part_of_a_chain(migrated_db):
    """The default is associative and most facts are. Walking them would make
    every chain the whole neighbourhood, which is what query_memory's graph
    hop already does and measures badly at."""
    conn = connect(migrated_db)
    embedder = _embedder()
    _write(conn, embedder, SWITCH, REWRITE, "an unrelated note about the pool")

    traced = trace_cause(conn, GROUP, "switch to Postgres", embedder)

    assert traced["causes"] == []
    assert traced["effects"] == []
    assert "no causal_hint" in traced["note"], (
        "an empty answer has to say it means nobody asserted a cause"
    )


def test_contradictions_are_reported_and_not_walked(migrated_db):
    """contradicts is symmetric, so it has no direction to walk. Growing a
    chain through it would assert an ordering nobody stated."""
    conn = connect(migrated_db)
    embedder = _embedder()
    _write(conn, embedder, SWITCH, OUTAGE,
           "the outage had nothing to do with it", "contradicts")

    traced = trace_cause(conn, GROUP, "switch to Postgres", embedder)

    assert [f["fact"] for f in traced["contradictions"]] == [
        "the outage had nothing to do with it"
    ]
    assert traced["causes"] == []
    assert traced["effects"] == []


def test_a_cycle_terminates(migrated_db):
    """Two facts that each cause the other is a thing a store can contain,
    and a walk that re-expands a node it has already reached runs to max_hops
    reporting the same pair over and over."""
    conn = connect(migrated_db)
    embedder = _embedder()
    _write(conn, embedder, SQLITE, SWITCH,
           "SQLite could not take the write concurrency", "led_to")
    _write(conn, embedder, SWITCH, SQLITE,
           "so the store moved to Postgres", "led_to")

    traced = trace_cause(conn, GROUP, "switch to Postgres", embedder, max_hops=MAX_HOPS)

    assert len(traced["causes"]) <= 2
    assert len(traced["effects"]) <= 2


@pytest.mark.parametrize(
    "direction,expect_causes,expect_effects",
    [("upstream", True, False), ("downstream", False, True), ("both", True, True)],
)
def test_direction_selects_which_half_is_walked(
    migrated_db, direction, expect_causes, expect_effects
):
    conn = connect(migrated_db)
    embedder = _embedder()
    _write(conn, embedder, SQLITE, SWITCH,
           "SQLite could not take the write concurrency", "led_to")
    _write(conn, embedder, SWITCH, REWRITE,
           "which made the pool rewrite necessary", "led_to")

    traced = trace_cause(conn, GROUP, "switch to Postgres", embedder, direction=direction)

    assert bool(traced["causes"]) == expect_causes
    assert bool(traced["effects"]) == expect_effects


def test_bad_arguments_are_refused_with_the_usual_error_shape(migrated_db):
    conn = connect(migrated_db)
    embedder = _embedder()
    assert "direction" in trace_cause(conn, GROUP, "x", embedder, direction="sideways")["error"]
    assert "max_hops" in trace_cause(conn, GROUP, "x", embedder, max_hops=99)["error"]
    assert "subject" in trace_cause(conn, GROUP, "   ", embedder)["error"]


def test_a_scope_with_nothing_matching_says_so(migrated_db):
    conn = connect(migrated_db)
    embedder = _embedder()
    traced = trace_cause(conn, GROUP, "switch to Postgres", embedder)
    assert traced["anchors"] == []
    assert "nothing in this scope" in traced["note"]
