"""Erasure removes the text from everywhere, not just from the graph.

Every test here corresponds to a way this can be implemented wrongly and still
look right from the outside. The one that matters most is the first: deleting
the edge and declaring victory passes any test that only asks whether the
fact is still retrievable, while the text sits in the audit log, the causal
quotes, and the lexeme table.
"""

from __future__ import annotations

import pytest
from fake_embedder import REFERENCE, VectorEmbedder, unit_vector_at_angle

from echo_memory import erasure
from echo_memory.infra.db import GRAPH_NAME as GRAPH
from echo_memory.infra.db import connect
from echo_memory.ingestion.write_episode import write_episode

GROUP = "g1"

# A nonsense surname, so a leak check cannot pass by accident on a word that
# happens to appear somewhere else in the fixture.
SUBJECT = "Zarquon"
SECRET = "Zarquon missed the deadline twice"
UNRELATED = "the deployment finished on a Friday"

_FACT = """(properties ->> '"fact"'::agtype)"""
_NAME = """(properties ->> '"name"'::agtype)"""
_GROUP = """(properties ->> '"group_id"'::agtype)"""


def _embedder():
    return VectorEmbedder(
        {
            SUBJECT: REFERENCE,
            "Deadline": unit_vector_at_angle(0.1),
            "Deployment": unit_vector_at_angle(0.2),
            "Friday": unit_vector_at_angle(0.3),
            SECRET: REFERENCE,
            UNRELATED: unit_vector_at_angle(0.4),
        }
    )


def _seed(conn, embedder):
    write_episode(
        conn,
        GROUP,
        "s1",
        [{"name": SUBJECT, "type": "person"}, {"name": "Deadline", "type": "thing"}],
        [
            {
                "source": SUBJECT,
                "target": "Deadline",
                "relation_type": "missed",
                "confidence": "extracted",
                "fact": SECRET,
            }
        ],
        None,
        embedder,
        assume_new=True,
    )
    write_episode(
        conn,
        GROUP,
        "s2",
        [{"name": "Deployment", "type": "thing"}, {"name": "Friday", "type": "day"}],
        [
            {
                "source": "Deployment",
                "target": "Friday",
                "relation_type": "happened_on",
                "confidence": "extracted",
                "fact": UNRELATED,
            }
        ],
        None,
        embedder,
        assume_new=True,
    )


def _leaks(conn) -> dict[str, int]:
    """Every place the subject's text could still be, counted.

    Asserting on the literal string across all of them, rather than checking
    the edge is gone, is the whole point: the edge going is the easy part.
    """
    like = f"%{SUBJECT}%"
    return {
        "fact_text": conn.execute(
            f"""SELECT count(*) FROM {GRAPH}."FACT" WHERE {_FACT} LIKE %s""", (like,)
        ).fetchone()[0],
        "node_name": conn.execute(
            f"""SELECT count(*) FROM {GRAPH}."Node" WHERE {_NAME} LIKE %s""", (like,)
        ).fetchone()[0],
        "audit_before": conn.execute(
            "SELECT count(*) FROM public.audit_entry WHERE before_fact LIKE %s", (like,)
        ).fetchone()[0],
        "audit_after": conn.execute(
            "SELECT count(*) FROM public.audit_entry WHERE after_fact LIKE %s", (like,)
        ).fetchone()[0],
        "audit_summary": conn.execute(
            "SELECT count(*) FROM public.audit_entry WHERE summary LIKE %s", (like,)
        ).fetchone()[0],
        "audit_detail": conn.execute(
            "SELECT count(*) FROM public.audit_entry WHERE resolution_detail LIKE %s",
            (like,),
        ).fetchone()[0],
        "causal_quote": conn.execute(
            "SELECT count(*) FROM public.causal_hint_scan WHERE quote LIKE %s", (like,)
        ).fetchone()[0],
    }


