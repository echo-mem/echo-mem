"""The measurement has to see the facts it is measuring.

`unattributed_facts` counted only `agent_id = 'unknown'`, the literal string
migration 0003 backfilled. A fact written with no agent_id at all is a
different shape: Apache AGE drops a null-valued property at CREATE, so the key
is absent rather than null, and an equality test against 'unknown' never
matches it.

Thirty such facts sat in the author's own store while `echo-memory health`
reported "every fact has a recorded author" and the criterion 6 gate's
`unattributed == 0` condition passed. A blind spot in the tool built to do the
measuring is worse than no measurement: it reads as a clean bill of health.

`IS NULL` in AGE matches an absent key as well as a null value, so one clause
covers both shapes."""

from fake_embedder import REFERENCE, VectorEmbedder

from echo_memory import server
from echo_memory.infra.config import Config
from echo_memory.infra.db import GRAPH_NAME as GRAPH
from echo_memory.trial.check import unattributed_facts

FACT = "a fact whose writer was never recorded"

# A session that can evidence its own author needs TWO facts, and they have to
# differ: restating one word for word stores nothing, so two identical writes
# yield one edge rather than two.
SECOND = "a second fact of the same session, written with its author intact"


def _serve(migrated_db, agent_id="claude-code"):
    config = Config(
        user_id="ayush", agent_id=agent_id, database_url=migrated_db, project="echo-mem"
    )
    server.startup(config=config, embedder=VectorEmbedder(
        {"probe": REFERENCE, "other-probe": REFERENCE, FACT: REFERENCE, SECOND: REFERENCE}
    ))
    return config


def _write_fact(session_id="session-1", subject="probe", fact=FACT):
    """`subject` and `fact` vary together so one session can assert two
    distinct facts. Edge identity is (source, target, relation_type), so
    reusing the subject with new text would supersede the first fact instead
    of adding a second, and reusing both would restate it and store nothing."""
    result = server.write_episode(
        "shared", session_id,
        [{"name": subject, "type": "test"}],
        [{"source": subject, "target": subject, "relation_type": "is",
          "fact": fact, "confidence": "extracted"}],
        entity_resolutions={subject: {"resolved_to": "new"}},
    )
    return result["edges_created"][0]


def _set_agent(conn, edge_id, value):
    """`value=None` removes the key, reproducing what a pre-agent_id server
    wrote; a string sets it, reproducing the 0003 placeholder."""
    conn.execute("LOAD 'age'")
    conn.execute('SET search_path = ag_catalog, "$user", public')
    clause = "REMOVE e.agent_id" if value is None else f"SET e.agent_id = '{value}'"
    conn.execute(
        f"""SELECT * FROM cypher('{GRAPH}', $$
            MATCH ()-[e:FACT]->() WHERE id(e) = {int(edge_id)}
            {clause}
            RETURN id(e)
        $$) AS (n agtype)"""
    )


def test_a_fact_with_no_agent_id_key_is_counted(migrated_db):
    """The bug: absent is not the same as 'unknown', and only one was counted."""
    config = _serve(migrated_db)
    edge_id = _write_fact()
    with server._state.pool.connection() as conn:
        _set_agent(conn, edge_id, None)
        assert unattributed_facts(conn, [config.shared_group_id()]) == 1


def test_the_placeholder_is_still_counted(migrated_db):
    """Broadening the query must not lose the shape it already caught."""
    config = _serve(migrated_db)
    edge_id = _write_fact()
    with server._state.pool.connection() as conn:
        _set_agent(conn, edge_id, "unknown")
        assert unattributed_facts(conn, [config.shared_group_id()]) == 1


def test_an_attributed_fact_is_not_counted(migrated_db):
    """The other direction: a real author must never be called unattributed."""
    config = _serve(migrated_db)
    _write_fact()
    with server._state.pool.connection() as conn:
        assert unattributed_facts(conn, [config.shared_group_id()]) == 0


def test_health_does_not_claim_an_author_that_is_not_there(migrated_db):
    """What the user actually saw: a green line over a store with 30 unattributable
    facts."""
    from echo_memory.cli import health

    config = _serve(migrated_db)
    edge_id = _write_fact()
    with server._state.pool.connection() as conn:
        _set_agent(conn, edge_id, None)
        report = health.collect(conn, config)

    assert report["unattributed_facts"] == 1
    assert "every fact has a recorded author" not in health.render(report)


def test_a_missing_author_is_not_reported_as_a_tool_named_None(migrated_db):
    """str(None) is "None", and it was printed as though a tool were called
    that."""
    from echo_memory.cli import health

    config = _serve(migrated_db)
    edge_id = _write_fact()
    with server._state.pool.connection() as conn:
        _set_agent(conn, edge_id, None)
        report = health.collect(conn, config)

    assert "None" not in report["writers"], report["writers"]
    assert "unknown" in report["writers"]



def test_recovery_is_not_offered_when_nothing_can_be_recovered(migrated_db):
    """`reattribute --agent` recovers a session's authorless facts from another
    fact of the SAME session, and refuses when there is none. Pointing an
    operator at it regardless sends them to a correct no-op: in the author's own
    store all 26 unattributed facts are of that kind, and the advice was being
    printed for all 26."""
    from echo_memory.cli.trial import render_check
    from echo_memory.trial.check import build_report, recoverable_attributions

    config = _serve(migrated_db)
    edge_id = _write_fact(session_id="orphan-session")
    with server._state.pool.connection() as conn:
        _set_agent(conn, edge_id, None)
        group = config.group_id("shared")

        assert recoverable_attributions(conn, [group]) == 0, (
            "the only fact of that session is the authorless one"
        )
        text = render_check(build_report(conn, config))

    assert "none are recoverable" in text
    assert "reattribute --agent" not in text, (
        "offered a command that would correctly do nothing"
    )


def test_recovery_is_offered_when_the_session_can_evidence_it(migrated_db):
    """The other side: one attributed fact in the session is what makes the
    rest recoverable, and then the command is worth naming."""
    from echo_memory.cli.trial import render_check
    from echo_memory.trial.check import build_report, recoverable_attributions

    config = _serve(migrated_db)
    keeps_author = _write_fact(session_id="evidenced-session")
    loses_author = _write_fact(
        session_id="evidenced-session", subject="other-probe", fact=SECOND
    )
    with server._state.pool.connection() as conn:
        _set_agent(conn, loses_author, None)
        group = config.group_id("shared")

        assert keeps_author != loses_author
        assert recoverable_attributions(conn, [group]) == 1
        text = render_check(build_report(conn, config))

    assert "1 of them are recoverable" in text
    assert "reattribute --agent" in text
