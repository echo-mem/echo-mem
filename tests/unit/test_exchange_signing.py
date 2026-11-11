"""A verifier that trusts the wrong thing is worse than one that trusts nothing.

Three of these tests are about refusal rather than about cryptography, and they
are the ones worth having. The ed25519 round trip exercises somebody else's
well-tested library. What this module can get wrong is accepting a bundle it
should not: an unknown key waved through, an empty anchor set treated as "no
objections", or - the one that is a real attack rather than a theoretical
one - letting the bundle choose the algorithm so that a published ed25519
public key can be replayed as an HMAC shared secret.
"""

from __future__ import annotations

import pytest

from echo_memory.exchange import bundle, sign

SEED = "11" * 32
OTHER_SEED = "22" * 32
HMAC_SECRET = "ab" * 24

ENV = {
    sign.KEYS_ENV: f"k1:{SEED},k2:{OTHER_SEED},h1:{HMAC_SECRET}",
    sign.KEY_ID_ENV: "k1",
}


def _manifest(**overrides) -> bundle.Manifest:
    body = {
        "format_version": bundle.FORMAT_VERSION,
        "bundle_id": "b-1",
        "deployment_id": "dep-a",
        "account": "acct-a",
        "memory": "shared",
        "created_at": "2026-11-11T00:00:00+00:00",
        "redaction_policy_id": "minimal",
        "fact_count": 3,
        "content_digest": "sha256:" + "0" * 64,
        "source_chain_head": {"seq": 12, "digest": "sha256:beef", "keyed": False},
    }
    body.update(overrides)
    return bundle.Manifest(**body)


def _signed(alg: str = sign.ALG_ED25519, key_id: str = "k1") -> bundle.Manifest:
    manifest = _manifest()
    env = {**ENV, sign.KEY_ID_ENV: key_id}
    return manifest.signed(sign.sign(manifest.unsigned(), alg=alg, env=env))


# --- round trip ---------------------------------------------------------


def test_ed25519_is_available_here():
    """Stated as a test rather than assumed, so a deployment that lost
    `cryptography` finds out from this file instead of from a partner who
    cannot verify its bundles."""
    assert sign.HAVE_ED25519
    assert sign.preferred_alg() == sign.ALG_ED25519


def test_sign_and_verify_round_trip_ed25519():
    manifest = _signed()
    trusted = {"k1": sign.trust_anchor("k1", env=ENV)}

    assert sign.verify(manifest, trusted_keys=trusted)
    ok, reason = sign.check(manifest, trusted_keys=trusted)
    assert ok and "ed25519" in reason


def test_sign_and_verify_round_trip_hmac():
    """The documented fallback for a deployment without `cryptography`, and it
    has to work on a deployment that has it or nobody would find out."""
    manifest = _signed(alg=sign.ALG_HMAC, key_id="h1")
    trusted = {"h1": sign.TrustedKey("h1", sign.ALG_HMAC, bytes.fromhex(HMAC_SECRET))}

    ok, reason = sign.check(manifest, trusted_keys=trusted)
    assert ok
    assert "no more" in reason, "an HMAC pass must not imply it identified the sender"


def test_the_signature_names_the_key_that_made_it():
    signature = _signed().signature
    assert signature is not None
    assert signature["key_id"] == "k1"
    assert signature["alg"] == sign.ALG_ED25519


def test_an_ed25519_anchor_is_a_public_key_and_not_the_seed():
    """It is published, so it had better not be the private key."""
    anchor = sign.trust_anchor("k1", env=ENV)
    assert anchor.alg == sign.ALG_ED25519
    assert anchor.material != bytes.fromhex(SEED)
    assert len(anchor.material) == sign.ED25519_KEY_BYTES


# --- what verification refuses ------------------------------------------


def test_a_tampered_manifest_fails():
    """Every field is covered, not just the digest. A bundle must not be
    re-labelled with a different account and keep its signature."""
    manifest = _signed()
    trusted = {"k1": sign.trust_anchor("k1", env=ENV)}

    altered = bundle.Manifest(**{**manifest.to_dict(), "account": "acct-victim"})
    assert sign.verify(altered, trusted_keys=trusted) is False
    _ok, reason = sign.check(altered, trusted_keys=trusted)
    assert "does not match the manifest" in reason


def test_a_tampered_content_digest_fails():
    """The field that binds the manifest to the facts. Swapping it is how one
    would pair a valid manifest with somebody else's claims."""
    manifest = _signed()
    trusted = {"k1": sign.trust_anchor("k1", env=ENV)}
    altered = bundle.Manifest(
        **{**manifest.to_dict(), "content_digest": "sha256:" + "f" * 64}
    )
    assert sign.verify(altered, trusted_keys=trusted) is False


def test_an_unknown_key_id_is_refused_with_a_reason():
    """Never a pass. Accepting an unknown key would make the anchor set
    decorative."""
    manifest = _signed(key_id="k2")
    trusted = {"k1": sign.trust_anchor("k1", env=ENV)}

    ok, reason = sign.check(manifest, trusted_keys=trusted)
    assert ok is False
    assert "k2" in reason
    assert "not a trusted key" in reason