def test_the_text_is_gone_from_every_table_that_held_it(migrated_db):
    conn = connect(migrated_db)
    embedder = _embedder()
    _seed(conn, embedder)

    # Precondition: it really is in the graph before the erasure, or this test
    # would pass against a store that never held it.
    assert _leaks(conn)["fact_text"] == 1

    plan = erasure.plan(conn, GROUP, entity=SUBJECT)
    erasure.erase(conn, GROUP, plan, requested_by="test")

    assert _leaks(conn) == {
        "fact_text": 0,
        "node_name": 0,
        "audit_before": 0,
        "audit_after": 0,
        "audit_summary": 0,
        "audit_detail": 0,
        "causal_quote": 0,
    }


def test_erasing_one_subject_leaves_everything_else(migrated_db):
    conn = connect(migrated_db)
    embedder = _embedder()
    _seed(conn, embedder)

    erasure.erase(conn, GROUP, erasure.plan(conn, GROUP, entity=SUBJECT), requested_by="test")

    surviving = [
        r[0]
        for r in conn.execute(
            f"""SELECT {_FACT} FROM {GRAPH}."FACT" WHERE {_GROUP} = %s""", (GROUP,)
        ).fetchall()
    ]
    assert surviving == [UNRELATED]


def test_a_shared_endpoint_survives_if_another_fact_still_needs_it(migrated_db):
    """Erasing a subject must not take its neighbours with it.

    Deadline is only reachable through the erased fact, so it goes. A node
    another fact still points at has to stay, or erasing one person's data
    deletes somebody else's.
    """
    conn = connect(migrated_db)
    embedder = _embedder()
    _seed(conn, embedder)
    # A second fact about Deadline, so it is no longer an orphan.
    write_episode(
        conn,
        GROUP,
        "s3",
        [{"name": "Deployment", "type": "thing"}, {"name": "Deadline", "type": "thing"}],
        [
            {
                "source": "Deployment",
                "target": "Deadline",
                "relation_type": "met",
                "confidence": "extracted",
                "fact": UNRELATED,
            }
        ],
        None,
        embedder,
    )

    erasure.erase(conn, GROUP, erasure.plan(conn, GROUP, entity=SUBJECT), requested_by="test")

    names = sorted(
        r[0]
        for r in conn.execute(
            f"""SELECT {_NAME} FROM {GRAPH}."Node" WHERE {_GROUP} = %s""", (GROUP,)
        ).fetchall()
    )
    assert "Deadline" in names
    assert SUBJECT not in names


def test_every_version_of_a_triple_goes_together(migrated_db):
    """A chain with a hole marks exactly where something was removed, which is
    the opposite of what an erasure is for."""
    conn = connect(migrated_db)
    embedder = _embedder()
    second = "Zarquon missed the deadline three times"
    embedder = VectorEmbedder({**_embedder()._vectors, second: REFERENCE})
    _seed(conn, embedder)
    # Same triple, new text: supersedes, so the scope now holds two versions.
    write_episode(
        conn,
        GROUP,
        "s4",
        [{"name": SUBJECT, "type": "person"}, {"name": "Deadline", "type": "thing"}],
        [
            {
                "source": SUBJECT,
                "target": "Deadline",
                "relation_type": "missed",
                "confidence": "extracted",
                "fact": second,
            }
        ],
        None,
        embedder,
    )
    versions = conn.execute(
        f"""SELECT count(*) FROM {GRAPH}."FACT"
             WHERE {_GROUP} = %s AND {_FACT} LIKE %s""",
        (GROUP, f"%{SUBJECT}%"),
    ).fetchone()[0]
    assert versions == 2, "precondition: supersession kept the old version"

    plan = erasure.plan(conn, GROUP, entity=SUBJECT)
    assert plan.n_edges == 2
    erasure.erase(conn, GROUP, plan, requested_by="test")
    assert _leaks(conn)["fact_text"] == 0


