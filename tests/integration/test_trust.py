"""Whether a claim was checked, who checked it, and what that record is worth.

Everything in this store is believed because somebody wrote it. These tests
are about the record of somebody having checked, and they attack it from the
three directions that matter:

- **Suppression.** Exactly one state removes a fact from an answer, and the
  tests pin that the other five do not. A trust mechanism that can hide a
  memory on a heuristic is a denial of service on your own memory.
- **Degradation.** An expired verification must leave the fact findable. The
  test asserts the fact comes back from `query_memory`, not merely that a set
  does not contain it, because "it still reads as stale" and "it is still
  there" are different claims and only the second one is the promise.
- **Tamper evidence.** A `verified` row by a named human on a named policy is
  the most attractive row in this schema to edit. The chain has to notice, and
  it has to keep noticing after a key rotation, or nobody will ever rotate.

And one that is not about this feature at all: `fact_attestation` must be bit
identical before and after any amount of verification activity. Two chains
that can perturb each other would mean recording a check changes the evidence
about the claim being checked.
"""

from __future__ import annotations

import pytest
from fake_embedder import REFERENCE, VectorEmbedder

from echo_memory import attestation
from echo_memory.infra.db import connect
from echo_memory.ingestion.write_episode import write_episode
from echo_memory.retrieval.query_memory import query_memory
from echo_memory.trust import evidence, states, vattest, verify

GROUP = "g-trust"
OTHER = "g-trust-other"

FACTS = {
    "billing uses Razorpay": ("billing", "Razorpay"),
    "deploys run from main": ("deploys", "main"),
    "alerts go to Slack": ("alerts", "Slack"),
    "the other scope's fact": ("a", "b"),
}

# Every string any of these tests embeds, registered up front. VectorEmbedder
# raises on anything unregistered, which is the point: a test cannot fall
# through to accidental behaviour.
EMBEDDER = VectorEmbedder({
    text: REFERENCE
    for fact, (source, target) in FACTS.items()
    for text in (fact, source, target)
})


def _write(conn, fact: str, group_id: str = GROUP) -> str:
    source, target = FACTS[fact]
    result = write_episode(
        conn, group_id, "s1",
        [{"name": source, "type": "x"}, {"name": target, "type": "x"}],
        [{"source": source, "target": target, "relation_type": "uses",
          "fact": fact, "confidence": "extracted"}],
        {source: {"resolved_to": "new"}, target: {"resolved_to": "new"}},
        EMBEDDER, assume_new=True,
    )
    assert result["edges_created"], result
    return result["edges_created"][0]


def _verified(conn, edge_id: str, **kwargs) -> int:
    return verify.record(
        conn, GROUP, edge_id, states.VERIFIED,
        verifier_kind=states.HUMAN, verifier_id="ayush",
        method="ran the suite", **kwargs,
    )


# --- what a state does to an answer -------------------------------------


def test_an_unchecked_fact_reads_as_unverified(migrated_db):
    """Which is every fact written before this existed. The default has to be
    a state and not a missing key, or every caller invents its own."""
    with connect(migrated_db) as conn:
        edge_id = _write(conn, "billing uses Razorpay")

        assert verify.state_of(conn, GROUP, edge_id) == states.UNVERIFIED
        assert verify.states_of(conn, GROUP, [edge_id]) == {
            edge_id: states.UNVERIFIED
        }


def test_only_rejected_suppresses(migrated_db):
    """`verified` and `disputed` are both returned, and `disputed` is the one
    that proves the point: a contested claim the reader can see is worth more
    than a gap they cannot."""
    with connect(migrated_db) as conn:
        good = _write(conn, "billing uses Razorpay")
        contested = _write(conn, "deploys run from main")
        refused = _write(conn, "alerts go to Slack")

        _verified(conn, good)
        verify.record(conn, GROUP, contested, states.DISPUTED,
                      verifier_kind=states.SERVICE, verifier_id="contradiction-check",
                      method="found a contradicting fact")
        verify.record(conn, GROUP, refused, states.REJECTED,
                      verifier_kind=states.HUMAN, verifier_id="ayush",
                      method="read the deploy log")

        out = verify.suppressed(conn, GROUP, [good, contested, refused])

    assert out == {refused}


