"""assume_new: a caller that knows its entities are new should not have to be
asked.

From a report by a store whose entity names are source symbols and bug titles,
almost all of them genuinely new. It was deferring on a large share of its
writes, answering "new" every time, and paying a second call to say so. The
other half of that report - a deferral that shipped candidates far below the
bar - is test_a_deferral_lists_only_candidates_at_or_above_the_bar in
test_write_episode.py."""

from __future__ import annotations

from fake_embedder import REFERENCE, VectorEmbedder, unit_vector_at_angle

from echo_memory.infra.db import connect
from echo_memory.ingestion.write_episode import write_episode

GROUP = "g1"

# In the plane of the first two axes, so the similarity between any two of
# them is the cosine of the angle between them: arithmetic, not a model's
# opinion. 0.80 against REFERENCE, which is above LOW_THRESHOLD and below
# HIGH_THRESHOLD - the band where a write defers.
NEAR = unit_vector_at_angle(0.80)


def _embedder():
    return VectorEmbedder({
        "Postgres": REFERENCE,
        "Postgres DB": NEAR,
        "a fact about it": unit_vector_at_angle(0.3),
    })


def _seed(conn, embedder, *names):
    for name in names:
        write_episode(conn, GROUP, "seed", [{"name": name, "type": "tool"}], [], {}, embedder)


def _fact(name):
    return [{
        "source": name, "target": name, "relation_type": "is",
        "fact": "a fact about it", "confidence": "extracted",
    }]


def test_assume_new_writes_what_would_otherwise_have_deferred(migrated_db):
    conn = connect(migrated_db)
    embedder = _embedder()
    _seed(conn, embedder, "Postgres")

    deferred = write_episode(
        conn, GROUP, "s1", [{"name": "Postgres DB", "type": "tool"}],
        _fact("Postgres DB"), {}, embedder,
    )
    assert deferred["edges_created"] == []
    assert len(deferred["ambiguous_entities"]) == 1

    written = write_episode(
        conn, GROUP, "s2", [{"name": "Postgres DB", "type": "tool"}],
        _fact("Postgres DB"), {}, embedder, assume_new=True,
    )
    assert written["ambiguous_entities"] == []
    assert len(written["edges_created"]) == 1


def test_assume_new_does_not_override_an_explicit_resolution(migrated_db):
    """A caller that names one entity and assumes the rest is saying something
    more specific than the default, and specificity wins."""
    conn = connect(migrated_db)
    embedder = _embedder()
    _seed(conn, embedder, "Postgres")

    (postgres_id,) = conn.execute(
        "SELECT node_id::text FROM public.node_embedding WHERE group_id = %s", (GROUP,)
    ).fetchone()

    result = write_episode(
        conn, GROUP, "s1", [{"name": "Postgres DB", "type": "tool"}],
        _fact("Postgres DB"),
        {"Postgres DB": {"resolved_to": postgres_id}},
        embedder, assume_new=True,
    )

    assert result["ambiguous_entities"] == []
    (nodes,) = conn.execute(
        "SELECT count(*) FROM public.node_embedding WHERE group_id = %s", (GROUP,)
    ).fetchone()
    assert nodes == 1, "the explicit resolution pointed at the existing node"


def test_assume_new_still_loses_to_an_exact_name_match(migrated_db):
    """Case-insensitive name equality is this system's definition of identity.
    Saying "new" for a name the scope already holds has never been allowed to
    mint a second node (see test_new_entity_claim), and saying it in bulk
    must not either."""
    conn = connect(migrated_db)
    embedder = _embedder()
    _seed(conn, embedder, "Postgres")

    result = write_episode(
        conn, GROUP, "s1", [{"name": "Postgres", "type": "tool"}],
        _fact("Postgres"), {}, embedder, assume_new=True,
    )

    assert "error" not in result
    (nodes,) = conn.execute(
        "SELECT count(*) FROM public.node_embedding WHERE group_id = %s", (GROUP,)
    ).fetchone()
    assert nodes == 1
