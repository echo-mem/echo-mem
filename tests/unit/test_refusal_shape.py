"""A write that stored nothing has to say so, on every path.

write_episode can end with an empty `edges_created` in three different ways,
and from a caller's side they are indistinguishable without knowing which
other key to inspect:

  refused    validation rejected it
  deferred   an entity mention was similar enough to an existing node to be
             worth asking about and not similar enough to merge
  unchanged  every fact already exists, unaltered

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


def test_an_episode_that_changes_nothing_says_so():
    out = _blank()

    assert out["stored"] is False
    assert out["not_stored"] == "unchanged"


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
