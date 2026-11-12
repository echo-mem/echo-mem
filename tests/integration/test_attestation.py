"""Nothing changes a fact behind this store's back without that being visible.

Provenance here was believed because the database said so: every fact records
an author, a session and a time, all editable by anyone who can write SQL. For
AI governance that is the gap that matters, because the value of "which agent
asserted this" is precisely that it cannot be edited later by whoever finds the
answer inconvenient.

So the tests that matter are the tampering ones, and each attacks a different
field: the text, the endpoints, and the author. Two more pin the honesty of
what the chain claims - supersession must not read as tampering, and a gap an
erasure accounts for must not read as evidence removal.
"""

from __future__ import annotations

import json
import os

import pytest
from fake_embedder import REFERENCE, VectorEmbedder
from hash_embedder import HashEmbedder

from echo_memory import attestation, erasure
from echo_memory.infra.db import GRAPH_NAME as GRAPH
from echo_memory.infra.db import connect
from echo_memory.ingestion.write_episode import write_episode

GROUP = "g-attest"


def _write(conn, fact: str, source: str = "billing", target: str = "Razorpay") -> str:
    embedder = VectorEmbedder({fact: REFERENCE, source: REFERENCE, target: REFERENCE})
    result = write_episode(
        conn, GROUP, "s1",
        [{"name": source, "type": "x"}, {"name": target, "type": "x"}],
        [{"source": source, "target": target, "relation_type": "uses",
          "fact": fact, "confidence": "extracted"}],
        {source: {"resolved_to": "new"}, target: {"resolved_to": "new"}},
        embedder, assume_new=True,
    )
    assert result["edges_created"], result
    return result["edges_created"][0]


def _set_property(conn, edge_id: str, key: str, value: str) -> None:
    """Edit a fact the way somebody with SQL access would: straight at the
    table, with nothing in this package involved."""
    conn.execute(
        f"""UPDATE {GRAPH}."FACT"
               SET properties = properties || ('{{"{key}": "{value}"}}')::agtype
             WHERE id = %s::graphid""",
        (edge_id,),
    )


# --- the chain ----------------------------------------------------------


def test_a_written_fact_is_attested_without_being_asked(migrated_db):
    """On the write path, not as a separate step an operator can forget. A
    fact that is only attested when somebody remembers to is attested for the
    facts nobody is suspicious about."""
    with connect(migrated_db) as conn:
        edge_id = _write(conn, "billing uses Razorpay")

        row = conn.execute(
            "SELECT seq, prev_digest, digest FROM public.fact_attestation "
            "WHERE edge_id = %s::graphid",
            (edge_id,),
        ).fetchone()

    assert row is not None, "a fact was written with no attestation"
    seq, prev, digest = row
    assert seq == 1
    assert prev == attestation.GENESIS, "the first link does not chain to genesis"
    assert digest.startswith(("sha256:", "hmac-sha256:"))


def test_each_fact_chains_to_the_one_before_it(migrated_db):
    """What makes this a chain rather than a column of digests: changing one
    fact means recomputing every digest after it, so a single edit cannot be
    repaired locally."""
    with connect(migrated_db) as conn:
        _write(conn, "billing uses Razorpay")
        _write(conn, "deploys run from main", source="deploys", target="main")

        rows = conn.execute(
            "SELECT seq, prev_digest, digest FROM public.fact_attestation "
            "WHERE group_id = %s ORDER BY seq",
            (GROUP,),
        ).fetchall()

    assert [r[0] for r in rows] == [1, 2]
    assert rows[1][1] == rows[0][2], "the second link does not chain to the first"


