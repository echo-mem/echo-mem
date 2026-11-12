"""`echo-memory trust` end to end, and the chain it was the missing link in.

Driven through `main(argv)` rather than by calling the subcommand functions,
because the thing that was broken was not the library. `trust/verify.record`
worked and had tests; what did not exist was any way for a person to call it,
so a test that calls it directly would have passed on the day the product
could not do this at all.

The last test in this file is the one that matters. Before this command
existed, nothing in any real deployment could put a fact into `verified` or
`evidence_backed`, and `lessons.candidates` filters on exactly those two - so
the whole of Capability A ended in an empty list that read as "your outcomes
disagree" and meant "no surface exists". `test_the_whole_chain_now_produces_a
_lesson` walks it: an event arrives, the drain applies it, an operator
verifies the outcome, and a lesson comes out.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from hash_embedder import HashEmbedder

from echo_memory import server
from echo_memory.cli.main import main
from echo_memory.events import envelope as events
from echo_memory.events import inbox
from echo_memory.infra.db import connect
from echo_memory.ingestion.write_episode import write_episode
from echo_memory.intelligence import lessons, outcomes
from echo_memory.trust import evidence, states, verify

USER = "trust-cli"
AGENT = "claude-code"
SCOPE = "shared"
GROUP = f"user:{USER}:shared"

DECISION = "move-the-queue-to-postgres"


@pytest.fixture
def cli(migrated_db, monkeypatch):
    """The environment `load_config` reads, so `main(argv)` resolves to the
    same scope the test writes into. Set on the process rather than passed,
    because that is how an operator's shell supplies it and the resolution
    path is part of what is under test."""
    monkeypatch.setenv("ECHO_MEMORY_USER_ID", USER)
    monkeypatch.setenv("ECHO_MEMORY_AGENT_ID", AGENT)
    monkeypatch.setenv("ECHO_MEMORY_DATABASE_URL", migrated_db)
    return migrated_db


def _embedder():
    """`HashEmbedder` rather than `VectorEmbedder`, for the reason that module
    states: the event path composes its own fact text out of the payload, so a
    strict embedder would mean restating `intelligence/outcomes.py`'s wording
    inside this test and a reworded sentence would fail as a KeyError from the
    embedder rather than as the assertion that cares."""
    return HashEmbedder()


def _seed_fact(migrated_db) -> str:
    """One live fact in GROUP, through the real write path, and its edge id.

    `write_episode` directly rather than `server.write_episode`, which would
    need `server.startup` and leave a Config, an embedder and an open
    connection on the module for every later test in the process. The conftest
    spells out what that costs: the teardown contends with connections the
    tests never close, and its failure then lands on whichever test ran next.
    The CLI under test opens its own connection from the environment anyway, so
    the server singleton was never part of this path.
    """
    conn = connect(migrated_db)
    written = write_episode(
        conn, GROUP, "sess-trust",
        [{"name": "Queue", "type": "component"}, {"name": "Postgres", "type": "tool"}],
        [{"source": "Queue", "target": "Postgres", "relation_type": "runs_on",
          "fact": "the queue runs on Postgres", "confidence": "extracted"}],
        # Answered up front so the write cannot come back asking which
        # existing node was meant. There is nobody to ask in a test, and a
        # deferral would read as a seed that silently stored nothing.
        {"Queue": {"resolved_to": "new"}, "Postgres": {"resolved_to": "new"}},
        _embedder(), project="acme", agent_id=AGENT,
    )
    created = written.get("edges_created") or []
    assert created, f"precondition: the seed episode stored nothing ({written})"
    return str(created[0])


def _envelope(event_type, event_id, **payload):
    return events.Envelope(
        event_id=event_id,
        event_type=event_type,
        occurred_at=datetime.now(UTC),
        actor=events.Actor(agent="ci-reporter", run_id="run-1"),
        scope=events.Scope(memory="default", scope=SCOPE),
        payload=payload,
    )


# --- the subcommands round trip -------------------------------------------


def test_show_reports_an_unchecked_fact_as_unverified(cli, capsys):
    fact = _seed_fact(cli)

    assert main(["trust", "show", "--scope", SCOPE, "--fact", fact]) == 0

    out = capsys.readouterr().out
    assert "the queue runs on Postgres" in out
    assert states.UNVERIFIED in out
    # The distinction states.py draws, surfaced where a person reads it.
    assert "advisory" in out
    assert "Nothing has ever checked this fact" in out


def test_verify_records_a_state_show_reads_back_and_list_finds_it(cli, capsys):
    fact = _seed_fact(cli)

    assert main([
        "trust", "verify", "--scope", SCOPE, "--fact", fact,
        "--verifier", "ayush", "--method", "ran the suite against prod",
        "--policy", "policy-1",
    ]) == 0
    assert "Recorded verified" in capsys.readouterr().out

    # The library's own derivation, not the CLI's rendering of it. If these
    # two ever disagree the command is lying to an operator.
    conn = connect(cli)
    assert verify.state_of(conn, GROUP, fact) == states.VERIFIED

    assert main(["trust", "show", "--scope", SCOPE, "--fact", fact]) == 0
    shown = capsys.readouterr().out
    assert "ran the suite against prod" in shown
    assert "ayush" in shown
    assert "policy-1" in shown
    assert "authoritative" in shown

    assert main(["trust", "list", "--scope", SCOPE]) == 0
    listed = capsys.readouterr().out
    assert fact in listed
    assert f"{states.VERIFIED}: 1" in listed


def test_list_filters_by_state_and_counts_every_state(cli, capsys):
    fact = _seed_fact(cli)
    main([
        "trust", "reject", "--scope", SCOPE, "--fact", fact,
        "--verifier", "ayush", "--method", "the queue never ran on Postgres",
    ])
    capsys.readouterr()

    assert main(["trust", "list", "--scope", SCOPE, "--state", states.VERIFIED]) == 0
    empty = capsys.readouterr().out
    # The summary is printed whatever the filter matched, so an operator can
    # tell "none in this state" from "nothing in this scope at all".
    assert "1 live fact(s)" in empty
    assert f"0 fact(s) in state {states.VERIFIED!r}" in empty

    assert main(["trust", "list", "--scope", SCOPE, "--state", states.REJECTED]) == 0
    assert fact in capsys.readouterr().out


def test_dispute_and_reject_are_recorded_and_the_history_keeps_both(cli, capsys):
    fact = _seed_fact(cli)

    assert main([
        "trust", "dispute", "--scope", SCOPE, "--fact", fact,
        "--verifier", "reviewer-a", "--method", "contradicted by the runbook",
    ]) == 0
    assert main([
        "trust", "reject", "--scope", SCOPE, "--fact", fact,
        "--verifier", "reviewer-b", "--method", "confirmed wrong",
    ]) == 0
    capsys.readouterr()

    assert main(["trust", "show", "--scope", SCOPE, "--fact", fact]) == 0
    out = capsys.readouterr().out
    # Append only: the dispute is still readable after the rejection, which is
    # the whole reason the table has no UPDATE path.
    assert "2 verification(s)" in out
    assert "reviewer-a" in out
    assert "reviewer-b" in out
    assert "removes the fact from an answer entirely" in out

    conn = connect(cli)
    assert verify.state_of(conn, GROUP, fact) == states.REJECTED


def test_evidence_is_recorded_linked_and_changes_no_state(cli, capsys):
    fact = _seed_fact(cli)

    assert main([
        "trust", "evidence", "--scope", SCOPE, "--fact", fact,
        "--kind", "test_run", "--uri", "https://ci.example/run/412",
        "--detail", '{"suite": "integration", "failures": 0}',
        "--by", "ci-reporter",
    ]) == 0
    out = capsys.readouterr().out
    assert "https://ci.example/run/412" in out

    conn = connect(cli)
    artefacts = evidence.for_fact(conn, GROUP, fact)
    assert len(artefacts) == 1
    assert artefacts[0]["detail"] == {"suite": "integration", "failures": 0}
    # THE separation. Attaching an artefact does not promote the claim, or a
    # reporter would admit its own outcomes into lesson support by citing its
    # own log.
    assert verify.state_of(conn, GROUP, fact) == states.UNVERIFIED
    assert "Evidence is not proof" in out


def test_an_expired_verification_reads_as_stale_and_the_row_is_left_alone(cli, capsys):
    fact = _seed_fact(cli)
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()

    assert main([
        "trust", "verify", "--scope", SCOPE, "--fact", fact,
        "--verifier", "ayush", "--method", "checked last month", "--expires", past,
    ]) == 0
    capsys.readouterr()

    assert main(["trust", "show", "--scope", SCOPE, "--fact", fact]) == 0
    out = capsys.readouterr().out
    assert f"state: {states.STALE}" in out
    # The row still says verified. A command that rewrote it would destroy the
    # evidence that the check ever passed.
    assert "a lapsed check is not a" in out.lower()
    conn = connect(cli)
    stored = conn.execute(
        "SELECT state FROM public.fact_verification WHERE group_id = %s", (GROUP,)
    ).fetchone()
    assert stored[0] == states.VERIFIED


# --- the refusals ----------------------------------------------------------


def test_verify_with_an_empty_verifier_is_refused(cli, capsys):
    """A verification with nobody against it would still count towards a
    lesson, so it is worth less than nothing. argparse's required=True is
    satisfied by an empty string, which is why this is checked in code."""
    fact = _seed_fact(cli)

    code = main([
        "trust", "verify", "--scope", SCOPE, "--fact", fact,
        "--verifier", "   ", "--method", "whatever",
    ])

    assert code == 2
    assert "--verifier must name who" in capsys.readouterr().out
    conn = connect(cli)
    assert verify.state_of(conn, GROUP, fact) == states.UNVERIFIED
    # Nothing was written at all, not merely nothing that counts.
    assert conn.execute(
        "SELECT count(*) FROM public.fact_verification WHERE group_id = %s", (GROUP,)
    ).fetchone()[0] == 0


def test_reject_with_an_empty_verifier_is_refused(cli, capsys):
    """The same gate on the state that SUPPRESSES a fact. Checked separately
    rather than parametrised with verify, because the three recorders share
    one implementation and this is the assertion that says so."""
    fact = _seed_fact(cli)

    code = main([
        "trust", "reject", "--scope", SCOPE, "--fact", fact,
        "--verifier", "", "--method", "whatever",
    ])

    assert code == 2
    assert "--verifier must name who" in capsys.readouterr().out
    conn = connect(cli)
    assert verify.state_of(conn, GROUP, fact) == states.UNVERIFIED


def test_an_empty_method_is_refused(cli, capsys):
    fact = _seed_fact(cli)

    code = main([
        "trust", "verify", "--scope", SCOPE, "--fact", fact,
        "--verifier", "ayush", "--method", "  ",
    ])

    assert code == 2
    assert "--method must say how" in capsys.readouterr().out


def test_a_naive_expiry_is_refused(cli, capsys):
    """Expiry is compared against the DATABASE's clock, so an operator in
    another zone typing a bare timestamp would set it hours out. Twenty four
    milliseconds of container skew has already turned three days into four
    here."""
    fact = _seed_fact(cli)

    code = main([
        "trust", "verify", "--scope", SCOPE, "--fact", fact,
        "--verifier", "ayush", "--method", "checked", "--expires", "2027-01-01T00:00",
    ])

    assert code == 2
    assert "timezone offset" in capsys.readouterr().out


def test_expiry_on_a_rejection_is_refused(cli, capsys):
    """`expires_at` is only ever read for a `verified` row. Accepting it here
    would let an operator believe a suppression lapses on a timer, which is a
    different decision from the one this command makes."""
    fact = _seed_fact(cli)
    future = (datetime.now(UTC) + timedelta(days=30)).isoformat()

    code = main([
        "trust", "reject", "--scope", SCOPE, "--fact", fact,
        "--verifier", "ayush", "--method", "wrong", "--expires", future,
    ])

    assert code == 2
    assert "applies only to a verification" in capsys.readouterr().out


def test_a_fact_in_another_scope_is_not_reachable_by_id(cli, capsys):
    """An edge id is global to the graph. Scoping the lookup by group_id as
    well is what stops a pasted number printing somebody else's fact."""
    fact = _seed_fact(cli)

    assert main(["trust", "show", "--scope", "solo", "--fact", fact]) == 1
    assert "No live fact" in capsys.readouterr().out