def test_the_newest_row_is_the_current_state(migrated_db):
    """Append only. "Verified on the 3rd, disputed on the 9th" has to read as
    disputed without the earlier row being destroyed, because the history is
    the only thing that answers who changed their mind."""
    with connect(migrated_db) as conn:
        edge_id = _write(conn, "billing uses Razorpay")
        _verified(conn, edge_id)
        verify.record(conn, GROUP, edge_id, states.DISPUTED,
                      verifier_kind=states.HUMAN, verifier_id="ayush",
                      method="found the deploy log said otherwise")

        assert verify.state_of(conn, GROUP, edge_id) == states.DISPUTED
        (n,) = conn.execute(
            "SELECT count(*) FROM public.fact_verification WHERE edge_id = %s::graphid",
            (edge_id,),
        ).fetchone()

    assert n == 2, "a later verification overwrote an earlier one"


def test_a_lapsed_verification_reads_as_stale_and_the_fact_is_still_findable(
    migrated_db,
):
    """The whole design of expiry in one test. A verification that has run out
    degrades the fact's ranking and must not remove it from an answer: hiding
    a real memory is worse than including a mediocre one, and a timer that
    hides thousands of them is the easiest version of that mistake to build.
    """
    with connect(migrated_db) as conn:
        edge_id = _write(conn, "billing uses Razorpay")
        _verified(conn, edge_id, expires_at="2020-01-01T00:00:00Z")

        assert verify.state_of(conn, GROUP, edge_id) == states.STALE
        assert verify.suppressed(conn, GROUP, [edge_id]) == set()

        found = query_memory(conn, GROUP, "billing uses Razorpay", 10, EMBEDDER)

    assert edge_id in [f["fact_id"] for f in found["facts"]], (
        "a fact whose verification lapsed vanished from the answer"
    )


def test_a_verification_that_has_not_lapsed_yet_still_reads_as_verified(migrated_db):
    with connect(migrated_db) as conn:
        edge_id = _write(conn, "billing uses Razorpay")
        _verified(conn, edge_id, expires_at="2099-01-01T00:00:00Z")

        assert verify.state_of(conn, GROUP, edge_id) == states.VERIFIED


def test_expiry_is_judged_against_the_databases_clock(migrated_db):
    """`now=` exists for this test and not for callers. Comparing against a
    Python timestamp instead would make a fact flip between verified and stale
    depending on which process asked, and 24ms of container skew has already
    turned "3 days left" into 4 in this project."""
    with connect(migrated_db) as conn:
        edge_id = _write(conn, "billing uses Razorpay")
        _verified(conn, edge_id, expires_at="2030-06-01T00:00:00Z")

        assert verify.state_of(conn, GROUP, edge_id) == states.VERIFIED
        assert verify.state_of(
            conn, GROUP, edge_id, now="2031-01-01T00:00:00Z"
        ) == states.STALE


def test_a_lapsed_rejection_stays_rejected(migrated_db):
    """`expires_at` says how long a positive check is good for. Applying it to
    a refusal would make suppression quietly expire on a clock, which is a
    different decision and not one this module is entitled to make."""
    with connect(migrated_db) as conn:
        edge_id = _write(conn, "alerts go to Slack")
        verify.record(conn, GROUP, edge_id, states.REJECTED,
                      verifier_kind=states.HUMAN, verifier_id="ayush",
                      method="read the deploy log",
                      expires_at="2020-01-01T00:00:00Z")

        assert verify.state_of(conn, GROUP, edge_id) == states.REJECTED


def test_summary_counts_every_state_including_the_zeros(migrated_db):
    """Six small integers, so a reader comparing two answers never has to work
    out whether a missing key is a zero or an older version of the field."""
    with connect(migrated_db) as conn:
        checked = _write(conn, "billing uses Razorpay")
        unchecked = _write(conn, "deploys run from main")
        _verified(conn, checked)

        out = verify.summary(conn, GROUP, [checked, unchecked])

    assert set(out) == set(states.ALL_STATES)
    assert out[states.VERIFIED] == 1
    assert out[states.UNVERIFIED] == 1
    assert out[states.REJECTED] == 0


# --- the restraint on ranking -------------------------------------------


def test_ranked_never_returns_an_id_it_was_not_given(migrated_db):
    """The restraint `salience` spells out. The graph hop introduced
    candidates and cost -0.142 MRR on the shape it was meant to help least,
    and a trust signal is a worse place to relax it: "this fact is verified"
    is not an answer to an unrelated question."""
    with connect(migrated_db) as conn:
        asked = _write(conn, "billing uses Razorpay")
        not_asked = _write(conn, "deploys run from main")
        # Verified, and so the most attractive thing a ranker could reach for.
        _verified(conn, not_asked)

        out = verify.ranked(conn, GROUP, [asked])

    assert out == [asked]
    assert not_asked not in out