def test_one_scope_cannot_perturb_another_scopes_chain(migrated_db):
    """Scopes are tenants. A chain whose head moved when another account wrote
    would make every verification depend on every tenant, and would hand one
    account a way to invalidate another's evidence."""
    with connect(migrated_db) as conn:
        _write(conn, "billing uses Razorpay")
        before = attestation.head(conn, GROUP)

        other = VectorEmbedder({"other scope fact": REFERENCE, "a": REFERENCE, "b": REFERENCE})
        write_episode(
            conn, "g-other", "s1",
            [{"name": "a", "type": "x"}, {"name": "b", "type": "x"}],
            [{"source": "a", "target": "b", "relation_type": "uses",
              "fact": "other scope fact", "confidence": "extracted"}],
            {"a": {"resolved_to": "new"}, "b": {"resolved_to": "new"}},
            other, assume_new=True,
        )

        assert attestation.head(conn, GROUP) == before
        assert attestation.head(conn, "g-other")[0] == 1


def test_a_clean_store_verifies_clean(migrated_db):
    with connect(migrated_db) as conn:
        _write(conn, "billing uses Razorpay")
        _write(conn, "deploys run from main", source="deploys", target="main")

        out = attestation.report(conn, GROUP)

    assert out.is_clean()
    assert out.n_attested == 2
    assert out.altered == []
    assert out.broken_links == []


# --- tampering ----------------------------------------------------------


def test_changing_a_facts_text_is_detected(migrated_db):
    """The straightforward attack. Rewriting what a fact says, at the table,
    with nothing in this package involved."""
    with connect(migrated_db) as conn:
        edge_id = _write(conn, "billing uses Razorpay")
        _set_property(conn, edge_id, "fact", "billing uses Stripe")

        out = attestation.report(conn, GROUP)

    assert not out.is_clean()
    assert [a["edge_id"] for a in out.altered] == [edge_id]
    assert out.altered[0]["explanation"] is None, (
        "an edit nothing in the audit log accounts for was reported as explained"
    )


def test_rewriting_the_author_is_detected(migrated_db):
    """The one that matters most for governance. Who asserted a fact is worth
    recording only if it cannot be changed afterwards by whoever would rather
    it said somebody else."""
    with connect(migrated_db) as conn:
        edge_id = _write(conn, "billing uses Razorpay")
        _set_property(conn, edge_id, "agent_id", "somebody-else")

        out = attestation.report(conn, GROUP)

    assert [a["edge_id"] for a in out.altered] == [edge_id]


def test_moving_an_edge_to_another_entity_is_detected(migrated_db):
    """A fact can be made to say something else without touching a single one
    of its own properties: repoint it. "billing uses Razorpay" becomes
    "billing uses Stripe" with one UPDATE to end_id, which is why the digest
    covers the endpoint names."""
    with connect(migrated_db) as conn:
        edge_id = _write(conn, "billing uses Razorpay")
        _write(conn, "payments use Stripe", source="payments", target="Stripe")

        conn.execute(
            f"""UPDATE {GRAPH}."FACT" SET end_id = (
                    SELECT id FROM {GRAPH}."Node"
                     WHERE (properties ->> '"name"'::agtype) = 'Stripe'
                       AND (properties ->> '"group_id"'::agtype) = %s)
                 WHERE id = %s::graphid""",
            (GROUP, edge_id),
        )

        out = attestation.report(conn, GROUP)

    assert edge_id in [a["edge_id"] for a in out.altered], (
        "an edge was repointed at a different entity and the digest did not notice"
    )


def test_a_break_early_in_the_chain_does_not_report_every_fact_after_it(migrated_db):
    """One cause, one finding. Recomputing each digest against the prev_digest
    that was stored rather than the one expected keeps a single broken link
    from reporting the whole tail as altered, which is the shape of report
    nobody reads twice."""
    with connect(migrated_db) as conn:
        first = _write(conn, "billing uses Razorpay")
        _write(conn, "deploys run from main", source="deploys", target="main")
        _write(conn, "alerts go to Slack", source="alerts", target="Slack")
        _set_property(conn, first, "fact", "billing uses Stripe")

        out = attestation.report(conn, GROUP)

    assert len(out.altered) == 1, (
        f"one edit reported {len(out.altered)} altered facts"
    )


