"""Saying the same thing twice must not count as saying something new.

Edge identity is (source, target, relation_type) and never the text, and
`_create_edge` used to run unconditionally. So an identical re-write created a
second edge, ended the first, and emitted a supersession whose `before_fact`
equalled its `after_fact`, while reporting `stored: true`.

That is the mechanism behind a customer edge carrying 47 versions of one
sentence, and behind 543 writes producing 112 facts while they were billed per
write for restating what the store already held. Reproduced 2026-10-21 before
the fix: three identical writes gave three versions and one distinct text.

The hard part is not detecting a duplicate. It is not detecting a CHANGE as
one, so most of these tests are about what must still supersede.
"""

from __future__ import annotations

from fake_embedder import REFERENCE, VectorEmbedder

from echo_memory.infra.db import GRAPH_NAME as GRAPH
from echo_memory.infra.db import connect
from echo_memory.ingestion.write_episode import write_episode

GROUP = "g1"
FACT = "the ground averages 188 in the first innings"
OTHER = "the ground averages 192 in the first innings"

_GROUP_PROP = """(properties ->> '"group_id"'::agtype)"""
_FACT_PROP = """(properties ->> '"fact"'::agtype)"""


def _embedder():
    return VectorEmbedder({FACT: REFERENCE, OTHER: REFERENCE, "Ground": REFERENCE,
                           "Average": REFERENCE})


def _write(conn, embedder, session, text=FACT, confidence="extracted", hint=None):
    fact = {
        "source": "Ground", "target": "Average", "relation_type": "averages",
        "confidence": confidence, "fact": text,
    }
    if hint is not None:
        fact["causal_hint"] = hint
    return write_episode(
        conn, GROUP, session,
        [{"name": "Ground", "type": "place"}, {"name": "Average", "type": "stat"}],
        [fact], None, embedder, assume_new=True,
    )


def _versions(conn):
    return conn.execute(
        f"""SELECT count(*) FROM {GRAPH}."FACT" WHERE {_GROUP_PROP} = %s""", (GROUP,)
    ).fetchone()[0]


def test_an_identical_rewrite_stores_nothing(migrated_db):
    conn = connect(migrated_db)
    embedder = _embedder()
    _write(conn, embedder, "s1")

    again = _write(conn, embedder, "s2")

    assert again["edges_created"] == []
    assert again["superseded"] == []
    assert again["restated"] != []
    assert _versions(conn) == 1, "a restatement must not add a version"


def test_three_identical_writes_leave_one_version(migrated_db):
    """The customer shape. Before the fix this was three versions of one
    sentence, each one billed."""
    conn = connect(migrated_db)
    embedder = _embedder()
    for i in range(3):
        _write(conn, embedder, f"s{i}")

    texts = conn.execute(
        f"""SELECT count(DISTINCT {_FACT_PROP}) FROM {GRAPH}."FACT"
             WHERE {_GROUP_PROP} = %s""", (GROUP,)
    ).fetchone()[0]

    assert _versions(conn) == 1
    assert texts == 1


def test_a_restatement_writes_no_supersession_audit_row(migrated_db):
    """The old behaviour left an audit trail asserting a change that did not
    happen, with before_fact equal to after_fact."""
    conn = connect(migrated_db)
    embedder = _embedder()
    _write(conn, embedder, "s1")
    _write(conn, embedder, "s2")

    rows = conn.execute(
        """SELECT count(*) FROM public.audit_entry
            WHERE group_id = %s AND mutation_type = 'fact_superseded'""",
        (GROUP,),
    ).fetchone()[0]

    assert rows == 0


def test_changed_text_still_supersedes(migrated_db):
    conn = connect(migrated_db)
    embedder = _embedder()
    _write(conn, embedder, "s1")

    changed = _write(conn, embedder, "s2", text=OTHER)

    assert changed["edges_created"] != []
    assert changed["superseded"] != []
    assert changed["stored"] is True


def test_changed_confidence_still_supersedes(migrated_db):
    """The text is identical and the fact is not. A claim retyped from
    inferred to extracted is a different claim about how it is known."""
    conn = connect(migrated_db)
    embedder = _embedder()
    _write(conn, embedder, "s1", confidence="inferred")

    retyped = _write(conn, embedder, "s2", confidence="extracted")

    assert retyped["superseded"] != []
    assert retyped["restated"] == []


def test_a_newly_typed_causal_hint_still_supersedes(migrated_db):
    """Adding a hint is what makes a fact traceable, so it has to land."""
    conn = connect(migrated_db)
    embedder = _embedder()
    _write(conn, embedder, "s1")

    typed = _write(conn, embedder, "s2", hint="caused_by")

    assert typed["superseded"] != []
    assert typed["restated"] == []


def test_the_unchanged_message_is_now_true_when_it_fires(migrated_db):
    """It used to be unreachable for any episode holding a ready fact, so the
    branch saying everything already exists could only fire where that was
    false."""
    conn = connect(migrated_db)
    embedder = _embedder()
    _write(conn, embedder, "s1")

    again = _write(conn, embedder, "s2")

    assert again["stored"] is False
    assert again["not_stored"] == "unchanged"
    assert "already exist" in again["not_stored_detail"]
    assert "re-sending will not store them" in again["not_stored_detail"]


def test_an_episode_asserting_no_facts_says_so(migrated_db):
    """Registration only. Nodes may well have been created, so reporting
    'every fact already exists, unaltered' was wrong in the one case it could
    fire."""
    conn = connect(migrated_db)
    embedder = _embedder()

    result = write_episode(
        conn, GROUP, "s1", [{"name": "Ground", "type": "place"}], [],
        None, embedder, assume_new=True,
    )

    assert result["stored"] is False
    assert result["not_stored"] == "no_facts"
    assert "registered" in result["not_stored_detail"]


def test_a_retry_after_an_apparent_failure_is_safe(migrated_db):
    """The reason this matters operationally. With no idempotency key, a
    caller retrying a timed out write used to add an edge and burn a
    supersession on whichever attempt had actually landed."""
    conn = connect(migrated_db)
    embedder = _embedder()
    first = _write(conn, embedder, "s1")

    retry = _write(conn, embedder, "s1")

    assert _versions(conn) == 1
    assert retry["restated"] == first["edges_created"]