def test_embeddings_go_with_the_facts_they_describe(migrated_db):
    """Inversion recovers a meaningful fraction of the text from the vector, so
    a surviving embedding is a surviving fact."""
    conn = connect(migrated_db)
    embedder = _embedder()
    _seed(conn, embedder)
    before = conn.execute(
        "SELECT count(*) FROM public.fact_embedding WHERE group_id = %s", (GROUP,)
    ).fetchone()[0]

    erasure.erase(conn, GROUP, erasure.plan(conn, GROUP, entity=SUBJECT), requested_by="test")

    after = conn.execute(
        "SELECT count(*) FROM public.fact_embedding WHERE group_id = %s", (GROUP,)
    ).fetchone()[0]
    assert (before, after) == (2, 1)


def test_no_lexeme_of_an_erased_fact_survives(migrated_db):
    """lexical_term holds the actual tokens, so leaving it is leaving the
    erased fact's vocabulary queryable."""
    conn = connect(migrated_db)
    embedder = _embedder()
    _seed(conn, embedder)
    from echo_memory.retrieval import bm25

    bm25.refresh(conn, GROUP)
    assert (
        conn.execute(
            "SELECT count(*) FROM public.lexical_term WHERE group_id = %s", (GROUP,)
        ).fetchone()[0]
        > 0
    ), "precondition: statistics were built"

    erasure.erase(conn, GROUP, erasure.plan(conn, GROUP, entity=SUBJECT), requested_by="test")

    assert (
        conn.execute(
            "SELECT count(*) FROM public.lexical_term WHERE group_id = %s", (GROUP,)
        ).fetchone()[0]
        == 0
    )


def test_a_read_event_keeps_its_row_and_loses_the_pointer(migrated_db):
    """The event is evidence a read happened, which the meter and criterion 6
    count. The id is a pointer, not content."""
    conn = connect(migrated_db)
    embedder = _embedder()
    _seed(conn, embedder)
    edge_id = conn.execute(
        f"""SELECT id::text FROM {GRAPH}."FACT" WHERE {_FACT} = %s""", (SECRET,)
    ).fetchone()[0]
    conn.execute(
        """INSERT INTO public.read_event
               (group_id, kind, n_facts, injected_chars, returned_fact_ids)
           VALUES (%s, 'query_memory', 2, 100, %s)""",
        (GROUP, [edge_id, "999"]),
    )

    erasure.erase(conn, GROUP, erasure.plan(conn, GROUP, entity=SUBJECT), requested_by="test")

    row = conn.execute(
        "SELECT n_facts, returned_fact_ids FROM public.read_event WHERE group_id = %s",
        (GROUP,),
    ).fetchone()
    assert row[0] == 2, "the count of what was read is unchanged"
    assert row[1] == ["999"], "only the erased id was stripped"


def test_an_audit_row_is_redacted_rather_than_deleted(migrated_db):
    """trial_observation.audit_entry_id is a NO ACTION foreign key, so deleting
    a referenced audit row errors. The row stays, emptied."""
    conn = connect(migrated_db)
    embedder = _embedder()
    _seed(conn, embedder)
    before = conn.execute(
        "SELECT count(*) FROM public.audit_entry WHERE group_id = %s", (GROUP,)
    ).fetchone()[0]

    erasure.erase(conn, GROUP, erasure.plan(conn, GROUP, entity=SUBJECT), requested_by="test")

    after = conn.execute(
        "SELECT count(*) FROM public.audit_entry WHERE group_id = %s", (GROUP,)
    ).fetchone()[0]
    assert after == before, "no audit row was deleted"
    assert (
        conn.execute(
            "SELECT count(*) FROM public.audit_entry WHERE summary = %s", (erasure.REDACTED,)
        ).fetchone()[0]
        > 0
    )