def test_ranked_puts_a_verified_fact_above_an_unchecked_one(migrated_db):
    """And returns every id it was given, in a stable order within a weight,
    so a scope that has never verified anything gets its input back
    unchanged."""
    with connect(migrated_db) as conn:
        first = _write(conn, "billing uses Razorpay")
        second = _write(conn, "deploys run from main")
        third = _write(conn, "alerts go to Slack")
        _verified(conn, third)

        reordered = verify.ranked(conn, GROUP, [first, second, third])
        untouched = verify.ranked(conn, GROUP, [first, second])

    assert reordered == [third, first, second]
    assert untouched == [first, second]


# --- what is refused ----------------------------------------------------


def test_an_unknown_state_is_refused(migrated_db):
    """Checked in code and not only by the CHECK constraint, because a
    CheckViolation names a constraint and never the vocabulary it enforced."""
    with connect(migrated_db) as conn:
        edge_id = _write(conn, "billing uses Razorpay")

        with pytest.raises(verify.VerificationError, match="unknown trust state"):
            verify.record(conn, GROUP, edge_id, "probably_fine",
                          verifier_kind=states.HUMAN, verifier_id="ayush",
                          method="felt right")


def test_an_unknown_verifier_kind_is_refused(migrated_db):
    with connect(migrated_db) as conn:
        edge_id = _write(conn, "billing uses Razorpay")

        with pytest.raises(verify.VerificationError, match="unknown verifier kind"):
            verify.record(conn, GROUP, edge_id, states.VERIFIED,
                          verifier_kind="vibes", verifier_id="ayush",
                          method="ran the suite")


def test_a_refused_verification_writes_nothing(migrated_db):
    """A refusal that had already inserted the row would leave the state it
    refused to accept in the table."""
    with connect(migrated_db) as conn:
        edge_id = _write(conn, "billing uses Razorpay")
        with pytest.raises(verify.VerificationError):
            verify.record(conn, GROUP, edge_id, "probably_fine",
                          verifier_kind=states.HUMAN, verifier_id="ayush",
                          method="felt right")

        (n,) = conn.execute(
            "SELECT count(*) FROM public.fact_verification WHERE group_id = %s",
            (GROUP,),
        ).fetchone()

    assert n == 0


# --- evidence -----------------------------------------------------------


def test_evidence_is_recorded_linked_and_read_back(migrated_db):
    with connect(migrated_db) as conn:
        edge_id = _write(conn, "billing uses Razorpay")
        evidence_id = evidence.record(
            conn, GROUP, "deployment",
            uri="https://ci.example/run/41",
            detail={"release": "0.5.7"},
            reporter_agent="claude-code", reporter_run="r-1",
            content_digest="sha256:abc",
        )
        evidence.link(conn, GROUP, edge_id, evidence_id)

        attached = evidence.for_fact(conn, GROUP, edge_id)

    assert [a["evidence_id"] for a in attached] == [evidence_id]
    assert attached[0]["kind"] == "deployment"
    assert attached[0]["detail"] == {"release": "0.5.7"}
    assert attached[0]["content_digest"] == "sha256:abc"


def test_an_unknown_evidence_kind_is_refused(migrated_db):
    with connect(migrated_db) as conn, \
            pytest.raises(evidence.EvidenceError, match="unknown evidence kind"):
        evidence.record(conn, GROUP, "a hunch", reporter_agent="claude-code")


def test_linking_the_same_evidence_twice_is_not_an_error(migrated_db):
    """Evidence arrives from hooks and retries, and a second identical link is
    the same statement rather than a new one."""
    with connect(migrated_db) as conn:
        edge_id = _write(conn, "billing uses Razorpay")
        evidence_id = evidence.record(conn, GROUP, "test_run",
                                      reporter_agent="claude-code")
        evidence.link(conn, GROUP, edge_id, evidence_id)
        evidence.link(conn, GROUP, edge_id, evidence_id)

        assert len(evidence.for_fact(conn, GROUP, edge_id)) == 1