def test_nothing_this_command_does_is_reachable_over_mcp(cli):
    """The guard test in test_erasure.py reads the live registry; this is the
    same assertion with the names this file introduces, kept here so the two
    fail in the place a reader is looking."""
    published = {t.name for t in server.server._tool_manager.list_tools()}
    assert published, "precondition: the server publishes some tools"
    assert not (published & {
        "trust", "trust_verify", "trust_reject", "trust_dispute",
        "trust_evidence", "trust_lessons", "record_evidence", "link_evidence",
    })


# --- the diagnosis an empty answer needs ----------------------------------


def test_lessons_on_unverified_outcomes_says_why_it_is_empty(cli, capsys):
    """The failure this whole stream exists to fix. Outcomes are reported,
    nothing can be checked, `candidates` returns [] and the empty list reads
    as "your outcomes disagree"."""
    conn = connect(cli)
    embedder = _embedder()
    inbox.enqueue(conn, GROUP, _envelope(
        events.DECISION_RECORDED, "d-why-001", decision=DECISION))
    for i, subject in enumerate(("latency", "throughput")):
        inbox.enqueue(conn, GROUP, _envelope(
            events.OUTCOME_OBSERVED, f"o-why-{i:03d}", decision=DECISION,
            outcome=subject, sign="success"))
    inbox.drain(conn, apply=lambda c, g, env: outcomes.apply(c, g, env, embedder))
    assert lessons.candidates(conn, GROUP) == []

    assert main(["trust", "lessons", "--scope", SCOPE]) == 0

    out = capsys.readouterr().out
    assert "No lessons" in out
    # Not an empty list. The three bars, with this scope's number against each.
    assert "1 decision node(s)" in out
    assert "2 live outcome observation(s)" in out
    assert "0 in a supporting state" in out
    # And the action, naming the command that fixes it.
    assert "echo-memory trust verify" in out
    assert "nothing is reporting" in out