def test_the_erasure_record_holds_counts_and_not_the_subject(migrated_db):
    """A compliance record listing the names of people who asked to be
    forgotten is the thing they asked to not exist."""
    conn = connect(migrated_db)
    embedder = _embedder()
    _seed(conn, embedder)

    plan = erasure.plan(conn, GROUP, entity=SUBJECT)
    result = erasure.erase(
        conn, GROUP, plan, requested_by="test", reason="subject request", request_ref="DSR-1"
    )

    row = conn.execute(
        """SELECT verb, n_edges, n_nodes, reason, request_ref, status
             FROM public.erasure_event WHERE id = %s""",
        (result["erasure_id"],),
    ).fetchone()
    assert row == ("entity", 1, 2, "subject request", "DSR-1", "completed")

    # Nothing in the row may contain the subject, in any column.
    everything = conn.execute(
        "SELECT erasure_event::text FROM public.erasure_event WHERE id = %s",
        (result["erasure_id"],),
    ).fetchone()[0]
    assert SUBJECT not in everything


def test_the_digest_is_absent_without_a_key_and_stable_with_one(monkeypatch):
    """Unset means counts-only evidence, which is honest. A key that lived in
    the database would let a dump holder recompute every candidate name."""
    monkeypatch.delenv(erasure.KEY_ENV, raising=False)
    assert erasure.subject_digest(SUBJECT) is None

    monkeypatch.setenv(erasure.KEY_ENV, "k1")
    first = erasure.subject_digest(SUBJECT)
    assert first is not None
    assert erasure.subject_digest(f"  {SUBJECT.lower()} ") == first, "normalised"
    monkeypatch.setenv(erasure.KEY_ENV, "k2")
    assert erasure.subject_digest(SUBJECT) != first, "keyed, not a plain hash"


def test_a_search_never_erases(migrated_db):
    """A text match is not a subject. A false positive here destroys a fact
    nobody asked to erase and there is no undo."""
    conn = connect(migrated_db)
    embedder = _embedder()
    _seed(conn, embedder)

    found = erasure.candidates(conn, GROUP, SUBJECT)

    assert [text for _, text in found] == [SECRET]
    assert _leaks(conn)["fact_text"] == 1, "listing candidates changed nothing"


def test_erasing_a_whole_scope_leaves_nothing(migrated_db):
    conn = connect(migrated_db)
    embedder = _embedder()
    _seed(conn, embedder)

    erasure.erase(conn, GROUP, erasure.plan(conn, GROUP, scope=True), requested_by="test")

    assert (
        conn.execute(
            f"""SELECT count(*) FROM {GRAPH}."FACT" WHERE {_GROUP} = %s""", (GROUP,)
        ).fetchone()[0]
        == 0
    )
    assert (
        conn.execute(
            f"""SELECT count(*) FROM {GRAPH}."Node" WHERE {_GROUP} = %s""", (GROUP,)
        ).fetchone()[0]
        == 0
    )


def test_a_missing_target_is_refused_rather_than_silently_doing_nothing(migrated_db):
    conn = connect(migrated_db)
    _seed(conn, _embedder())

    with pytest.raises(erasure.ErasureError, match="no entity named"):
        erasure.plan(conn, GROUP, entity="Nobody At All")
    with pytest.raises(erasure.ErasureError, match="no fact"):
        erasure.plan(conn, GROUP, fact="999999")
    with pytest.raises(erasure.ErasureError, match="exactly one"):
        erasure.plan(conn, GROUP)


def test_no_tool_published_over_mcp_can_erase():
    """Irreversible deletion of a whole scope, one prompt injection away from
    any agent holding a key, is not a trade worth making.

    Reads the tools the server actually publishes rather than the source, so
    this fails when someone registers one, not when someone names a function.
    """
    from echo_memory import server

    published = {t.name for t in server.server._tool_manager.list_tools()}

    assert published, "precondition: the server publishes some tools"
    # Retention enforcement is in here because it IS erasure: it hands its
    # overdue facts to erase() and deletes exactly as permanently. An agent
    # that can set a one day policy and enforce it has an erase_scope by
    # another name.
    forbidden = {
        "forget", "erase", "delete", "purge", "erase_scope",
        "retain", "retention", "set_retention", "enforce_retention",
    }
    assert not (published & forbidden), (
        f"erasure must not be reachable over MCP: {sorted(published & forbidden)}"
    )