def test_evidence_cannot_be_linked_across_scopes(migrated_db):
    """`fact_evidence` carries its own group_id, so an insert can name one
    scope while the evidence belongs to another, and the link would read back
    under both. Scopes are tenants, so that is a cross-tenant leak with no
    error attached to it."""
    with connect(migrated_db) as conn:
        edge_id = _write(conn, "billing uses Razorpay")
        elsewhere = evidence.record(conn, OTHER, "deployment",
                                    reporter_agent="claude-code")

        with pytest.raises(evidence.EvidenceError, match="another scope"):
            evidence.link(conn, GROUP, edge_id, elsewhere)

        assert evidence.for_fact(conn, GROUP, edge_id) == []


def test_count_for_returns_a_zero_for_every_fact_nothing_backs(migrated_db):
    """A caller that has to remember a default is a caller that gets it wrong
    where the tests do not look."""
    with connect(migrated_db) as conn:
        backed = _write(conn, "billing uses Razorpay")
        bare = _write(conn, "deploys run from main")
        for _ in range(2):
            evidence_id = evidence.record(conn, GROUP, "test_run",
                                          reporter_agent="claude-code")
            evidence.link(conn, GROUP, backed, evidence_id)

        counts = evidence.count_for(conn, GROUP, [backed, bare])

    assert counts == {backed: 2, bare: 0}


# --- the verification chain ---------------------------------------------


def test_a_verification_is_attested_without_being_asked(migrated_db):
    """On the write path, not as a separate step an operator can forget. A
    verification that is only attested when somebody remembers to is attested
    for the rows nobody is suspicious about."""
    with connect(migrated_db) as conn:
        edge_id = _write(conn, "billing uses Razorpay")
        verification_id = _verified(conn, edge_id)

        row = conn.execute(
            """SELECT seq, prev_digest, chain_version, key_id
                 FROM public.verification_attestation
                WHERE verification_id = %s""",
            (verification_id,),
        ).fetchone()
        out = vattest.report(conn, GROUP)

    assert row is not None, "a verification was recorded with no attestation"
    assert row[0] == 1
    assert row[1] == vattest.GENESIS
    assert row[2] == vattest.CHAIN_VERSION
    assert row[3] == vattest.UNKEYED
    assert out.is_clean()
    assert out.n_attested == 1
    assert "outside this database" in out.strength


def test_each_verification_chains_to_the_one_before_it(migrated_db):
    with connect(migrated_db) as conn:
        edge_id = _write(conn, "billing uses Razorpay")
        _verified(conn, edge_id)
        verify.record(conn, GROUP, edge_id, states.DISPUTED,
                      verifier_kind=states.HUMAN, verifier_id="ayush",
                      method="found the deploy log said otherwise")

        rows = conn.execute(
            """SELECT seq, prev_digest, digest
                 FROM public.verification_attestation
                WHERE group_id = %s ORDER BY seq""",
            (GROUP,),
        ).fetchall()

    assert [r[0] for r in rows] == [1, 2]
    assert rows[1][1] == rows[0][2], "the second link does not chain to the first"


def test_editing_a_verifications_state_is_detected(migrated_db):
    """The attack this chain exists for: turning a `disputed` row into a
    `verified` one, at the table, with nothing in this package involved."""
    with connect(migrated_db) as conn:
        edge_id = _write(conn, "billing uses Razorpay")
        verification_id = verify.record(
            conn, GROUP, edge_id, states.DISPUTED,
            verifier_kind=states.HUMAN, verifier_id="ayush",
            method="found a contradicting fact",
        )
        conn.execute(
            "UPDATE public.fact_verification SET state = %s WHERE id = %s",
            (states.VERIFIED, verification_id),
        )

        out = vattest.report(conn, GROUP)

    assert not out.is_clean()
    assert [a["verification_id"] for a in out.altered] == [verification_id]


def test_extending_an_expiry_is_detected(migrated_db):
    """The cheapest useful edit available against this table: a lapsed check
    reads as stale, and pushing `expires_at` out makes it read as verified
    again with nothing else out of place. Which is why the digest covers it."""
    with connect(migrated_db) as conn:
        edge_id = _write(conn, "billing uses Razorpay")
        verification_id = _verified(conn, edge_id,
                                    expires_at="2020-01-01T00:00:00Z")
        conn.execute(
            "UPDATE public.fact_verification SET expires_at = %s WHERE id = %s",
            ("2099-01-01T00:00:00Z", verification_id),
        )

        assert verify.state_of(conn, GROUP, edge_id) == states.VERIFIED
        out = vattest.report(conn, GROUP)

    assert [a["verification_id"] for a in out.altered] == [verification_id], (
        "an expiry was extended and the chain did not notice"
    )


