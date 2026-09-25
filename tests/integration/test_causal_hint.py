"""causal_hint on the write path: accepted, validated, stored.

The field is a small closed set rather than free text because a traversal has
to know which end of an edge is the cause, and it cannot learn that from a
string it has never seen. These tests hold that boundary: a known value is
stored, an unknown one is refused rather than dropped, and omitting it is the
ordinary case rather than an error."""

from __future__ import annotations

from fake_embedder import REFERENCE, VectorEmbedder, unit_vector_at_angle

from echo_memory.infra.db import connect
from echo_memory.ingestion.write_episode import write_episode

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


# --- the write side -----------------------------------------------------------

def test_a_causal_hint_is_stored_and_returned(migrated_db):
    conn = connect(migrated_db)
    embedder = _embedder()
    result = _write(
        conn, embedder, SQLITE, SWITCH,
        "SQLite could not take the write concurrency", hint="led_to",
    )
    assert len(result["edges_created"]) == 1

    (hint,) = conn.execute(
        """SELECT (e.properties ->> '"causal_hint"'::agtype)
             FROM echo_memory."FACT" e WHERE e.id = %s::graphid""",
        (result["edges_created"][0],),
    ).fetchone()
    assert hint == "led_to"


def test_an_unknown_hint_is_refused_rather_than_dropped(migrated_db):
    """A hint nobody can traverse is worse than no hint: the fact reads as
    causally typed and answers nothing."""
    conn = connect(migrated_db)
    result = _write(
        conn, _embedder(), SQLITE, SWITCH,
        "SQLite could not take the write concurrency", hint="because_of",
    )
    assert "invalid causal_hint" in result["error"]
    assert "caused_by" in result["error"], "the error has to name the valid set"


def test_omitting_the_hint_is_not_an_error(migrated_db):
    conn = connect(migrated_db)
    result = _write(
        conn, _embedder(), SQLITE, SWITCH, "an unrelated note about the pool"
    )
    assert len(result["edges_created"]) == 1
