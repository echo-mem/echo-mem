"""`why` must not say a fact has no history when it has one, one scope over.

`recall` answers from both scopes and tags each fact with the one it came
from. `why` takes the global --scope, which defaults to solo. So the obvious
flow hands back an id the next command cannot resolve, and the old answer,
"No audit history found", asserts the fact has no provenance.
"""

from __future__ import annotations

import pytest

from echo_memory.cli.why import render_history

ENTRY = {
    "timestamp": "2026-10-26T13:27:15+00:00",
    "summary": "created: billing moved from Stripe to Razorpay",
}


def test_a_real_history_is_rendered():
    out = render_history("1125899906842625", [ENTRY])

    assert "History for fact 1125899906842625:" in out
    assert "billing moved from Stripe to Razorpay" in out


def test_a_fact_in_the_other_scope_names_that_scope():
    out = render_history("1125899906842625", [], found_in="shared")

    assert "is in the shared scope" in out
    assert "No audit history" not in out, (
        "it asserted the fact has no provenance, which is the bug"
    )


def test_it_gives_the_command_not_just_the_scope():
    """--scope is a global option and must precede the subcommand, so
    `echo-memory why <id> --scope shared` is rejected with "unrecognized
    arguments". Naming the flag without the order is a message somebody still
    gets wrong."""
    out = render_history("1125899906842625", [], found_in="shared")

    assert "echo-memory --scope shared why 1125899906842625" in out


@pytest.mark.parametrize("other", ["shared", "solo"])
def test_it_works_in_both_directions(other):
    assert f"is in the {other} scope" in render_history("x", [], found_in=other)


def test_a_fact_in_neither_scope_still_says_so_plainly():
    """The honest empty case has to survive. A fact that genuinely has no
    history must not be reported as living somewhere else."""
    out = render_history("1125899906842625", [], found_in=None)

    assert "No audit history found" in out
    assert "scope" not in out


def test_entries_win_over_a_stray_hint():
    """If the caller passes both, the history is the answer. Defensive, because
    the two arguments are computed in different branches of the caller."""
    out = render_history("x", [ENTRY], found_in="shared")

    assert "History for fact x:" in out
    assert "is in the shared scope" not in out