def test_no_tool_published_over_mcp_can_grant_trust_or_export():
    """The same argument as erasure, for the two powers added since.

    Verification decides what retrieval returns, so a tool that could set a
    state would let any agent holding a key promote its own claims - which is
    the elevation events.envelope.REQUIRES_VERIFY exists to deny, and denying
    it in one layer while publishing a tool for it in another would be
    pointless. Exchange hands one organisation's memory to another, and
    `promote` decides that an imported bundle may be read at all.

    None of these is a step in an agent's loop. They are a person's decision,
    and the operator CLI is where a person makes it.

    Reads the live registry rather than the source for the same reason the
    erasure guard does: this fails when somebody registers one, not when
    somebody names a function.
    """
    from echo_memory import server

    published = {t.name for t in server.server._tool_manager.list_tools()}

    assert published, "precondition: the server publishes some tools"
    forbidden = {
        # Trust: setting a state, or approving the rules that set one.
        "verify", "verify_fact", "record_verification", "set_verification",
        "reject_fact", "dispute_fact", "approve_policy",
        # The names `cli/trust.py` introduces. Added here rather than only in
        # test_cli_trust.py so that this list stays the one place to read for
        # "what must never be a tool", and so that deleting that file cannot
        # also delete the guard.
        "trust", "trust_verify", "trust_reject", "trust_dispute",
        "trust_evidence", "trust_lessons", "record_evidence", "link_evidence",
        # Exchange: sending, accepting, or making an import readable.
        "export", "export_bundle", "import", "import_bundle",
        "promote", "promote_bundle", "revoke", "revoke_bundle", "exchange",
    }
    assert not (published & forbidden), (
        f"trust and exchange must not be reachable over MCP: "
        f"{sorted(published & forbidden)}"
    )


def test_an_as_of_answer_says_what_erasure_removed(migrated_db):
    """The honesty cost of erasure, paid out loud.

    A query about a past moment is a claim about what was believed then, and
    an erasure since makes that claim incomplete. Returning a thinner past
    silently would turn the one property a reader is meant to rely on into one
    they cannot.
    """
    import time as _time

    from echo_memory.retrieval.query_memory import query_memory

    conn = connect(migrated_db)
    embedder = _embedder()
    _seed(conn, embedder)
    before_erasure = int(_time.time())

    erasure.erase(conn, GROUP, erasure.plan(conn, GROUP, entity=SUBJECT), requested_by="test")

    # Asking about the present is not a claim about the past, so it says nothing.
    now = query_memory(conn, GROUP, UNRELATED, 10, embedder)
    assert "excluded_by_erasure" not in now

    past = query_memory(conn, GROUP, UNRELATED, 10, embedder, as_of=before_erasure)
    excluded = past["excluded_by_erasure"]
    assert excluded["n_facts"] == 1
    assert "excludes 1 fact(s) erased on" in excluded["note"]
    assert len(excluded["erasures"]) == 1


def test_an_erasure_before_the_as_of_moment_is_not_reported(migrated_db):
    """It removed facts that were already gone by then, so it changes nothing
    about what was believed at that moment."""
    import time as _time

    from echo_memory.retrieval.query_memory import query_memory

    conn = connect(migrated_db)
    embedder = _embedder()
    _seed(conn, embedder)
    erasure.erase(conn, GROUP, erasure.plan(conn, GROUP, entity=SUBJECT), requested_by="test")
    _time.sleep(1.1)
    after_erasure = int(_time.time())

    past = query_memory(conn, GROUP, UNRELATED, 10, embedder, as_of=after_erasure)

    assert "excluded_by_erasure" not in past


