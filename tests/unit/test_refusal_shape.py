"""A write that stored nothing has to say so, on every path.

write_episode can end with an empty `edges_created` in three different ways,
and from a caller's side they are indistinguishable without knowing which
other key to inspect:

  refused    validation rejected it
  deferred   an entity mention was similar enough to an existing node to be
             worth asking about and not similar enough to merge
  unchanged  every fact already exists word for word
  no_facts   the episode asserted none

The last two were one case until 2026-10-21, and conflating them meant the
message "every fact in this episode already exists, unaltered" could only
fire for an episode that asserted no facts, where it was false.

The deferred case is the one that hurts at scale and looks most like success:
a well formed response, no error, `isError` false, and nothing written. A
customer loading 26,633 facts read that as 26,633 successes and stored 1,650.

These pin that every one of those paths reports `stored: False` and names
which it was.
"""

from __future__ import annotations

import pytest

from echo_memory.ingestion.write_episode import _not_stored, _refused

ANSWER_KEYS = {"edges_created", "superseded", "ambiguous_entities"}


def _blank(**over):
    base = {"edges_created": [], "superseded": [], "ambiguous_entities": []}
    return _not_stored({**base, **over})


def test_a_refusal_carries_the_same_keys_as_a_successful_write():
    """So `edges_created` is answerable on every path, and one check works
    whether the episode was refused, deferred, or changed nothing."""
    refusal = _refused("fact text too long")

    assert ANSWER_KEYS <= set(refusal), (
        f"a caller cannot check edges_created on {sorted(refusal)}"
    )
    assert refusal["error"], "a refusal must still say why"


@pytest.mark.parametrize("reason", [
    "fact text too long",
    "fact source 'X' not in entities",
    "refusing to write a fact with no agent_id",
])
def test_every_refusal_reports_that_it_stored_nothing(reason):
    out = _refused(reason)

    assert out["stored"] is False
    assert out["not_stored"] == "refused"
    assert out["edges_created"] == []


def test_a_deferred_episode_says_it_was_deferred_and_why():
    """This is the path that cost the 19,000 facts. It returns no error at
    all, so a caller checking for one sees a clean success."""
    out = _blank(ambiguous_entities=[{"mention": "Khayam Khan"}])

    assert out["stored"] is False
    assert out["not_stored"] == "deferred"
    assert "Khayam Khan" in out["not_stored_detail"]
    assert "entity_resolutions" in out["not_stored_detail"], (
        "it did not say how to resolve the deferral"
    )
    assert "error" not in out, "a deferral is not an error and must not claim to be"


def test_an_episode_whose_facts_all_already_exist_says_unchanged():
    """And says it is a success, because a caller that reads "not stored" and
    retries will get the same answer forever."""
    out = _blank(restated=["1125899906843001"])

    assert out["stored"] is False
    assert out["not_stored"] == "unchanged"
    assert "already exist" in out["not_stored_detail"]


def test_an_episode_that_asserted_no_facts_is_not_called_unchanged():
    """Nodes may well have been created. Before these were separated, the
    only case that could reach "unchanged" was this one, where the message
    was wrong."""
    out = _blank()

    assert out["stored"] is False
    assert out["not_stored"] == "no_facts"
    assert "registered" in out["not_stored_detail"]


def test_a_partly_deferred_write_says_it_was_partial():
    """Nine of ten facts dropped used to look exactly like ten of ten
    written. That is how 680 LoCoMo turns became 336 facts with the ingest
    reporting success throughout."""
    out = _blank(
        edges_created=["1125899906843001"],
        ambiguous_entities=[{"mention": "John", "candidates": []}],
    )

    assert out["stored"] is True, "something really was written"
    assert out["partial"] is True
    assert "held on ambiguous entities" in out["partial_detail"]


def test_a_clean_write_is_not_marked_partial():
    out = _blank(edges_created=["1125899906843001"])

    assert out["stored"] is True
    assert "partial" not in out


def test_a_real_write_is_marked_stored():
    assert _blank(edges_created=["1125899906843001"])["stored"] is True


def test_a_supersession_counts_as_stored():
    """Replacing a fact changed the graph, even though nothing was created
    under a new id. Reporting it as not stored would send a caller retrying a
    write that worked."""
    assert _blank(superseded=["1125899906843002"])["stored"] is True


def test_the_one_check_a_caller_writes_is_correct_everywhere():
    """`stored` is the single field a bulk writer can branch on, across all
    four outcomes, without knowing which key to inspect for which case."""
    cases = [
        _refused("fact text too long"),
        _blank(ambiguous_entities=[{"mention": "Khayam Khan"}]),
        _blank(),
        _blank(edges_created=["1"]),
    ]

    assert [c["stored"] for c in cases] == [False, False, False, True]


def test_an_outage_is_marked_retryable_and_a_validation_error_is_not():
    """The two wrong guesses a caller makes without this are opposite:
    hammering a validation error forever, or dropping data on a database
    blip."""
    validation = _refused("fact text too long")
    outage = _refused("memory database unavailable", kind="unavailable")

    assert validation["not_stored"] == "refused"
    assert validation["retryable"] is False
    assert outage["not_stored"] == "unavailable"
    assert outage["retryable"] is True
    assert "worth retrying" in outage["not_stored_detail"]


def test_the_kind_is_never_leaked_to_the_caller():
    """It is how _refused talks to _not_stored, not part of the contract."""
    assert "_kind" not in _refused("x")
    assert "_kind" not in _refused("x", kind="unavailable")


def test_every_refusal_still_answers_edges_created():
    """The property the whole shape exists for: one check works on every
    path."""
    for kind in ("refused", "unavailable"):
        out = _refused("something", kind=kind)
        assert ANSWER_KEYS <= set(out), sorted(out)
        assert out["edges_created"] == []
        assert out["stored"] is False
