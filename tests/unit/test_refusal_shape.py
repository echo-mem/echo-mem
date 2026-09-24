"""A refusal has to look like an answer, or nobody can check it.

write_episode refuses in two ways that neither raise nor return None: a fact
over the length limit, and a fact naming a source or target that is not in
the episode's own entities. Both used to come back as `{"error": ...}` and
nothing else.

That shape defeats the only reliable success check. A caller cannot ask
`len(result["edges_created"])` on a response with no such key, so the checks
people actually write are "did it return" and "did it throw" - and a refusal
does neither.

It cost a customer 19,000 facts. They bulk loaded 26,633, counted every
non-null response as stored, and ended with 1,650: validation refused the
rest, and because validation runs before anything touches the graph, those
refusals left no audit entry either. From the outside the writes evaporated.
"""

from __future__ import annotations

import pytest

from echo_memory.ingestion.write_episode import _refused

ANSWER_KEYS = {"edges_created", "superseded", "ambiguous_entities"}


def test_a_refusal_carries_the_same_keys_as_a_successful_write():
    """So `edges_created` is answerable on every path, and one check works
    whether the episode was refused, deferred, or simply changed nothing."""
    refusal = _refused("fact text too long")

    assert ANSWER_KEYS <= set(refusal), (
        f"a caller cannot check edges_created on {sorted(refusal)}"
    )
    assert refusal["error"], "a refusal must still say why"


def test_a_refusal_reports_nothing_stored():
    refusal = _refused("fact source 'Someone Else' not in entities")

    assert refusal["edges_created"] == []
    assert refusal["superseded"] == []
    assert refusal["ambiguous_entities"] == []


@pytest.mark.parametrize("reason", [
    "fact text too long",
    "fact source 'X' not in entities",
    "refusing to write a fact with no agent_id",
])
def test_the_one_success_check_is_correct_for_every_refusal(reason):
    """The check the docs tell people to write, against each refusal that
    exists. `not result["edges_created"]` must be true for all of them."""
    assert not _refused(reason)["edges_created"]
