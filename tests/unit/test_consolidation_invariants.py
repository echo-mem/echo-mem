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


@pytest.mark.skip(
    reason="No consolidator exists. Delete this skip when one does; these are "
           "its acceptance criteria, not aspirations."
)
@pytest.mark.parametrize("fixture", _loaded()["fixtures"], ids=lambda f: f["id"])
def test_consolidation_preserves_decision_relevant_semantics(fixture):
    """The shape the implementation has to satisfy.

    Deliberately not written against an imagined API. Whoever builds
    consolidation decides what the validator is called and what it returns;
    what is fixed here is that each of these inputs produces the stated verdict
    for the stated reason.

    One of them, unsupported-strengthening, cannot be settled deterministically
    against the current schema at all. The confidence enum records how a fact
    was obtained rather than the modality of its claim, and the hedge lives in
    the prose. That fixture is the concrete argument for either an independent
    semantic evaluator or a write contract that carries modality as a field.
    """
    raise NotImplementedError(fixture["id"])