def test_erasure_reaches_the_intelligence_tables(migrated_db):
    """A deletion request has to reach every table holding the subject's data,
    and five were added after this module was written.

    Split the way the rest of erase() splits. A pointer and a piece of content
    are deleted; a record that something happened is kept with its prose
    stripped, because the record is what makes a chain over it mean anything
    and an id on its own is not content.
    """
    import json
    from datetime import UTC, datetime

    from echo_memory.events import envelope as events
    from echo_memory.events import inbox
    from echo_memory.trust import evidence as evidence_mod
    from echo_memory.trust import states, verify

    conn = connect(migrated_db)
    embedder = _embedder()
    _seed(conn, embedder)

    subject_edge, other_edge = (
        str(r[0]) for r in conn.execute(
            f"""SELECT e.id FROM {GRAPH}."FACT" e
                 WHERE {_GROUP.replace('properties', 'e.properties')} = %s
                 ORDER BY CASE WHEN {_FACT.replace('properties', 'e.properties')}
                               LIKE %s THEN 0 ELSE 1 END, e.id""",
            (GROUP, f"%{SUBJECT}%"),
        ).fetchall()[:2]
    )

    shared = evidence_mod.record(
        conn, GROUP, "human_correction", uri="https://tickets.example/9001",
        detail={"note": f"{SUBJECT} asked for a correction"},
        reporter_agent="claude-code",
    )
    evidence_mod.link(conn, GROUP, subject_edge, shared)
    evidence_mod.link(conn, GROUP, other_edge, shared)

    private = evidence_mod.record(
        conn, GROUP, "human_correction", uri="https://tickets.example/9002",
        detail={"note": f"{SUBJECT} home address"}, reporter_agent="claude-code",
    )
    evidence_mod.link(conn, GROUP, subject_edge, private)

    verify.record(
        conn, GROUP, subject_edge, states.VERIFIED, verifier_kind=states.HUMAN,
        verifier_id="reviewer", method=f"spoke to {SUBJECT} directly",
        detail=f"{SUBJECT} confirmed by email",
    )

    inbox.enqueue(conn, GROUP, events.Envelope(
        event_id="erasure-reach-0001",
        event_type=events.OUTCOME_OBSERVED,
        occurred_at=datetime.now(UTC),
        actor=events.Actor(agent="ci-reporter"),
        scope=events.Scope(memory=None, scope="shared"),
        payload={"decision": "d", "outcome": "o", "sign": "success",
                 "detail": f"reported by {SUBJECT}"},
    ))
    # mark() takes the scope now: migration 0037 keyed the inbox on
    # (group_id, event_id), because a global event_id made one tenant's
    # idempotency namespace visible to another.
    inbox.mark(conn, GROUP, "erasure-reach-0001", inbox.APPLIED,
               edge_ids=[subject_edge])

    erasure.erase(
        conn, GROUP, erasure.plan(conn, GROUP, entity=SUBJECT), requested_by="test",
    )

    assert conn.execute(
        "SELECT count(*) FROM public.fact_evidence WHERE edge_id = %s::graphid",
        (subject_edge,),
    ).fetchone()[0] == 0

    remaining = {
        r[0] for r in conn.execute(
            "SELECT id FROM public.evidence WHERE group_id = %s", (GROUP,)
        ).fetchall()
    }
    assert private not in remaining, "evidence nothing points at any more must go"
    assert shared in remaining, "evidence a surviving fact still cites must stay"

    row = conn.execute(
        """SELECT method, detail FROM public.fact_verification
            WHERE edge_id = %s::graphid""",
        (subject_edge,),
    ).fetchone()
    assert row is not None, "deleting the row would break verification_attestation"
    assert row[0] == "redacted"
    assert row[1] is None

    event = conn.execute(
        """SELECT envelope, applied_edge_ids FROM public.event_inbox
            WHERE event_id = %s""",
        ("erasure-reach-0001",),
    ).fetchone()
    assert event is not None, "the idempotency key must survive an erasure"
    assert event[0].get("redacted") is True
    assert SUBJECT not in json.dumps(event[0])
    assert subject_edge not in [str(x) for x in (event[1] or [])]