def test_lessons_distinguishes_a_scope_with_no_decisions_at_all(cli, capsys):
    """Two empty answers with different owners. Nothing has recorded a
    decision is not the same problem as nothing has verified one, and an
    operator pointed at the wrong one goes looking in the wrong place."""
    _seed_fact(cli)

    assert main(["trust", "lessons", "--scope", SCOPE]) == 0

    out = capsys.readouterr().out
    assert "0 decision node(s)" in out
    assert "Nothing has recorded a decision in this scope" in out


def test_lessons_reports_evidence_arriving_with_nobody_adjudicating_it(cli, capsys):
    """The other half of the pair `lessons.py` logs. Artefacts attached and
    no verification recorded is a different problem from silence, because
    `evidence_backed` is never derived from evidence existing."""
    conn = connect(cli)
    embedder = _embedder()
    inbox.enqueue(conn, GROUP, _envelope(
        events.DECISION_RECORDED, "d-adj-001", decision=DECISION))
    inbox.enqueue(conn, GROUP, _envelope(
        events.OUTCOME_OBSERVED, "o-adj-001", decision=DECISION,
        outcome="latency", sign="success"))
    inbox.drain(conn, apply=lambda c, g, env: outcomes.apply(c, g, env, embedder))
    outcome_edge = conn.execute(
        "SELECT applied_edge_ids FROM public.event_inbox WHERE event_id = %s",
        ("o-adj-001",),
    ).fetchone()[0][0]

    assert main([
        "trust", "evidence", "--scope", SCOPE, "--fact", str(outcome_edge),
        "--kind", "deployment", "--uri", "https://ci.example/deploy/9",
    ]) == 0
    capsys.readouterr()

    assert main(["trust", "lessons", "--scope", SCOPE]) == 0
    out = capsys.readouterr().out
    assert "1 evidence row(s)" in out
    assert "nobody is" in out and "adjudicating" in out


