"""A bundle has to mean the same thing on the machine that reads it.

The tests that matter here are the ones about bytes. A digest over
non-canonical JSON verifies where it was made and nowhere else, and the
failure reaches the recipient as "this bundle is corrupt" - indistinguishable
from an attack, and unfixable by either end. So the first group pins
determinism, and the second pins that a bundle which does not hang together is
refused with a message saying which of the three possible things went wrong.

The redaction tests are rule by rule on purpose. A policy that drops the right
total for the wrong reasons is a policy whose audit lies to the operator
reading it before they hand a file to another company.
"""

from __future__ import annotations

import json

import pytest

from echo_memory.exchange import bundle, redact

POLICY = redact.POLICIES["full"]


def _fact(**overrides) -> dict:
    row = {
        "origin_fact_id": "844424930131969",
        "relation_type": "uses",
        "fact": "billing uses Razorpay for recurring subscriptions",
        "confidence": "extracted",
        "causal_hint": None,
        "t_valid": 1762800000,
        "source": "billing",
        "target": "Razorpay",
        "source_type": "component",
        "target_type": "service",
        "provenance": {"session_id": "s1", "source_episode_id": "e1"},
        "agent_id": "claude-code",
    }
    row.update(overrides)
    return row


def _bundle_of(facts: list[dict]) -> tuple[bytes, bytes]:
    """A manifest and facts blob built the way `build` builds them, without a
    database. Keeps these tests unit tests while still exercising the exact
    bytes the real builder produces."""
    blob = b"".join(bundle.canonical(f) + b"\n" for f in facts)
    manifest = bundle.Manifest(
        format_version=bundle.FORMAT_VERSION,
        bundle_id="b-1",
        deployment_id="dep-a",
        account="acct-a",
        memory="shared",
        created_at="2026-11-11T00:00:00+00:00",
        redaction_policy_id="full",
        fact_count=len(facts),
        content_digest=bundle.digest_of(blob),
    )
    return bundle.canonical(manifest.to_dict()), blob


# --- canonical bytes ----------------------------------------------------


def test_canonical_is_byte_stable_across_key_orderings():
    """The property the whole format rests on. Two dicts holding the same
    claim in different insertion orders must serialise identically, because
    the exporter and the importer build their dicts in different code."""
    one = {"fact": "a", "source": "b", "t_valid": 1, "provenance": {"x": 1, "y": 2}}
    other = {"provenance": {"y": 2, "x": 1}, "t_valid": 1, "source": "b", "fact": "a"}

    assert bundle.canonical(one) == bundle.canonical(other)
    assert bundle.digest_of(bundle.canonical(one)) == bundle.digest_of(
        bundle.canonical(other)
    )


def test_canonical_has_no_insignificant_whitespace():
    """json.dumps defaults to ", " and ": " separators. A bundle built by a
    caller that reached for the default and one built here would hash
    differently while holding identical claims."""
    assert bundle.canonical({"a": 1, "b": [1, 2]}) == b'{"a":1,"b":[1,2]}'


def test_canonical_keeps_non_ascii_as_utf8():
    """Not \\uXXXX escapes. The escaped form is longer and its exact spelling
    depends on the encoder, which is a second way for two serialisations of one
    claim to differ."""
    raw = bundle.canonical({"fact": "Ateleir México"})
    assert "México".encode() in raw
    assert b"\\u" not in raw


def test_a_digest_over_the_same_facts_in_the_same_order_is_the_same():
    facts = [_fact(), _fact(origin_fact_id="844424930131970", fact="deploys run from main")]
    first_manifest, first_blob = _bundle_of(facts)
    second_manifest, second_blob = _bundle_of(facts)
    assert first_blob == second_blob
    assert first_manifest == second_manifest


# --- what parse refuses -------------------------------------------------


def test_a_tampered_facts_blob_fails_the_digest_and_says_what_differed():
    """The message has to name both digests. An operator holding a file that
    will not import needs to tell a truncated download from a rewritten blob,
    and "invalid bundle" answers neither."""
    manifest_bytes, blob = _bundle_of([_fact()])
    tampered = blob.replace(b"Razorpay", b"Stripe!!")
    assert len(tampered) == len(blob), "the tamper must not change the length"

    with pytest.raises(bundle.BundleError) as e:
        bundle.parse(manifest_bytes, tampered)

    message = str(e.value)
    assert "content digest mismatch" in message
    assert bundle.digest_of(blob) in message, "the expected digest is not named"
    assert bundle.digest_of(tampered) in message, "the computed digest is not named"