def test_a_removed_link_shows_as_a_broken_chain(migrated_db):
    """Deleting the attestation row rather than the fact. The chain is what
    makes this visible: the row after it still names a prev_digest that is now
    nowhere."""
    with connect(migrated_db) as conn:
        _write(conn, "billing uses Razorpay")
        _write(conn, "deploys run from main", source="deploys", target="main")
        conn.execute("DELETE FROM public.fact_attestation WHERE group_id = %s AND seq = 1",
                     (GROUP,))

        out = attestation.report(conn, GROUP)

    assert out.broken_links, "a deleted link left the chain looking continuous"
    assert out.broken_links[0]["seq"] == 2


# --- the honesty of what it claims --------------------------------------


def test_supersession_is_not_tampering(migrated_db):
    """A superseded fact gets t_invalid set long after it was written, which
    is the model working. Covering it would make every superseded fact fail
    verification, and a report that cries wolf on normal operation is a report
    nobody reads."""
    with connect(migrated_db) as conn:
        edge_id = _write(conn, "billing uses Razorpay")
        conn.execute(
            f"""UPDATE {GRAPH}."FACT"
                   SET properties = properties ||
                       ('{{"t_invalid": ' || extract(epoch FROM now())::bigint::text
                        || '}}')::agtype
                 WHERE id = %s::graphid""",
            (edge_id,),
        )

        out = attestation.report(conn, GROUP)

    assert out.is_clean(), "supersession was reported as tampering"


def test_a_recorded_mutation_is_labelled_rather_than_called_tampering(migrated_db):
    """merge, unmerge and reattribute all legitimately rewrite a fact or its
    endpoints, and all three write an audit entry. The fact still is not what
    was attested and the report still says so - but it says why, which is the
    difference between a finding and a false alarm."""
    with connect(migrated_db) as conn:
        edge_id = _write(conn, "billing uses Razorpay")
        _set_property(conn, edge_id, "agent_id", "cursor")
        conn.execute(
            """INSERT INTO public.audit_entry
                   (group_id, session_id, writer_version, mutation_type,
                    affected_edge_ids, summary)
               VALUES (%s, 's1', '0.0.0', 'entity_resolved',
                       %s::text[]::graphid[], 'agent_id corrected by hand')""",
            (GROUP, [edge_id]),
        )

        out = attestation.report(conn, GROUP)

    assert len(out.altered) == 1
    explanation = out.altered[0]["explanation"]
    assert explanation is not None, "a recorded mutation was reported as unexplained"
    assert "entity_resolved" in explanation


def test_an_erased_fact_is_a_gap_the_erasure_log_accounts_for(migrated_db):
    """Erasure deletes facts on purpose and leaves the attestation row, which
    is deliberate: the gap is what makes the deletion visible. Whether it was
    erasure doing its job or somebody removing evidence is answered by
    reconciling against erasure_event, and nothing else can answer it."""
    with connect(migrated_db) as conn:
        edge_id = _write(conn, "billing uses Razorpay")
        erasure.erase(
            conn, GROUP,
            erasure.Plan(verb="fact", group_id=GROUP, edge_ids=[edge_id], node_ids=[]),
            requested_by="subject request",
        )

        out = attestation.report(conn, GROUP)

    assert out.vanished == [], (
        "a fact erased on request was reported as unaccounted for"
    )
    assert out.erased >= 1, "the erasure log was not consulted"


def test_a_fact_that_vanished_with_no_erasure_is_reported(migrated_db):
    """The other half of the same reconciliation, and the one that is a
    finding: a fact gone from the graph with no erasure recorded anywhere."""
    with connect(migrated_db) as conn:
        edge_id = _write(conn, "billing uses Razorpay")
        conn.execute(f'DELETE FROM {GRAPH}."FACT" WHERE id = %s::graphid', (edge_id,))

        out = attestation.report(conn, GROUP)

    assert out.vanished == [edge_id]
    assert not out.is_clean()