# --- the chain that was impossible ----------------------------------------


def test_the_whole_chain_now_produces_a_lesson(cli, capsys):
    """Machine event, inbox, drain, outcome in the graph, operator
    verification through the CLI, lesson.

    Every link existed before this stream except the second to last, and
    without it the chain ended in an empty list in every real deployment. The
    assertions walk it in order so a break names which link went.
    """
    conn = connect(cli)
    embedder = _embedder()

    # 1. A machine reports a decision and two outcomes of it.
    inbox.enqueue(conn, GROUP, _envelope(
        events.DECISION_RECORDED, "chain-decision-001", decision=DECISION))
    for i, subject in enumerate(("queue-depth", "worker-restarts")):
        accepted = inbox.enqueue(conn, GROUP, _envelope(
            events.OUTCOME_OBSERVED, f"chain-outcome-{i:03d}", decision=DECISION,
            outcome=subject, sign="success"))
        assert accepted["accepted"] is True

    # 2. The single writer applies them.
    drained = inbox.drain(
        conn, apply=lambda c, g, env: outcomes.apply(c, g, env, embedder))
    assert drained["rejected"] == []
    assert len(drained["applied"]) == 3

    # 3. The outcomes are edges in the graph the lesson layer can see.
    edges = [
        str(e)
        for (applied,) in conn.execute(
            """SELECT applied_edge_ids FROM public.event_inbox
                WHERE event_id LIKE 'chain-outcome-%%' AND state = 'applied'
                ORDER BY event_id""").fetchall()
        for e in (applied or [])
    ]
    assert len(edges) == 2

    # 4. Nothing has checked them, so there is no lesson yet. This is the
    #    state every deployment was permanently in.
    assert lessons.candidates(conn, GROUP) == []

    # 5. A person verifies each, through the surface that did not exist.
    for edge in edges:
        assert main([
            "trust", "verify", "--scope", SCOPE, "--fact", edge,
            "--verifier", "ayush", "--method", "read the dashboards for a week",
        ]) == 0
    capsys.readouterr()

    # 6. A lesson, derived at read time from what was just recorded.
    produced = lessons.candidates(conn, GROUP)
    assert len(produced) == 1
    assert produced[0]["decision"] == DECISION
    assert produced[0]["support"] == 2
    assert produced[0]["sign"] == "success"
    # Every supporter is `verified`, so the strongest basis is earned rather
    # than reached by majority.
    assert produced[0]["basis"] == "verified_causal"

    # 7. And the operator can see it, which is the other half of the gap.
    assert main(["trust", "lessons", "--scope", SCOPE]) == 0
    out = capsys.readouterr().out
    assert "1 lesson(s)" in out
    assert DECISION in out
    assert "verified_causal" in out
