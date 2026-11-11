"""`CHARS_PER_TOKEN` is defined three times and the prose calls it one.

Until this test landed, `eval/retrieval.py` printed "A token is 4 characters,
the constant the read accounting uses" at the bottom of every eval table an
operator reads. It was not that constant; it was a third copy of the same
number. `trial/reads.py` has the one the read accounting actually uses and
`retrieval/routing.py` has the one the token budget uses, and all three are an
independent `= 4`. The prose now says "the same number", which is true, and
this is what keeps it true.

All three being 4 means no figure printed anywhere is currently wrong, which
is exactly why this needs a test rather than a reader. The consequence of drift
is quiet and specific: `routing.fit_to_budget` reports `estimated_tokens` to a
caller, `eval/retrieval.py` reports a context saving against injecting the
whole scope, and `trial/reads.py` reports `injected_tokens` per recall. Those
three numbers are compared with each other, in BENCHMARKS.md and in the trial
criteria, and two of them measured in different tokens would be a comparison
nobody could see was invalid.

Not collapsed into one definition here, because the honest home for it is a
decision rather than an edit. `trial/reads.py` is the oldest and is what the
prose names, but importing a constant from `trial/` into `retrieval/` inverts
the layering, and `eval/` importing from either is fine while the reverse is
not. That is a small design call and it belongs with whoever owns the layering.
"""

from __future__ import annotations

from echo_memory.eval import retrieval as eval_retrieval
from echo_memory.retrieval import routing
from echo_memory.trial import reads

#: Module -> the constant, by the name each one gives it. Listed explicitly so
#: that a fourth copy is a deliberate addition to this list rather than
#: something a loop over the package would quietly absorb.
COPIES = {
    "trial/reads.py": reads.CHARS_PER_TOKEN,
    "retrieval/routing.py": routing.CHARS_PER_TOKEN,
    "eval/retrieval.py": eval_retrieval.CHARS_PER_TOKEN,
}


def test_every_copy_of_the_token_constant_is_the_same_number():
    assert len(set(COPIES.values())) == 1, (
        f"the three copies of CHARS_PER_TOKEN disagree: {COPIES}. Every token "
        f"figure in BENCHMARKS.md and in the trial criteria is compared "
        f"against the others; two of them in different tokens is a comparison "
        f"nobody can see is invalid."
    )


def test_the_number_is_still_four():
    """Separate from agreement, because all three can move together. Four is
    what every published token figure was taken at, so changing it reprices
    every one of them rather than improving an estimate.
    """
    assert set(COPIES.values()) == {4}
