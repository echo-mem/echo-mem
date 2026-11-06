"""How long a scope may keep a fact, and that enforcing it deletes properly.

Erasure answers a subject who asks to be forgotten. Retention is the standing
rule: GDPR Article 5(1)(e) and the DPDP Act both require that data is not kept
longer than its purpose needs. A store with erasure and no retention can delete
on demand and cannot say why it still holds anything.

Most of this file is about refusing. The destructive half is one call, and the
ways it should decline to run are the part worth pinning.
"""

from __future__ import annotations

import pytest
from fake_embedder import REFERENCE, VectorEmbedder

from echo_memory import retention
from echo_memory.infra.db import GRAPH_NAME as GRAPH
from echo_memory.infra.db import connect
from echo_memory.ingestion.write_episode import write_episode

GROUP = "g-retention"
FACT = "the deploy branch is master"


def _write(conn, fact: str = FACT, age_days: int = 0) -> str:
    """One fact, optionally backdated so retention can see it as old."""
    embedder = VectorEmbedder({fact: REFERENCE, "thing": REFERENCE, "other": REFERENCE})
    result = write_episode(
        conn, GROUP, "s1",
        [{"name": "thing", "type": "x"}, {"name": "other", "type": "x"}],
        [{"source": "thing", "target": "other", "relation_type": "about",
          "fact": fact, "confidence": "extracted"}],
        {"thing": {"resolved_to": "new"}, "other": {"resolved_to": "new"}},
        embedder, assume_new=True,
    )
    edge_id = result["edges_created"][0]
    if age_days:
        # t_valid is the instant the fact became true, which is what retention
        # measures from. Backdating it is how a test reaches an age without
        # waiting, and it is also the real case: a fact can be recorded today
        # about something that happened last year.
        conn.execute(
            f"""UPDATE "echo_memory"."FACT"
                   SET properties = properties || (
                       '{{"t_valid": ' ||
                       (extract(epoch FROM now())::bigint - {age_days * 86400})::text
                       || '}}')::agtype
                 WHERE id = %s::graphid""",
            (edge_id,),
        )
    return edge_id


# --- the policy itself ---------------------------------------------------


def test_a_policy_needs_a_reason_and_a_decider(migrated_db):
    """"We delete after 90 days" is not a compliance answer. The purpose and
    the person who decided are the parts nobody can reconstruct later, which
    is why they are NOT NULL rather than nice to have."""
    with connect(migrated_db) as conn:
        for reason, who in (("", "ayush"), ("   ", "ayush"), ("the window", "")):
            with pytest.raises(retention.RetentionError):
                retention.set_policy(conn, GROUP, 90, reason, who)


def test_a_policy_must_keep_data_for_at_least_a_day(migrated_db):
    """Zero or negative means "delete everything immediately", which is never
    what somebody meant to type. Refused in Python and by a CHECK, because the
    second one covers whatever writes the row next."""
    with connect(migrated_db) as conn:
        for days in (0, -1, -365):
            with pytest.raises(retention.RetentionError):
                retention.set_policy(conn, GROUP, days, "a reason", "ayush")


def test_setting_a_policy_retires_the_one_it_replaces(migrated_db):
    """Retired rather than updated, so "why did we stop deleting after 90
    days" stays answerable. Two live policies would be a question with no
    answer, and the shorter one silently winning is behaviour nobody chose."""
    with connect(migrated_db) as conn:
        retention.set_policy(conn, GROUP, 90, "the dispute window", "ayush")
        retention.set_policy(conn, GROUP, 30, "shortened after legal review", "ayush")

        live = retention.policy(conn, GROUP)
        past = retention.history(conn, GROUP)

    assert live.max_age_days == 30
    assert len(past) == 2, "the replaced policy was deleted rather than retired"
    assert past[0]["retired"] == "", "the live one is marked retired"
    assert past[1]["retired"] != "", "the replaced one is not marked retired"
    assert past[1]["reason"] == "the dispute window", "the old reason survives"


def test_no_policy_is_a_real_answer_and_is_not_forever(migrated_db):
    """None means nobody has decided, which is different from "we keep it
    forever". The difference matters because one of them is a finding."""
    with connect(migrated_db) as conn:
        assert retention.policy(conn, GROUP) is None


# --- what is due --------------------------------------------------------


def test_due_measures_age_from_when_the_fact_became_true(migrated_db):
    """Not from when we were told. A fact backdated into the past is older
    than the write that recorded it, and retention is about the data's age
    rather than our knowledge of it."""
    with connect(migrated_db) as conn:
        fresh = _write(conn, "written today")
        old = _write(conn, "happened long ago", age_days=400)

        overdue = retention.due(conn, GROUP, 365)

    assert old in overdue
    assert fresh not in overdue


def test_due_includes_superseded_versions(migrated_db):
    """A superseded fact is still stored and still says whatever it said.
    Leaving it out would let a store satisfy a retention policy while keeping
    every old version of everything, which is the opposite of the point."""
    with connect(migrated_db) as conn:
        old = _write(conn, "the old wording", age_days=400)
        conn.execute(
            """UPDATE "echo_memory"."FACT"
                   SET properties = properties ||
                       ('{"t_invalid": ' || extract(epoch FROM now())::bigint::text
                        || '}')::agtype
                 WHERE id = %s::graphid""",
            (old,),
        )

        assert old in retention.due(conn, GROUP, 365)


# --- enforcement --------------------------------------------------------


