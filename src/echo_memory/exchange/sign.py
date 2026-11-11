"""A signature over the manifest, checked against an anchor the verifier holds.

The manifest carries the facts blob's digest, so one signature over one small
object covers the whole bundle. What this module is careful about is not the
cryptography - it is where the trust comes from.

## verify takes its anchors as an argument, and that is the whole design

`verify` cannot read a trusted key set from the environment, from the database,
or from the bundle. It takes one, explicitly, from the caller.

A verifier that reads its trust anchors from the same place the bundle came
from is not verifying anything. It is confirming that a file is internally
consistent, which anybody who wrote the file can arrange. The signature is
only worth something if the key was obtained over a channel the sender of the
bundle does not control - published on the partner's own domain, read out on a
call, pasted into a signed contract - and that channel is the operator's
problem, which is exactly why this module refuses to guess at it.

So there is no default. An empty trusted set refuses every bundle, including a
correctly signed one, because a verifier that trusts nothing has verified
nothing.

## The algorithm comes from the anchor, never from the bundle

Each `TrustedKey` declares its own algorithm, and a signature whose `alg`
disagrees with the anchor's is refused before any comparison.

That check is the one that stops a real attack rather than a theoretical one.
An ed25519 trust anchor is a PUBLIC key - published, by design, to anybody who
asks. If a verifier let the bundle choose the algorithm, an attacker could
take that public key, use it as an HMAC shared secret, sign a bundle of their
own claims with `alg: hmac-sha256`, and the verifier would compute the same
HMAC and pass it. Trusting the anchor's declared algorithm and not the file's
is what makes a published key safe to publish.

## What a signature here proves, and what it does not

**It proves the holder of a key produced these bytes.** Change one character of
a claim and the digest changes, the manifest changes, and the signature fails.

**It does not prove the claims are true, or extracted honestly.** A partner can
sign a bundle of inventions. The signature binds the bundle to a sender; it
says nothing about how the sender came by what is in it. That is why imported
facts land in quarantine at `confidence="inferred"` whatever their signature
says, and why promotion needs a human verifier rather than a passing check.

**With HMAC it does not prove WHO.** An HMAC key is shared, so the verifier can
produce any signature the signer could. It establishes that somebody holding
the shared secret produced the bundle, which between two parties who each
already trust the other is often enough, and which is worth nothing as evidence
to a third party. ed25519 does not have that property and is preferred wherever
it is available. `describe` says which one a given anchor gives you rather than
letting a report imply the stronger claim.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Mapping
from dataclasses import dataclass

from echo_memory.exchange.bundle import Manifest, canonical

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

    HAVE_ED25519 = True
except ImportError:  # pragma: no cover - exercised by a deployment without it
    HAVE_ED25519 = False

#: Signing keys, as `key_id:hexkey,key_id:hexkey`. For ed25519 the hex is a 32
#: byte private seed; for HMAC it is the shared secret. Read from the
#: environment and never from the database, for the reason
#: `attestation.KEY_ENV` gives: a key stored next to what it protects protects
#: nothing.
KEYS_ENV = "ECHO_MEMORY_EXCHANGE_KEYS"

#: Which of them signs. Explicit rather than "the only one", so that rotating
#: a key is adding the new one and then moving this, with both verifiable
#: throughout.
KEY_ID_ENV = "ECHO_MEMORY_EXCHANGE_KEY_ID"

ALG_ED25519 = "ed25519"
ALG_HMAC = "hmac-sha256"
ALGORITHMS = (ALG_ED25519, ALG_HMAC)

#: An ed25519 private seed and public key are both exactly this long.
ED25519_KEY_BYTES = 32

#: The shortest HMAC secret this will use. Not a cryptographic threshold - it
#: is the line below which a key is obviously a placeholder somebody meant to
#: replace, and signing with one of those is worse than not signing.
MIN_HMAC_KEY_BYTES = 16


class SignatureError(Exception):
    """A key or a signature is unusable, and the message says which."""


@dataclass(frozen=True)
class TrustedKey:
    """One anchor: a key id, the algorithm it may be used with, and the key.

    The algorithm belongs here rather than on the signature because this is the
    side the verifier controls. See the module note on algorithm confusion.
    """

    key_id: str
    alg: str
    #: An ed25519 public key, or an HMAC shared secret.
    material: bytes

    def describe(self) -> str:
        """What this anchor's signatures are worth, in one phrase, so a report
        cannot imply the stronger claim."""
        if self.alg == ALG_ED25519:
            return (
                f"{self.key_id}: ed25519, so a signature could only have been "
                f"made by the holder of the private key"
            )
        return (
            f"{self.key_id}: hmac-sha256 over a shared secret, so a signature "
            f"proves somebody holding that secret made this bundle - which "
            f"includes you, and is therefore not evidence to a third party"
        )


def preferred_alg() -> str:
    """ed25519 when `cryptography` is installed, HMAC otherwise.

    HMAC is a fallback rather than a choice: it cannot identify a sender to
    anybody but the two parties sharing the key. A deployment that falls back
    should know it did, which is what `describe` is for.
    """
    return ALG_ED25519 if HAVE_ED25519 else ALG_HMAC


def _parse_keys(spec: str) -> dict[str, bytes]:
    keys: dict[str, bytes] = {}
    for entry in spec.split(","):
        entry = entry.strip()
        if not entry:
            continue
        key_id, _, hexkey = entry.partition(":")
        if not key_id or not hexkey:
            raise SignatureError(
                f"{KEYS_ENV} entry {entry!r} is not key_id:hexkey"
            )
        try:
            keys[key_id.strip()] = bytes.fromhex(hexkey.strip())
        except ValueError:
            raise SignatureError(
                f"{KEYS_ENV} entry for {key_id!r} is not hex"
            ) from None
    return keys


def signing_keys(env: Mapping[str, str] | None = None) -> dict[str, bytes]:
    """Every key this deployment can sign with, by id."""
    env = env if env is not None else os.environ
    return _parse_keys(env.get(KEYS_ENV, ""))


def current_key_id(env: Mapping[str, str] | None = None) -> str | None:
    env = env if env is not None else os.environ
    return env.get(KEY_ID_ENV) or None


def _signing_key(env: Mapping[str, str] | None, alg: str) -> tuple[str, bytes]:
    keys = signing_keys(env)
    if not keys:
        raise SignatureError(
            f"no signing key: set {KEYS_ENV} to key_id:hexkey[,...]. An "
            f"unsigned bundle can be built, but no importer will accept one."
        )
    key_id = current_key_id(env)
    if key_id is None:
        raise SignatureError(
            f"set {KEY_ID_ENV} to which of {', '.join(sorted(keys))} signs. "
            f"Picking one implicitly makes a key rotation invisible."
        )
    if key_id not in keys:
        raise SignatureError(
            f"{KEY_ID_ENV} is {key_id!r}, which is not in {KEYS_ENV} "
            f"({', '.join(sorted(keys)) or 'empty'})"
        )
    material = keys[key_id]
    if alg == ALG_ED25519 and len(material) != ED25519_KEY_BYTES:
        raise SignatureError(
            f"key {key_id!r} is {len(material)} bytes; an ed25519 private "
            f"seed is {ED25519_KEY_BYTES}"
        )
    if alg == ALG_HMAC and len(material) < MIN_HMAC_KEY_BYTES:
        raise SignatureError(
            f"key {key_id!r} is {len(material)} bytes; refusing to sign with "
            f"fewer than {MIN_HMAC_KEY_BYTES}"
        )
    return key_id, material


def sign(
    manifest_without_sig: dict,
    *,
    alg: str | None = None,
    env: Mapping[str, str] | None = None,
) -> dict:
    """Sign a manifest body, returning {"alg", "key_id", "value"}.

    The body must be the manifest WITHOUT its signature field, which is what
    `Manifest.unsigned` returns. A signature cannot cover itself, and the
    bytes signed are `canonical(body)` so that the verifier reconstructs
    exactly what was signed from the manifest it received.
    """
    alg = alg or preferred_alg()
    if alg not in ALGORITHMS:
        raise SignatureError(f"unknown algorithm {alg!r}, expected one of {ALGORITHMS}")
    if alg == ALG_ED25519 and not HAVE_ED25519:
        raise SignatureError(
            "ed25519 needs the `cryptography` package, which is not installed "
            f"here. Pass alg={ALG_HMAC!r} to sign with a shared secret instead."
        )

    key_id, material = _signing_key(env, alg)
    body = canonical(manifest_without_sig)

    if alg == ALG_ED25519:
        value = Ed25519PrivateKey.from_private_bytes(material).sign(body).hex()
    else:
        value = hmac.new(material, body, hashlib.sha256).hexdigest()
    return {"alg": alg, "key_id": key_id, "value": value}


def trust_anchor(key_id: str, *, env: Mapping[str, str] | None = None) -> TrustedKey:
    """The anchor an exporter publishes so a partner can verify its bundles.

    For ed25519 this derives the PUBLIC key, which is safe to publish anywhere.
    For HMAC there is nothing to derive - the anchor is the shared secret - so
    this returns it and the caller must deliver it over a channel the bundle
    does not travel on. Publishing an HMAC anchor lets anybody sign as you.
    """
    keys = signing_keys(env)
    if key_id not in keys:
        raise SignatureError(f"no signing key {key_id!r} in {KEYS_ENV}")
    material = keys[key_id]
    if HAVE_ED25519 and len(material) == ED25519_KEY_BYTES:
        public = Ed25519PrivateKey.from_private_bytes(material).public_key()
        return TrustedKey(
            key_id=key_id,
            alg=ALG_ED25519,
            material=public.public_bytes_raw(),
        )
    return TrustedKey(key_id=key_id, alg=ALG_HMAC, material=material)


def parse_trusted(spec: str) -> dict[str, TrustedKey]:
    """Trust anchors from `key_id:alg:hexkey[,...]`, for the CLI.

    The algorithm is in the spec because the operator supplying the anchor is
    the one who knows what it is. A format that left it out would have to infer
    it from the key's length, and a 32 byte HMAC secret and an ed25519 public
    key are both 32 bytes - which is precisely the confusion the verifier
    refuses to be exposed to.
    """
    anchors: dict[str, TrustedKey] = {}
    for entry in spec.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        if len(parts) != 3:
            raise SignatureError(
                f"trusted key {entry!r} is not key_id:alg:hexkey"
            )
        key_id, alg, hexkey = (p.strip() for p in parts)
        if alg not in ALGORITHMS:
            raise SignatureError(
                f"trusted key {key_id!r} names algorithm {alg!r}, expected one "
                f"of {ALGORITHMS}"
            )
        try:
            material = bytes.fromhex(hexkey)
        except ValueError:
            raise SignatureError(f"trusted key {key_id!r} is not hex") from None
        anchors[key_id] = TrustedKey(key_id=key_id, alg=alg, material=material)
    return anchors


def check(
    manifest: Manifest,
    *,
    trusted_keys: Mapping[str, TrustedKey],
) -> tuple[bool, str]:
    """Whether this manifest's signature holds, and why.

    Returns a reason either way, because every caller has to report one. An
    importer that can only say "rejected" leaves an operator unable to tell a
    bundle signed with a key they have not been given from a bundle somebody
    has altered, and those have opposite responses.
    """
    signature = manifest.signature
    if not signature:
        return False, (
            "the bundle is unsigned. An unsigned bundle has no sender, so "
            "there is nothing to trust and nothing to hold responsible."
        )
    if not trusted_keys:
        return False, (
            "no trusted keys were supplied, so no signature can be accepted. "
            "A verifier with an empty anchor set has verified nothing, and "
            "this refuses rather than treating that as a pass."
        )

    key_id = signature.get("key_id")
    alg = signature.get("alg")
    value = signature.get("value")
    if not (key_id and alg and value):
        return False, (
            f"the signature is incomplete: alg={alg!r}, key_id={key_id!r}, "
            f"value present={bool(value)}"
        )

    anchor = trusted_keys.get(str(key_id))
    if anchor is None:
        return False, (
            f"signed with key {key_id!r}, which is not a trusted key here "
            f"(trusted: {', '.join(sorted(trusted_keys)) or 'none'}). An "
            f"unknown key is a refusal, never a pass: accepting one would "
            f"make the anchor set decorative."
        )

    # Before any comparison. See the module note: an ed25519 anchor is public,
    # so letting the bundle choose HMAC would let anyone holding that public
    # key sign as its owner.
    if anchor.alg != alg:
        return False, (
            f"key {key_id!r} is trusted for {anchor.alg} and this bundle is "
            f"signed with {alg}. The algorithm comes from the trust anchor, "
            f"not from the bundle."
        )

    body = canonical(manifest.unsigned())
    try:
        raw = bytes.fromhex(str(value))
    except ValueError:
        return False, f"the signature value for key {key_id!r} is not hex"

    if alg == ALG_ED25519:
        if not HAVE_ED25519:
            return False, (
                "this bundle is signed with ed25519 and `cryptography` is not "
                "installed here, so the signature cannot be checked. Not "
                "accepted: an unverifiable signature is not a verified one."
            )
        try:
            Ed25519PublicKey.from_public_bytes(anchor.material).verify(raw, body)
        except InvalidSignature:
            return False, _altered(key_id)
        except ValueError as e:
            return False, f"trusted key {key_id!r} is not a usable ed25519 public key: {e}"
        return True, f"ed25519 signature by {key_id!r} is valid"

    expected = hmac.new(anchor.material, body, hashlib.sha256).digest()
    # Constant time, so that a verifier cannot be used as an oracle to
    # discover a valid signature byte by byte.
    if not hmac.compare_digest(expected, raw):
        return False, _altered(key_id)
    return True, (
        f"hmac-sha256 signature by {key_id!r} is valid, which proves a holder "
        f"of that shared secret built this bundle and no more"
    )


def _altered(key_id) -> str:
    return (
        f"the signature does not match the manifest. Something changed after "
        f"{key_id!r} signed it, or it was signed for a different manifest. "
        f"Nothing from this bundle is written."
    )


def verify(manifest: Manifest, *, trusted_keys: Mapping[str, TrustedKey]) -> bool:
    """Whether this manifest's signature holds, against anchors the caller
    supplies.

    `trusted_keys` is a required keyword argument and there is no default. A
    verifier that read its trust anchors from the same place the bundle came
    from would not be verifying anything; see the module note.

    `check` returns the same answer with the reason attached, and every caller
    that reports to a person should use that instead.
    """
    ok, _reason = check(manifest, trusted_keys=trusted_keys)
    return ok