def test_an_unknown_format_version_is_refused():
    """A newer bundle is not read on a best-effort basis: the field this
    version does not understand may be the one saying what may not leave."""
    manifest_bytes, blob = _bundle_of([_fact()])
    body = json.loads(manifest_bytes)
    body["format_version"] = bundle.FORMAT_VERSION + 1

    with pytest.raises(bundle.BundleError) as e:
        bundle.parse(bundle.canonical(body), blob)
    assert str(bundle.FORMAT_VERSION + 1) in str(e.value)
    assert "format_version" in str(e.value)


def test_a_manifest_with_no_format_version_is_refused_as_a_version_problem():
    manifest_bytes, blob = _bundle_of([_fact()])
    body = json.loads(manifest_bytes)
    del body["format_version"]

    with pytest.raises(bundle.BundleError, match="no format_version"):
        bundle.parse(bundle.canonical(body), blob)


def test_a_fact_count_that_disagrees_with_the_blob_is_refused():
    """The digest already proves the blob belongs to the manifest, so this
    only fires when the exporter disagreed with itself - a bug at the sender,
    and not a count to import under."""
    facts = [_fact()]
    blob = b"".join(bundle.canonical(f) + b"\n" for f in facts)
    manifest = bundle.Manifest(
        format_version=bundle.FORMAT_VERSION,
        bundle_id="b-1",
        deployment_id="dep-a",
        account="acct-a",
        memory="shared",
        created_at="2026-11-11T00:00:00+00:00",
        redaction_policy_id="full",
        fact_count=7,
        content_digest=bundle.digest_of(blob),
    )

    with pytest.raises(bundle.BundleError, match="fact count mismatch"):
        bundle.parse(bundle.canonical(manifest.to_dict()), blob)


def test_a_round_trip_returns_the_same_claims():
    facts = [_fact(), _fact(origin_fact_id="2", fact="deploys run from main")]
    manifest_bytes, blob = _bundle_of(facts)

    manifest, parsed = bundle.parse(manifest_bytes, blob)

    assert manifest.fact_count == 2
    assert manifest.bundle_id == "b-1"
    assert parsed == facts


def test_a_signature_is_not_part_of_what_a_signature_covers():
    manifest, _blob = _bundle_of([_fact()])
    body = bundle.Manifest.from_dict(json.loads(manifest))
    with_sig = body.signed({"alg": "ed25519", "key_id": "k1", "value": "ff"})

    assert "signature" not in with_sig.unsigned()
    assert with_sig.unsigned() == body.unsigned(), (
        "attaching a signature changed the bytes the signature covers"
    )


# --- redaction, rule by rule --------------------------------------------


def test_drop_relations_drops_only_that_relation():
    policy = redact.Policy(
        policy_id="t", drop_relations=frozenset({"pays"}),
        drop_entity_types=frozenset(), drop_matching=(),
        keep_agent_id=True, keep_provenance=True,
    )
    assert redact.apply(policy, _fact(relation_type="pays")) is None
    assert redact.apply(policy, _fact(relation_type="uses")) is not None


def test_drop_entity_types_looks_at_both_endpoints():
    """A fact about a person is about a person whichever end they are on."""
    policy = redact.Policy(
        policy_id="t", drop_relations=frozenset(),
        drop_entity_types=frozenset({"person"}), drop_matching=(),
        keep_agent_id=True, keep_provenance=True,
    )
    assert redact.apply(policy, _fact(source_type="person")) is None
    assert redact.apply(policy, _fact(target_type="person")) is None
    assert redact.apply(policy, _fact()) is not None


def test_drop_matching_scans_the_endpoint_names_too():
    """This store's entities are frequently identifiers, so a pattern list
    that read only `fact` would pass a bundle whose source node is an address."""
    policy = redact.POLICIES["minimal"]
    assert redact.apply(policy, _fact(fact="mail ayush@example.com about it")) is None
    assert redact.apply(policy, _fact(source="ayush@example.com")) is None
    assert redact.apply(policy, _fact(target="ayush@example.com")) is None


