"""Acceptance criteria for consolidation, written before consolidation exists.

The README claimed until 2026-09-21 that memory "consolidates into higher level
summaries over time". It does not, and a reader believed it and designed a
validation harness for a system that was not there. The feature is designed and
unbuilt, and this file is what it has to satisfy on the day somebody builds it.

The objection these encode is that traceability is not preservation. The design
says "consolidation demotes; it never deletes", with an edge from every summary
back to the facts it came from. That establishes where a summary came from. It
does not establish that the summary still says what those facts said, and
retrieval metrics cannot tell the difference, because the right node did come
back.

Contributed by Jayasurya Mahadevan by email, 2026-09-21.

Two of these tests run today and guard the fixtures themselves. The rest skip
until there is a consolidator to point them at: a suite that cannot fail is
worth nothing, so the schema is checked now rather than discovered rotten later.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from echo_memory.consolidation import invariants

FIXTURES = (
    pathlib.Path(__file__).parent.parent / "fixtures" / "consolidation_invariants.json"
)

VERDICTS = {"accept", "reject"}


def _loaded() -> dict:
    return json.loads(FIXTURES.read_text())


# ------------------------------------------------------- guards that run now


def test_every_fixture_names_the_invariant_it_tests():
    """A fixture whose expected verdict is not attributable to one named rule
    is an opinion. When it fails later, nobody can tell whether the
    consolidator is wrong or the fixture was."""
    data = _loaded()
    known = set(data["invariants"])

    for fixture in data["fixtures"]:
        assert fixture["verdict"] in VERDICTS, fixture["id"]
        assert fixture["invariant"] in known, f"{fixture['id']} names an unknown invariant"
        assert fixture["sources"], fixture["id"]
        assert fixture["candidate"].strip(), fixture["id"]
        assert len(fixture["why"]) > 40, f"{fixture['id']} does not say why"


def test_the_suite_cannot_be_passed_by_rejecting_everything():
    """Without a control, a consolidator that refuses to consolidate scores
    perfectly. That is the failure mode of every safety harness that only
    contains violations."""
    verdicts = {f["verdict"] for f in _loaded()["fixtures"]}

    assert "accept" in verdicts, "no control fixture: refusing all output would pass"
    assert "reject" in verdicts


# ------------------------------------------- the contract, until it can run


@pytest.mark.parametrize("fixture", _loaded()["fixtures"], ids=lambda f: f["id"])
def test_consolidation_preserves_decision_relevant_semantics(fixture):
    """The acceptance criteria, now running.

    Deliberately not written against an imagined API: whoever built the
    validator chose what it is called and what it returns, and what was fixed
    in advance is that each of these inputs produces the stated verdict for the
    stated reason. The `invariant` assertion is the half that matters. A
    checker that rejected everything would get all four verdicts right and name
    the wrong rule for three of them.

    One note the fixture file makes has turned out to be half right.
    `unsupported-strengthening` genuinely cannot be settled from the schema:
    `confidence` records how a fact was obtained, not the modality of its
    claim. It can be settled from the prose, where the hedge actually lives, by
    the same marker check that handles conditions. That is weaker than a
    semantic evaluator and stronger than nothing, and it needs no model and no
    schema change.
    """
    verdict = invariants.check(
        fixture["sources"], fixture["candidate"], as_of=fixture.get("as_of")
    )

    assert verdict.accepted == (fixture["verdict"] == "accept"), (
        f"{fixture['id']}: expected {fixture['verdict']}, got "
        f"{'accept' if verdict.accepted else 'reject'} because {verdict.why!r}. "
        f"The fixture's own reasoning: {fixture['why']}"
    )
    if fixture["verdict"] == "reject":
        assert verdict.invariant == fixture["invariant"], (
            f"{fixture['id']}: rejected for the wrong reason. Expected "
            f"{fixture['invariant']!r}, got {verdict.invariant!r} ({verdict.why}). "
            f"Rejecting for the wrong reason is how a checker passes this "
            f"suite without implementing it."
        )


# ------------------------------------------- controls the fixtures cannot be

# Every fixture above is a single input with a single expected verdict, so a
# checker tuned to them is indistinguishable from one that works. These are the
# other direction of each rule: the same shape of input, kept faithful, which
# must be ACCEPTED. Without them the suite is passed by rejecting more.


def test_a_condition_that_survives_is_accepted():
    """no_unconditionalization rejects a condition that was DROPPED. A summary
    that keeps it, in fewer words, is the thing consolidation is for."""
    sources = [{
        "source": "supplier A", "target": "procurement policy",
        "relation_type": "selected_by",
        "fact": "Use supplier A unless lead time exceeds 10 days; then use supplier B.",
        "confidence": "extracted",
    }]
    verdict = invariants.check(sources, "Supplier A, unless lead time is over 10 days.")
    assert verdict.accepted, verdict.why


def test_a_hedge_that_survives_is_accepted():
    sources = [{
        "source": "outage", "target": "expired credential",
        "relation_type": "possibly_caused_by",
        "fact": "The outage may have been caused by an expired credential.",
        "confidence": "inferred",
    }]
    verdict = invariants.check(
        sources, "The outage was possibly caused by an expired credential."
    )
    assert verdict.accepted, verdict.why


def test_the_passive_voice_is_not_a_reversal():
    """"A caused B" and "B was caused by A" are one claim written from opposite
    ends. trace_cause already relies on that equivalence, so a checker that
    called the passive form a reversal would reject correct summaries of every
    causal chain in the store."""
    sources = [{
        "source": "queue saturation", "target": "request latency",
        "relation_type": "caused",
        "fact": "Queue saturation caused request latency to increase.",
        "confidence": "extracted",
    }]
    assert invariants.check(
        sources, "Request latency rose because of queue saturation."
    ).accepted
    assert invariants.check(
        sources, "Queue saturation led to higher request latency."
    ).accepted


def test_a_fact_still_live_at_as_of_may_be_asserted():
    """The other side of supersession_respected: the CURRENT fact is exactly
    what a summary as of that moment should say."""
    sources = [
        {"source": "primary database", "target": "db-a", "relation_type": "is",
         "fact": "Primary database is db-a.", "confidence": "extracted",
         "valid_from": "t1", "valid_until": "t2"},
        {"source": "primary database", "target": "db-b", "relation_type": "is",
         "fact": "Primary database moved to db-b.", "confidence": "extracted",
         "valid_from": "t2"},
    ]
    assert invariants.check(sources, "Primary database is db-b.", as_of="t2").accepted


def test_the_same_summary_is_fine_before_it_was_superseded():
    """as_of is honoured rather than 'latest wins'. Read as of t1, db-a is not
    a retired fact, it is the answer."""
    sources = [
        {"source": "primary database", "target": "db-a", "relation_type": "is",
         "fact": "Primary database is db-a.", "confidence": "extracted",
         "valid_from": "t1", "valid_until": "t2"},
    ]
    assert invariants.check(sources, "Primary database is db-a.", as_of="t1").accepted


def test_a_hedge_word_inside_another_word_is_not_a_hedge():
    """`may` is inside `maybe` and `dismay`; `if` is inside `verify` and
    `specific`. A substring test finds a hedge in most English sentences and
    the check then accepts everything."""
    sources = [{
        "source": "deploy", "target": "verification step", "relation_type": "uses",
        "fact": "The deploy runs a verification step that notifies the specific owner.",
        "confidence": "extracted",
    }]
    assert invariants.check(sources, "The deploy verifies and notifies the owner.").accepted


def test_an_empty_candidate_is_never_a_faithful_summary():
    assert not invariants.check(
        [{"source": "a", "target": "b", "relation_type": "uses", "fact": "a uses b"}],
        "",
    ).accepted