def test_an_empty_trusted_set_refuses_everything():
    """Including a correctly signed bundle. A verifier that trusts nothing has
    verified nothing, and the honest answer is a refusal rather than a pass."""
    manifest = _signed()
    assert sign.verify(manifest, trusted_keys={}) is False
    _ok, reason = sign.check(manifest, trusted_keys={})
    assert "no trusted keys" in reason


def test_an_unsigned_bundle_is_refused():
    ok, reason = sign.check(
        _manifest(), trusted_keys={"k1": sign.trust_anchor("k1", env=ENV)}
    )
    assert ok is False
    assert "unsigned" in reason


def test_the_right_key_for_the_wrong_manifest_is_refused():
    """The same signature value lifted onto a different bundle."""
    first = _signed()
    second = _manifest(bundle_id="b-2")
    assert first.signature is not None
    forged = second.signed(first.signature)
    trusted = {"k1": sign.trust_anchor("k1", env=ENV)}
    assert sign.verify(forged, trusted_keys=trusted) is False


def test_a_published_ed25519_anchor_cannot_be_replayed_as_an_hmac_secret():
    """The attack the algorithm check exists for.

    An ed25519 trust anchor is public by design. If the bundle chose the
    algorithm, anybody holding that public key could use it as an HMAC shared
    secret, sign their own claims with alg hmac-sha256, and a verifier would
    compute the same HMAC and pass it. The algorithm comes from the anchor.
    """
    anchor = sign.trust_anchor("k1", env=ENV)
    attacker_env = {
        sign.KEYS_ENV: f"k1:{anchor.material.hex()}",
        sign.KEY_ID_ENV: "k1",
    }
    manifest = _manifest(account="acct-attacker")
    forged = manifest.signed(
        sign.sign(manifest.unsigned(), alg=sign.ALG_HMAC, env=attacker_env)
    )

    ok, reason = sign.check(forged, trusted_keys={"k1": anchor})
    assert ok is False
    assert "comes from the trust anchor" in reason


def test_an_incomplete_signature_is_refused():
    manifest = _manifest().signed({"alg": sign.ALG_ED25519, "key_id": "k1"})
    ok, reason = sign.check(
        manifest, trusted_keys={"k1": sign.trust_anchor("k1", env=ENV)}
    )
    assert ok is False
    assert "incomplete" in reason


def test_a_non_hex_signature_value_is_refused_rather_than_raising():
    manifest = _manifest().signed(
        {"alg": sign.ALG_ED25519, "key_id": "k1", "value": "not-hex"}
    )
    ok, reason = sign.check(
        manifest, trusted_keys={"k1": sign.trust_anchor("k1", env=ENV)}
    )
    assert ok is False
    assert "not hex" in reason


# --- what signing refuses -----------------------------------------------


def test_signing_with_no_key_configured_says_what_to_set():
    with pytest.raises(sign.SignatureError, match=sign.KEYS_ENV):
        sign.sign(_manifest().unsigned(), env={})


def test_signing_without_naming_the_current_key_is_refused():
    """Picking one implicitly makes a key rotation invisible."""
    with pytest.raises(sign.SignatureError, match=sign.KEY_ID_ENV):
        sign.sign(_manifest().unsigned(), env={sign.KEYS_ENV: f"k1:{SEED}"})


def test_a_seed_of_the_wrong_length_is_refused_before_signing():
    env = {sign.KEYS_ENV: "k1:aabb", sign.KEY_ID_ENV: "k1"}
    with pytest.raises(sign.SignatureError, match="ed25519 private seed is 32"):
        sign.sign(_manifest().unsigned(), alg=sign.ALG_ED25519, env=env)


def test_a_placeholder_hmac_secret_is_refused():
    env = {sign.KEYS_ENV: "h1:aabbccdd", sign.KEY_ID_ENV: "h1"}
    with pytest.raises(sign.SignatureError, match="refusing to sign"):
        sign.sign(_manifest().unsigned(), alg=sign.ALG_HMAC, env=env)


def test_a_malformed_key_spec_names_the_entry():
    with pytest.raises(sign.SignatureError, match="not key_id:hexkey"):
        sign.signing_keys({sign.KEYS_ENV: "justakey"})


def test_parse_trusted_requires_the_algorithm_to_be_declared():
    """A 32 byte HMAC secret and an ed25519 public key are both 32 bytes, so
    the length cannot be used to infer which this is."""
    with pytest.raises(sign.SignatureError, match="not key_id:alg:hexkey"):
        sign.parse_trusted(f"k1:{SEED}")

    anchors = sign.parse_trusted(f"k1:ed25519:{SEED},h1:hmac-sha256:{HMAC_SECRET}")
    assert anchors["k1"].alg == sign.ALG_ED25519
    assert anchors["h1"].alg == sign.ALG_HMAC


def test_an_anchor_says_what_its_signatures_are_worth():
    """So a report cannot imply the stronger claim for an HMAC key."""
    assert "holder of the private key" in sign.trust_anchor("k1", env=ENV).describe()
    hmac_anchor = sign.TrustedKey("h1", sign.ALG_HMAC, bytes.fromhex(HMAC_SECRET))
    assert "includes you" in hmac_anchor.describe()
