"""The trust vocabulary, and the things about it that no database can check.

Three kinds of test here, and the middle one is the reason this file exists.

The states are a vocabulary spelled in three places: `trust/states.py`, the
CHECK constraint in migration 0032, and every caller. Two of those three can
drift silently - a state added in Python is accepted by nothing, a state added
to the CHECK is understood by nothing - and the failure is a write that is
refused in production and nowhere else, or a state read back that retrieval
treats as weight 0.0. So the agreement is pinned by parsing the migration
rather than by asserting that a few names appear in it, because a guard that
checks for presence cannot see a value the migration allows and this code has
never heard of.

The rest is the key handling in `vattest`, which has no database in it at all
and is where rotation either works or quietly signs with the wrong key.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from echo_memory.trust import states, vattest

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "src/echo_memory/migrations/versions/0032_fact_verification.py"


def _check_values(constraint: str) -> set[str]:
    """The literals one CHECK constraint in migration 0032 allows.

    Parsed out of the DDL rather than taken on trust. Set equality in both
    directions is the point: a state this code does not know about, allowed by
    the database, would be written by something newer and read here as weight
    0.0, which is how a future `rejected` becomes a boost.
    """
    source = MIGRATION.read_text()
    match = re.search(
        rf"CONSTRAINT {constraint}\s*\n?\s*CHECK \((?:\w+) IN \((.*?)\)\)",
        source, re.DOTALL,
    )
    assert match, f"no CHECK constraint named {constraint} in {MIGRATION.name}"
    return set(re.findall(r"'([a-z_]+)'", match.group(1)))


# --- the vocabulary and the database agree ------------------------------


def test_the_states_the_database_accepts_are_exactly_the_states_in_code():
    assert _check_values("fact_verification_state_check") == set(states.ALL_STATES)


def test_the_verifier_kinds_the_database_accepts_are_exactly_the_ones_in_code():
    assert _check_values("fact_verification_verifier_kind_check") == set(
        states.VERIFIER_KINDS
    )


def test_every_state_is_either_authoritative_or_advisory_and_not_both():
    """The split decides whether a state can remove a fact from an answer. A
    state in neither set is one nothing has decided about, and a state in both
    is a heuristic that can hide a memory."""
    assert states.AUTHORITATIVE | states.ADVISORY == set(states.ALL_STATES)
    assert not states.AUTHORITATIVE & states.ADVISORY


def test_only_a_deliberate_state_can_suppress():
    """A rule that could suppress in bulk is a denial of service on your own
    memory, so the suppressing states have to be ones a verifier chose."""
    assert states.SUPPRESSED <= states.AUTHORITATIVE
    assert states.SUPPRESSED == {states.REJECTED}


def test_every_state_has_a_weight():
    """A state with no weight entry reads as 0.0, which is indistinguishable
    from a state measured to be worth nothing."""
    assert set(states.TRUST_WEIGHTS) == set(states.ALL_STATES)


def test_a_state_this_version_does_not_know_weighs_nothing():
    """Written by a newer version. Guessing what it meant is how a future
    `rejected` becomes a boost."""
    assert states.weight("quantum_verified") == 0.0


def test_a_checked_claim_outranks_an_unchecked_one_and_a_lapsed_one_does_not():
    assert states.weight(states.VERIFIED) > states.weight(states.UNVERIFIED)
    assert states.weight(states.STALE) < states.weight(states.UNVERIFIED)


# --- keys, and rotation -------------------------------------------------


def test_no_keys_configured_signs_unkeyed_under_a_name(monkeypatch):
    """`unkeyed` is a recorded key_id rather than NULL, so "signed with no
    key" and "we forgot to record which key" cannot read as the same thing."""
    monkeypatch.delenv(vattest.KEYS_ENV, raising=False)
    monkeypatch.delenv(vattest.KEY_ID_ENV, raising=False)

    key_id, material = vattest.current()

    assert (key_id, material) == (vattest.UNKEYED, None)
    assert vattest.alg_of(material) == "sha256"


def test_the_first_key_signs_when_no_key_id_is_named(monkeypatch):
    """A single-key deployment needs one variable, not two."""
    monkeypatch.setenv(vattest.KEYS_ENV, "k1:aaaa,k2:bbbb")
    monkeypatch.delenv(vattest.KEY_ID_ENV, raising=False)

    assert vattest.current() == ("k1", "aaaa")


def test_naming_a_key_selects_it(monkeypatch):
    monkeypatch.setenv(vattest.KEYS_ENV, "k1:aaaa,k2:bbbb")
    monkeypatch.setenv(vattest.KEY_ID_ENV, "k2")

    assert vattest.current() == ("k2", "bbbb")


def test_naming_a_key_that_is_not_configured_is_refused(monkeypatch):
    """Falling back to the first key would sign with a key other than the one
    asked for, which is the exact failure rotation exists to avoid and would
    leave nothing behind to notice."""
    monkeypatch.setenv(vattest.KEYS_ENV, "k1:aaaa")
    monkeypatch.setenv(vattest.KEY_ID_ENV, "k2")

    with pytest.raises(vattest.VAttestError, match="not in"):
        vattest.current()


def test_a_malformed_key_entry_is_refused_rather_than_skipped(monkeypatch):
    """Skipping it produces a chain that is unkeyed because of a missing colon
    in an environment variable, and keyed according to whoever set it."""
    monkeypatch.setenv(vattest.KEYS_ENV, "k1:aaaa,oops")

    with pytest.raises(vattest.VAttestError, match="key_id:hexkey"):
        vattest.keys()


def test_a_keyed_digest_cannot_be_recomputed_without_the_key():
    """The property the key buys. Without it, anybody who can write to this
    database can also write a digest that verifies."""
    body = vattest.canonical({"state": "verified", "verifier_id": "ayush"})
    keyed = vattest.digest(vattest.GENESIS, body, "a-test-key")

    assert keyed.startswith("hmac-sha256:")
    assert vattest.digest(vattest.GENESIS, body, None) != keyed
    assert vattest.digest(vattest.GENESIS, body, "another-key") != keyed


def test_the_chain_puts_the_previous_digest_inside_the_hash():
    """What makes this a chain rather than a column of digests: the same
    content at a different position in the chain is a different link, so a
    changed link cannot be repaired locally."""
    body = vattest.canonical({"state": "verified"})

    assert vattest.digest("sha256:abc", body, None) != vattest.digest(
        vattest.GENESIS, body, None
    )


def test_canonical_does_not_depend_on_key_order():
    """A digest has to be reproducible by a different version of this code on
    a different machine. Anything that lets two serialisations of the same row
    differ turns a verification failure into a coin flip."""
    assert vattest.canonical({"a": 1, "b": 2}) == vattest.canonical({"b": 2, "a": 1})