def test_the_report_says_whether_the_chain_is_keyed(migrated_db):
    """A digest in the same database as the data it attests is worth nothing
    against somebody who can recompute it. Keyed or witnessed externally is
    the whole difference, and a report that said "verified" without saying
    which would be the line somebody quotes."""
    with connect(migrated_db) as conn:
        _write(conn, "billing uses Razorpay")
        unkeyed = attestation.report(conn, GROUP)

    assert unkeyed.keyed is False
    assert "outside this database" in unkeyed.strength

    os.environ[attestation.KEY_ENV] = "a-test-key"
    try:
        with connect(migrated_db) as conn:
            _write(conn, "deploys run from main", source="deploys", target="main")
            mixed = attestation.report(conn, GROUP)
    finally:
        del os.environ[attestation.KEY_ENV]

    assert mixed.mixed_keying is True, (
        "a chain attested partly keyed and partly not was described as one or "
        "the other"
    )
    assert "part of this chain is keyed" in mixed.strength


def test_a_keyed_digest_cannot_be_recomputed_without_the_key(migrated_db):
    """The property the key buys. Without it, anybody who can write to this
    database can also write a digest that verifies."""
    os.environ[attestation.KEY_ENV] = "a-test-key"
    try:
        with connect(migrated_db) as conn:
            edge_id = _write(conn, "billing uses Razorpay")
            body = attestation.canonical(attestation.claims_of(conn, GROUP, edge_id))
            keyed = attestation.digest(attestation.GENESIS, body)
    finally:
        del os.environ[attestation.KEY_ENV]

    assert keyed.startswith("hmac-sha256:")
    assert attestation.digest(attestation.GENESIS, body) != keyed


# --- backfill -----------------------------------------------------------


def test_backfill_attests_what_predates_it_and_says_so(migrated_db):
    """Facts written before this shipped have no digest, and attesting them
    establishes a baseline from now rather than a statement about the past.
    attested_at is what keeps that visible, which is why this is an operator
    command and not something init-db does quietly."""
    with connect(migrated_db) as conn:
        edge_id = _write(conn, "billing uses Razorpay")
        conn.execute("DELETE FROM public.fact_attestation WHERE group_id = %s", (GROUP,))

        assert attestation.unattested(conn, GROUP) == [edge_id]
        assert attestation.backfill(conn, GROUP) == 1
        assert attestation.unattested(conn, GROUP) == []

        out = attestation.report(conn, GROUP)

    assert out.is_clean()


def test_attesting_a_fact_that_is_not_there_is_refused(migrated_db):
    with connect(migrated_db) as conn, \
            pytest.raises(attestation.AttestationError, match="no fact"):
        attestation.attest(conn, GROUP, "999999")


# --- the two readers must agree, forever ---------------------------------


def _claims_through_cypher(conn, group_id: str, edge_id: str) -> dict:
    """What `claims_of` used to do, kept here on purpose.

    `claims_of` reads the edge and node tables now, because asking through
    Cypher left the pattern unbound and AGE expanded every FACT edge in the
    graph before filtering by id: 6.785 ms against 0.694 ms at 5,807 edges, on
    the write path, once per fact.

    The two reads do not return the same bytes. Cypher returns JSON quoted
    agtype and `->>` returns raw text, and 51 of 63 fields differ in their raw
    form across facts containing newlines, quotes, backslashes, unicode, tabs
    and JSON-looking text. They agree once decoded, and that agreement is what
    makes the change safe, because this function feeds the digest.

    So the old reader lives in the test rather than in the module. If either
    side drifts, this fails. Without it the failure mode is silent and total:
    every fact in every store reports as altered, and nothing says why.
    """
    row = conn.execute(
        f"""SELECT * FROM cypher('{GRAPH}', $$
            MATCH (a)-[e:FACT]->(b)
            WHERE id(e) = $eid AND e.group_id = $gid
            RETURN a.name, b.name, e.relation_type, e.fact, e.confidence,
                   e.causal_hint, e.t_valid, e.agent_id, e.project,
                   e.provenance
        $$, %s) AS (source agtype, target agtype, relation_type agtype,
                     fact agtype, confidence agtype, causal_hint agtype,
                     t_valid agtype, agent_id agtype, project agtype,
                     provenance agtype)""",
        (json.dumps({"eid": int(edge_id), "gid": group_id}),),
    ).fetchone()
    assert row is not None
    values = [attestation._value(x) for x in row]
    covered = dict(zip(attestation.COVERED, values[2:9], strict=True))
    provenance = values[9] or {}
    return {
        "group_id": group_id,
        "source": values[0],
        "target": values[1],
        **covered,
        "session_id": provenance.get("session_id")
        if isinstance(provenance, dict) else None,
    }


