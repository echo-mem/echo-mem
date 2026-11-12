"""Three canonicalisers, which must produce the same bytes.

`attestation.canonical`, `trust/vattest.canonical` and
`exchange/bundle.canonical` are three copies of one line, each with its own
docstring giving the same reason: a digest has to be reproducible by a
different version of this code on a different machine. The three are
byte-identical today - `sort_keys=True`, `separators=(",", ":")`,
`ensure_ascii=False` - and nothing checks that.

They are deliberately NOT collapsed here, and the reason is why this file is a
test instead of a refactor. Each one feeds a digest that is already recorded:
attestation's over every fact in every store including customers', vattest's
over every verification, and bundle's over bytes that have left the building
for another organisation. Migration 0036 spells out what moving one costs - the
moment a covered value serialises differently, the chain reports the whole
scope as altered, which is an integrity event rather than a cleanup. A pure
extraction would be safe; proving it is safe is this assertion, and it is worth
having whether or not anybody takes the extraction.

What this catches: a separator, a sort flag or an `ensure_ascii` changed in one
of the three. That drift is invisible until two digests over the same claim
disagree, at which point the question "was this tampered with" has no answer.

What it does not catch: the digest functions themselves, which legitimately
differ. `attestation.digest` reads its key from the environment,
`vattest.digest` takes the material as an argument so a key can be rotated per
link, and `bundle.digest_of` chains nothing because a bundle is one object.
Those are three designs, not three copies.
"""

from __future__ import annotations

import pytest

from echo_memory import attestation
from echo_memory.exchange import bundle
from echo_memory.trust import vattest

CANONICALISERS = {
    "attestation": attestation.canonical,
    "vattest": vattest.canonical,
    "bundle": bundle.canonical,
}

#: Inputs chosen for the ways a serialiser can disagree with another, not for
#: realism. Key order, a non-ASCII claim, an escape, a float, a null, and the
#: empty case.
CASES = [
    {},
    {"a": 1, "b": 2},
    # Insertion order deliberately not sorted order, which is what sort_keys
    # exists for and the only thing that makes two writers agree.
    {"z": "last", "a": "first", "m": "middle"},
    {"fact": "we ship from the release branch"},
    # A non-ASCII claim: ensure_ascii=False makes this one UTF-8 string, and
    # ensure_ascii=True makes it a pile of \\u escapes. Both are valid JSON and
    # they are different bytes.
    {"fact": "Razorpay charges ₹149, not €149"},
    # An embedded quote and a newline, which is the shape the agtype decoder
    # bug was about and the shape a separator change would mangle.
    {"fact": '"ship it"\nwas the whole review'},
    {"confidence": "extracted", "t_valid": 1762800000, "causal_hint": None},
    {"nested": {"b": [1, 2, {"d": 4, "c": 3}], "a": None}},
]


@pytest.mark.parametrize("claims", CASES, ids=range(len(CASES)))
def test_the_three_canonicalisers_agree_byte_for_byte(claims):
    produced = {name: fn(claims) for name, fn in CANONICALISERS.items()}
    distinct = set(produced.values())
    assert len(distinct) == 1, (
        f"three canonicalisers disagree on {claims!r}: {produced}. A digest "
        f"over one of these is already recorded in every store; see this "
        f"file's docstring and migration 0036."
    )


def test_each_one_returns_bytes_rather_than_a_string():
    """A str would still compare equal between the three and would hash
    differently once encoded by whoever got there first."""
    for name, fn in CANONICALISERS.items():
        assert isinstance(fn({"a": 1}), bytes), name


def test_the_bytes_are_the_form_the_recorded_digests_were_taken_over():
    """Pinned as a literal, because "all three agree" is satisfied by all three
    being wrong together. This is the one assertion that would fail if every
    copy were changed in the same commit, which is exactly how a cleanup would
    change them.
    """
    expected = b'{"a":"\xe2\x82\xb91","b":null}'
    for name, fn in CANONICALISERS.items():
        assert fn({"b": None, "a": "₹1"}) == expected, name