def test_a_removed_link_shows_as_a_broken_chain(migrated_db):
    with connect(migrated_db) as conn:
        edge_id = _write(conn, "billing uses Razorpay")
        _verified(conn, edge_id)
        verify.record(conn, GROUP, edge_id, states.DISPUTED,
                      verifier_kind=states.HUMAN, verifier_id="ayush",
                      method="found the deploy log said otherwise")
        conn.execute(
            """DELETE FROM public.verification_attestation
                WHERE group_id = %s AND seq = 1""",
            (GROUP,),
        )

        out = vattest.report(conn, GROUP)

    assert out.broken_links, "a deleted link left the chain looking continuous"
    assert out.broken_links[0]["seq"] == 2


def test_a_deleted_verification_row_is_reported_as_vanished(migrated_db):
    """Nothing deletes from `fact_verification` by design, so unlike a missing
    fact there is no erasure that could account for this."""
    with connect(migrated_db) as conn:
        edge_id = _write(conn, "billing uses Razorpay")
        verification_id = _verified(conn, edge_id)
        conn.execute("DELETE FROM public.fact_verification WHERE id = %s",
                     (verification_id,))

        out = vattest.report(conn, GROUP)

    assert out.vanished == [verification_id]
    assert not out.is_clean()


def test_one_scope_cannot_perturb_another_scopes_chain(migrated_db):
    """Scopes are tenants. A chain whose head moved when another account
    recorded a verification would make every verification depend on every
    tenant, and would hand one account a way to invalidate another's
    evidence."""
    with connect(migrated_db) as conn:
        edge_id = _write(conn, "billing uses Razorpay")
        _verified(conn, edge_id)
        before = vattest.head(conn, GROUP)

        other_edge = _write(conn, "the other scope's fact", group_id=OTHER)
        verify.record(conn, OTHER, other_edge, states.VERIFIED,
                      verifier_kind=states.SERVICE, verifier_id="their-ci",
                      method="ran their suite")

        assert vattest.head(conn, GROUP) == before
        assert vattest.head(conn, OTHER)[0] == 1
        assert vattest.report(conn, GROUP).is_clean()
        assert vattest.report(conn, OTHER).is_clean()


# --- keys and rotation --------------------------------------------------


def test_rotating_a_key_still_verifies_the_links_the_old_key_signed(
    migrated_db, monkeypatch,
):
    """The property that makes rotation possible at all. Verifying an old link
    with the current key would make every verification in history read as
    forged the moment a key changed, and nobody rotates a key that invalidates
    their own audit trail."""
    monkeypatch.setenv(vattest.KEYS_ENV, "kA:" + "aa" * 16)
    with connect(migrated_db) as conn:
        edge_id = _write(conn, "billing uses Razorpay")
        _verified(conn, edge_id)

        monkeypatch.setenv(vattest.KEYS_ENV, f"kA:{'aa' * 16},kB:{'bb' * 16}")
        monkeypatch.setenv(vattest.KEY_ID_ENV, "kB")
        verify.record(conn, GROUP, edge_id, states.DISPUTED,
                      verifier_kind=states.HUMAN, verifier_id="ayush",
                      method="found the deploy log said otherwise")

        out = vattest.report(conn, GROUP)

    assert out.key_ids == ["kA", "kB"], "the signing key was not recorded per link"
    assert out.is_clean(), "a link signed with the retired key failed to verify"
    assert out.keyed is True
    assert out.mixed_keying is False, (
        "two keys were reported as a partly unkeyed chain, which is rotation "
        "described as a weakness"
    )
    assert out.unverifiable == []
    assert f"without the matching key in {vattest.KEYS_ENV}" in out.strength


def test_a_link_signed_with_a_key_that_is_gone_is_unverifiable_not_altered(
    migrated_db, monkeypatch,
):
    """Not being able to check a link is an absence of evidence, not evidence
    of tampering. A report that called a retired key a forgery would cry wolf
    every time a key was decommissioned, and `strength` says so out loud
    rather than letting `is_clean` imply the chain was checked."""
    monkeypatch.setenv(vattest.KEYS_ENV, "kA:" + "aa" * 16)
    with connect(migrated_db) as conn:
        edge_id = _write(conn, "billing uses Razorpay")
        _verified(conn, edge_id)

        monkeypatch.setenv(vattest.KEYS_ENV, "kB:" + "bb" * 16)
        out = vattest.report(conn, GROUP)

    assert out.altered == []
    assert [u["key_id"] for u in out.unverifiable] == ["kA"]
    assert "were not checked at all" in out.strength