HOSTILE_FACTS = [
    ("plain", "billing uses Razorpay", "led_to"),
    ("newline", "line one\nline two\nline three", "caused_by"),
    ("quotes", 'he said "deploy from release" and meant it', None),
    ("backslash", r"the path is C:\Users\work\thing", None),
    ("unicode", "the cafe costs 500 rupees, naive", None),
    ("json_text", '{"not": "a map", "just": "text"}', None),
    ("tab", "a\tb\tc", None),
    ("colon", "note: the ratio is 3:1 at 09:30", None),
]


def test_both_readers_decode_a_fact_identically(migrated_db):
    """The equivalence the digest rests on, over text chosen to break a naive
    decoder. Checked field by field rather than by comparing digests, so a
    failure says WHICH property moved."""
    conn = connect(migrated_db)
    embedder = HashEmbedder()

    for i, (name, text, hint) in enumerate(HOSTILE_FACTS):
        fact = {
            "source": f"S{i}", "target": f"T{i}", "relation_type": "uses",
            "fact": text, "confidence": "extracted",
        }
        if hint:
            fact["causal_hint"] = hint
        out = write_episode(
            conn, GROUP, f"hostile{i}",
            [{"name": f"S{i}", "type": "tool"}, {"name": f"T{i}", "type": "tool"}],
            [fact], None, embedder, assume_new=True,
        )
        edge_id = str((out.get("edges_created") or [None])[0])
        assert edge_id != "None", f"{name} was not stored: {out}"

        from_tables = attestation.claims_of(conn, GROUP, edge_id)
        from_cypher = _claims_through_cypher(conn, GROUP, edge_id)
        assert from_tables == from_cypher, (
            f"the readers disagree on {name!r}: "
            f"{ {k: (from_tables.get(k), from_cypher.get(k)) for k in set(from_tables) | set(from_cypher) if from_tables.get(k) != from_cypher.get(k)} }"
        )


def test_every_stored_digest_still_recomputes(migrated_db):
    """The whole point, stated as the assertion a release should care about.

    Writes a populated scope, then recomputes every digest from the chain's own
    prev_digest and the claims as read today. A reader change that altered any
    covered value would break this for every row at once.
    """
    conn = connect(migrated_db)
    embedder = HashEmbedder()
    for i in range(25):
        write_episode(
            conn, GROUP, f"bulk{i}",
            [{"name": f"N{i}", "type": "t"}, {"name": f"M{i}", "type": "t"}],
            [{"source": f"N{i}", "target": f"M{i}", "relation_type": "uses",
              "fact": f"fact {i} with enough text to be realistic",
              "confidence": "extracted"}],
            None, embedder, assume_new=True,
        )

    rows = conn.execute(
        """SELECT edge_id::text, seq, prev_digest, digest
             FROM public.fact_attestation WHERE group_id = %s ORDER BY seq""",
        (GROUP,),
    ).fetchall()
    assert len(rows) >= 25

    for edge_id, seq, prev, stored in rows:
        claims = attestation.claims_of(conn, GROUP, edge_id)
        again = attestation.digest(prev, attestation.canonical(claims))
        assert again == stored, f"digest moved at seq {seq} for edge {edge_id}"

    assert attestation.report(conn, GROUP).is_clean()