def test_a_matched_fact_is_dropped_whole_rather_than_masked():
    """A claim with a hole in it is still the same claim, and the hole says
    where to look."""
    policy = redact.POLICIES["minimal"]
    assert redact.apply(policy, _fact(fact="the key is AKIAIOSFODNN7EXAMPLE")) is None


def test_minimal_strips_the_author_and_the_provenance_and_keeps_the_claim():
    kept = redact.apply(redact.POLICIES["minimal"], _fact())
    assert kept is not None
    assert "agent_id" not in kept
    assert "provenance" not in kept
    assert kept["fact"] == _fact()["fact"]
    assert kept["relation_type"] == "uses"
    assert kept["origin_fact_id"] == _fact()["origin_fact_id"]


def test_full_keeps_the_author_and_the_provenance():
    kept = redact.apply(redact.POLICIES["full"], _fact())
    assert kept is not None
    assert kept["agent_id"] == "claude-code"
    assert kept["provenance"] == {"session_id": "s1", "source_episode_id": "e1"}


def test_full_still_refuses_a_secret():
    """Nobody intends to export an AWS key, and a policy that permits it
    because it is named "full" is a policy that exports one."""
    assert redact.apply(
        redact.POLICIES["full"], _fact(fact="the key is AKIAIOSFODNN7EXAMPLE")
    ) is None


def test_the_default_policy_is_the_restrictive_one():
    """Forgetting to choose must not export more than choosing would."""
    assert redact.get(None).policy_id == "minimal"


def test_an_unknown_policy_id_is_an_error_rather_than_a_fallback():
    with pytest.raises(KeyError, match="no redaction policy"):
        redact.get("mimimal")


# --- audit --------------------------------------------------------------


def test_audit_counts_each_rule_separately():
    """"11 dropped" and "11 dropped, all because a pattern matched an address"
    lead to different decisions, and only the second says the scope has a
    problem that is not about this export."""
    policy = redact.Policy(
        policy_id="t", drop_relations=frozenset({"pays"}),
        drop_entity_types=frozenset({"person"}),
        drop_matching=redact.POLICIES["minimal"].drop_matching,
        keep_agent_id=False, keep_provenance=False,
    )
    facts = [
        _fact(relation_type="pays"),
        _fact(relation_type="pays"),
        _fact(source_type="person"),
        _fact(fact="mail ayush@example.com"),
        _fact(),
        _fact(fact="deploys run from main"),
    ]

    report = redact.audit(policy, facts)

    assert report["n_in"] == 6
    assert report["n_out"] == 2
    assert report["n_dropped"] == 4
    assert report["dropped"] == {"relation": 2, "entity_type": 1, "matching": 1}
    assert report["relations_dropped"] == ["pays"]
    assert report["entity_types_dropped"] == ["person"]


def test_audit_counts_what_was_stripped_from_the_facts_that_stayed():
    report = redact.audit(redact.POLICIES["minimal"], [_fact(), _fact()])
    assert report["stripped"] == {"agent_id": 2, "provenance": 2}
    assert report["n_out"] == 2


def test_audit_does_not_count_a_strip_on_a_dropped_fact():
    """A dropped fact is accounted for once, by the rule that dropped it.
    Counting it twice would make the totals not add up, and an operator
    checking the arithmetic is exactly the reader this is for."""
    report = redact.audit(redact.POLICIES["minimal"], [_fact(source_type="person")])
    assert report["dropped"]["entity_type"] == 1
    assert report["stripped"] == {"agent_id": 0, "provenance": 0}
    assert report["n_out"] == 0


def test_audit_agrees_with_apply():
    """One decision behind both, so the audit an operator reads cannot
    under-report what the export does."""
    policy = redact.POLICIES["minimal"]
    facts = [
        _fact(),
        _fact(source_type="person"),
        _fact(fact="mail ayush@example.com"),
        _fact(fact="deploys run from main"),
    ]
    report = redact.audit(policy, facts)
    kept = [f for f in (redact.apply(policy, f) for f in facts) if f is not None]
    assert report["n_out"] == len(kept)
    assert report["n_dropped"] == len(facts) - len(kept)


def test_audit_previews_the_text_of_what_is_being_held_back():
    report = redact.audit(redact.POLICIES["minimal"], [_fact(source_type="person")])
    assert report["preview"] == [_fact()["fact"]]