def test_enforce_refuses_when_nobody_has_set_a_policy(migrated_db):
    """Inventing a default here would be this module deciding a compliance
    question on the operator's behalf, in the direction of deleting their
    data."""
    with connect(migrated_db) as conn:
        _write(conn, "a fact", age_days=400)

        with pytest.raises(retention.RetentionError, match="no retention policy"):
            retention.enforce(conn, GROUP, dry_run=False)


def test_a_dry_run_is_the_default_and_deletes_nothing(migrated_db):
    """The signature matters as much as the behaviour: a function whose
    default is permanent deletion of customer data is one somebody calls while
    exploring."""
    with connect(migrated_db) as conn:
        old = _write(conn, "old enough to go", age_days=400)
        retention.set_policy(conn, GROUP, 365, "the dispute window", "ayush")

        out = retention.enforce(conn, GROUP)          # no dry_run argument

        still_there = conn.execute(
            'SELECT count(*) FROM "echo_memory"."FACT" WHERE id = %s::graphid',
            (old,),
        ).fetchone()[0]

    assert out.dry_run is True
    assert out.n_due == 1
    assert out.erased == 0
    assert out.nodes_erased == 0
    assert still_there == 1, "a dry run deleted a fact"


def test_enforcing_deletes_the_overdue_and_keeps_the_rest(migrated_db):
    with connect(migrated_db) as conn:
        old = _write(conn, "old enough to go", age_days=400)
        fresh = _write(conn, "still within the window")
        retention.set_policy(conn, GROUP, 365, "the dispute window", "ayush")

        out = retention.enforce(conn, GROUP, dry_run=False, requested_by="ayush")

        rows = conn.execute(
            'SELECT id::text FROM "echo_memory"."FACT" WHERE id = ANY(%s::text[]::graphid[])',
            ([old, fresh],),
        ).fetchall()

    assert out.erased == 1
    assert [r[0] for r in rows] == [fresh], "the wrong fact survived"


def test_enforcement_leaves_the_same_evidence_a_subject_request_leaves(migrated_db):
    """It deletes THROUGH erasure rather than beside it, so retention and a
    subject request produce one kind of record rather than two. A second
    deletion path would be a second place to get versions and orphans wrong,
    and the second one is the one nobody tests as hard."""
    with connect(migrated_db) as conn:
        _write(conn, "old enough to go", age_days=400)
        policy = retention.set_policy(conn, GROUP, 365, "the dispute window", "ayush")

        retention.enforce(conn, GROUP, dry_run=False, requested_by="ayush")

        rows = conn.execute(
            "SELECT verb, requested_by, reason FROM public.erasure_event "
            "WHERE group_id = %s ORDER BY id DESC",
            (GROUP,),
        ).fetchall()

    assert rows, "a retention deletion left no erasure_event"
    verb, who, reason = rows[0]
    assert verb == "retention", "it is not distinguishable from a subject request"
    assert who == "ayush"
    assert str(policy.id) in reason, "the record does not say which policy"
    assert "old enough to go" not in (reason or ""), (
        "the compliance record contains the text it was supposed to remove"
    )


def test_enforcing_with_nothing_due_is_not_an_error(migrated_db):
    """The common case once a scope is in a steady state. It should be quiet
    rather than exceptional, or whatever runs it nightly learns to ignore it."""
    with connect(migrated_db) as conn:
        _write(conn, "recent")
        retention.set_policy(conn, GROUP, 365, "the dispute window", "ayush")

        out = retention.enforce(conn, GROUP, dry_run=False)

    assert out.is_empty()
    assert out.erased == 0


def test_enforcement_does_not_leave_the_entity_name_behind(migrated_db):
    """The one thing erasure.plan would have done that building a plan by age
    does not: an entity whose last fact just expired is left as a bare node,
    and the name is often the personal data. "Dr Patel" with no facts attached
    is still a record that Dr Patel was in here."""
    with connect(migrated_db) as conn:
        _write(conn, "old enough to go", age_days=400)
        retention.set_policy(conn, GROUP, 365, "the dispute window", "ayush")

        out = retention.enforce(conn, GROUP, dry_run=False, requested_by="ayush")

        left = conn.execute(
            f'SELECT count(*) FROM {GRAPH}."Node" '
            f"""WHERE (properties ->> '"group_id"'::agtype) = %s""",
            (GROUP,),
        ).fetchone()[0]

    assert out.nodes_erased == 2, "the orphaned entities were not removed"
    assert left == 0


def test_a_node_that_was_already_isolated_is_left_alone(migrated_db):
    """Retention deletes what its policy covers and nothing else. A node with
    no facts before this ran was not part of what expired, and removing it
    would be enforcement quietly doing more than the policy says."""
    with connect(migrated_db) as conn:
        retention.set_policy(conn, GROUP, 365, "the dispute window", "ayush")
        _write(conn, "old enough to go", age_days=400)
        conn.execute(
            f"""SELECT * FROM cypher('{GRAPH}', $$
                  CREATE (:Node {{name: 'lonely', group_id: '{GROUP}'}})
                $$) AS (v agtype)"""
        )

        retention.enforce(conn, GROUP, dry_run=False, requested_by="ayush")

        survivors = conn.execute(
            f"""SELECT (properties ->> '"name"'::agtype) FROM {GRAPH}."Node"
                 WHERE (properties ->> '"group_id"'::agtype) = %s""",
            (GROUP,),
        ).fetchall()

    assert [r[0] for r in survivors] == ["lonely"]