def test_a_chain_part_keyed_and_part_not_is_described_as_neither(
    migrated_db, monkeypatch,
):
    """The difference between tamper-evident and tamper-proof, and a report
    that picked one of the two would be the line somebody quotes."""
    with connect(migrated_db) as conn:
        edge_id = _write(conn, "billing uses Razorpay")
        _verified(conn, edge_id)

        monkeypatch.setenv(vattest.KEYS_ENV, "kA:" + "aa" * 16)
        verify.record(conn, GROUP, edge_id, states.DISPUTED,
                      verifier_kind=states.HUMAN, verifier_id="ayush",
                      method="found the deploy log said otherwise")

        out = vattest.report(conn, GROUP)

    assert out.mixed_keying is True
    assert out.keyed is False
    assert "part of this chain is keyed" in out.strength
    assert out.is_clean()


# --- the other chain must not move --------------------------------------


def test_verification_leaves_the_fact_attestation_chain_bit_identical(migrated_db):
    """The invariant that keeps the two chains honest.

    `fact_attestation` is what makes a fact unrewritable, and it is dense per
    scope: if verifications shared it, recording a check would shift the
    sequence numbers every fact's digest chains through, so the act of
    verifying a claim would alter the evidence about that claim. Head, every
    digest, and the report are all compared across a full round of evidence
    and verification work.
    """
    with connect(migrated_db) as conn:
        first = _write(conn, "billing uses Razorpay")
        second = _write(conn, "deploys run from main")
        head_before = attestation.head(conn, GROUP)
        digests_before = conn.execute(
            """SELECT edge_id::text, seq, prev_digest, digest, keyed
                 FROM public.fact_attestation
                WHERE group_id = %s ORDER BY seq""",
            (GROUP,),
        ).fetchall()

        evidence_id = evidence.record(conn, GROUP, "deployment",
                                      uri="https://ci.example/run/41",
                                      reporter_agent="claude-code")
        evidence.link(conn, GROUP, first, evidence_id)
        evidence.link(conn, GROUP, second, evidence_id)
        _verified(conn, first, evidence_ids=[evidence_id],
                  scope={"environment": "production"})
        verify.record(conn, GROUP, second, states.REJECTED,
                      verifier_kind=states.HUMAN, verifier_id="ayush",
                      method="read the deploy log")
        verify.record(conn, GROUP, first, states.DISPUTED,
                      verifier_kind=states.RULE, verifier_id="contradiction-rule",
                      method="found a contradicting fact")

        head_after = attestation.head(conn, GROUP)
        digests_after = conn.execute(
            """SELECT edge_id::text, seq, prev_digest, digest, keyed
                 FROM public.fact_attestation
                WHERE group_id = %s ORDER BY seq""",
            (GROUP,),
        ).fetchall()
        fact_report = attestation.report(conn, GROUP)
        verification_report = vattest.report(conn, GROUP)

    assert head_after == head_before, (
        "verification activity moved the fact attestation head"
    )
    assert digests_after == digests_before, (
        "verification activity rewrote a fact's digest"
    )
    assert fact_report.is_clean()
    assert fact_report.n_attested == 2
    assert verification_report.n_attested == 3
    assert verification_report.is_clean()


def test_tampering_with_a_verification_leaves_the_fact_chain_clean(migrated_db):
    """The other direction of the same separation. An edited verification is a
    finding on its own chain and says nothing about the fact, because the fact
    has not changed."""
    with connect(migrated_db) as conn:
        edge_id = _write(conn, "billing uses Razorpay")
        verification_id = _verified(conn, edge_id)
        conn.execute(
            "UPDATE public.fact_verification SET verifier_id = %s WHERE id = %s",
            ("somebody-else", verification_id),
        )

        assert vattest.report(conn, GROUP).altered, (
            "the verifier was rewritten and the chain did not notice"
        )
        assert attestation.report(conn, GROUP).is_clean(), (
            "an edit to a verification was reported against the fact's chain"
        )
