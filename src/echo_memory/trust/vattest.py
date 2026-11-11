"""A digest per verification, chained, so that no change to one is silent.

`attestation` protects what a fact says about itself. This protects the record
of who checked it. The two need the same property for the same reason: a
verification is worth something precisely because it cannot be edited later by
whoever finds the answer inconvenient, and a row saying `verified` by a named
human on a named policy is the single most attractive row in this schema to
somebody with SQL access.

## Why this is a second chain and not more rows in fact_attestation

`fact_attestation` is dense per scope and `echo-memory verify` recomputes the
whole of it. Appending verifications to it would mean every check shifts the
sequence numbers the facts' own digests chain through, so recording a
verification would alter the evidence about the claim being verified. Two
chains, independent, and neither can perturb the other.

The serialisation is reimplemented here rather than imported from
`attestation`, which looks like duplication and is not. A shared canonical
form means a change made for one chain silently rewrites every digest in the
other, and the whole point of a chain is that its past does not move.

## Keys, and rotation

`ECHO_MEMORY_VERIFICATION_KEYS` holds `key_id:hexkey` pairs, comma separated,
so more than one key can be known at once. That is what makes rotation
possible: a new key signs from now on, and old links are verified with the
`key_id` recorded on them. Without the per-link key_id, rotating a key would
make every verification in history read as forged - which is the same thing as
having no rotation at all, because nobody rotates a key that invalidates their
own audit trail.

A key_id that is no longer configured is reported as unverifiable rather than
as altered. Not being able to check a link is not evidence that it was
changed, and a report that conflates those two trains a reader to ignore it.

## What this proves

The same two things `attestation` proves and in the same words, because
nothing here is stronger. Keyed, a digest cannot be recomputed without the
key. Unkeyed, the chain is still a chain and any change alters every digest
after it, so a head recorded outside this database makes a silent rewrite
impossible. With neither, this detects corruption and careless edits and
nothing else. `report().strength` says which is in force rather than implying
a guarantee that is not there.

It does not authenticate the verifier. `verifier_id` is a string the caller
supplies, so this proves the recorded verifier has not changed since the row
was written, not that it was true when it was.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass, field

from echo_memory.infra.logging import get_logger

_logger = get_logger("vattest")

#: Where the signing keys are read from, as `key_id:hexkey,key_id2:hexkey2`.
#: Deliberately not in the database: a key stored next to what it protects
#: protects nothing.
KEYS_ENV = "ECHO_MEMORY_VERIFICATION_KEYS"

#: Which of those keys signs from now on. Absent, the first entry signs, so a
#: single-key deployment needs one variable rather than two.
KEY_ID_ENV = "ECHO_MEMORY_VERIFICATION_KEY_ID"

#: The digest definition this code implements. Recorded on every link because
#: the covered fields of a verification will change - `scope` was added to
#: this design twice while it was being written - and a chain whose definition
#: moved without a version reports its entire history as altered.
CHAIN_VERSION = 1

#: What the first link in a chain chains to. A literal rather than an empty
#: string, so a row whose prev_digest was blanked is distinguishable from the
#: genuine first link.
GENESIS = "genesis"

#: The key_id recorded when there is no key. A name rather than NULL, so
#: "signed with no key" and "we forgot to record which key" cannot be read as
#: the same thing.
UNKEYED = "unkeyed"

#: The advisory lock namespace. Distinct from write_episode's `episode|<gid>`
#: and from `attestation`'s bare `<gid>` on purpose: sharing either would make
#: recording a verification block a write, or worse, make a verification's
#: lock look taken when it is a fact's.
LOCK_PREFIX = "verify|"

#: The verification columns the digest covers, in this order. Everything a
#: verification asserts, plus the two timestamps.
#:
#: `verified_at` is covered so a check cannot be backdated into an interval it
#: was not made in, and `expires_at` so a lapsed check cannot be quietly
#: extended - which is the cheapest useful edit available against this table
#: and would leave a `stale` fact reading as `verified` with nothing out of
#: place.
COVERED = (
    "edge_id",
    "state",
    "verifier_kind",
    "verifier_id",
    "method",
    "policy_id",
    "evidence_ids",
    "scope",
    "verified_at",
    "expires_at",
)


class VAttestError(Exception):
    """The request names something that cannot be done, and why."""


def keys() -> dict[str, str]:
    """The configured keys, by key_id, in the order the variable names them.

    A malformed entry raises rather than being skipped. Skipping it would
    produce a chain that is unkeyed because of a missing colon somewhere in an
    environment variable, described as keyed by whoever set it, and this
    project has already shipped two settings that were right while the data
    they governed was absent and nothing errored.
    """
    raw = os.environ.get(KEYS_ENV, "").strip()
    if not raw:
        return {}
    out: dict[str, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        key_id, _, material = entry.partition(":")
        if not key_id or not material:
            raise VAttestError(
                f"{KEYS_ENV} entry {entry!r} is not key_id:hexkey; a chain "
                f"signed with no key because of a typo would still be "
                f"described as keyed"
            )
        out[key_id.strip()] = material.strip()
    return out


def current() -> tuple[str, str | None]:
    """The key signing from now on, as (key_id, material).

    `(UNKEYED, None)` when nothing is configured. A named key that is not in
    the variable raises rather than falling back to the first one: signing
    with a key other than the one asked for is the failure rotation exists to
    avoid, and it would be invisible.
    """
    configured = keys()
    if not configured:
        return (UNKEYED, None)
    want = os.environ.get(KEY_ID_ENV, "").strip()
    if want:
        if want not in configured:
            raise VAttestError(
                f"{KEY_ID_ENV}={want!r} is not in {KEYS_ENV} "
                f"({list(configured)})"
            )
        return (want, configured[want])
    first = next(iter(configured))
    return (first, configured[first])


def alg_of(material: str | None) -> str:
    return "hmac-sha256" if material else "sha256"


def canonical(claims: dict) -> bytes:
    """The bytes a digest is taken over.

    JSON with sorted keys and no insignificant whitespace, because the digest
    has to be reproducible by a different version of this code on a different
    machine. Anything that lets two serialisations of the same row differ
    turns a verification failure into a coin flip.
    """
    return json.dumps(claims, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode()


def digest(prev: str, body: bytes, material: str | None) -> str:
    """One link. HMAC with a key, plain SHA-256 without one.

    The previous digest goes inside the hash rather than beside it, which is
    what makes this a chain: a changed link invalidates every link after it,
    so tampering with one verification means recomputing the whole tail.
    """
    payload = prev.encode() + b"\n" + body
    if material:
        return "hmac-sha256:" + hmac.new(
            material.encode(), payload, hashlib.sha256
        ).hexdigest()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def claims_of(conn, group_id: str, verification_id: int) -> dict:
    """What a verification asserts, read back from the table.

    The timestamps are rendered by the database in a fixed UTC format rather
    than cast to text, because `timestamptz::text` depends on the session's
    TimeZone and DateStyle. Two readers with different settings would compute
    different digests over an unchanged row, which is the coin flip `canonical`
    exists to prevent.
    """
    row = conn.execute(
        """SELECT edge_id::text, state, verifier_kind, verifier_id, method,
                  policy_id, evidence_ids, scope,
                  to_char(verified_at AT TIME ZONE 'UTC',
                          'YYYY-MM-DD HH24:MI:SS.US'),
                  to_char(expires_at AT TIME ZONE 'UTC',
                          'YYYY-MM-DD HH24:MI:SS.US')
             FROM public.fact_verification
            WHERE id = %s AND group_id = %s""",
        (verification_id, group_id),
    ).fetchone()
    if row is None:
        raise VAttestError(f"no verification {verification_id} in {group_id}")
    claims = dict(zip(COVERED, row, strict=True))
    claims["group_id"] = group_id
    return claims


def head(conn, group_id: str) -> tuple[int, str]:
    """This scope's last link: its sequence number and digest.

    The value worth recording somewhere outside the database. One string makes
    every verification in the scope unrewritable, because changing any of them
    changes this.

    Scoped to `CHAIN_VERSION`, so a future definition starts its own chain
    rather than appending to one whose digests it cannot reproduce.
    """
    row = conn.execute(
        """SELECT seq, digest FROM public.verification_attestation
            WHERE group_id = %s AND chain_version = %s
            ORDER BY seq DESC LIMIT 1""",
        (group_id, CHAIN_VERSION),
    ).fetchone()
    return (row[0], row[1]) if row else (0, GENESIS)


def attest(conn, group_id: str, verification_id: int) -> str:
    """Append a verification to its scope's chain, and return the new digest.

    Called from `verify.record`, in the same transaction as the row it
    attests. Two writers appending to one scope at once would otherwise read
    the same head and fork the chain, and a fork lets a verification pick the
    branch that passes - so this takes a per-scope advisory lock first.

    Per scope rather than global because scopes are tenants: one account's
    verifications must not be able to block another's. On a key distinct from
    the two locks already in this codebase, so that recording a verification
    and writing an episode do not wait on each other.
    """
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext(%s))",
        (LOCK_PREFIX + group_id,),
    )
    seq, prev = head(conn, group_id)
    key_id, material = current()
    body = canonical(claims_of(conn, group_id, verification_id))
    value = digest(prev, body, material)
    conn.execute(
        """INSERT INTO public.verification_attestation
               (verification_id, group_id, chain_version, seq, prev_digest,
                digest, alg, key_id)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (verification_id, group_id, CHAIN_VERSION, seq + 1, prev, value,
         alg_of(material), key_id),
    )
    return value


@dataclass
class Report:
    """What verification of the chain found. Every list is a separate kind of
    finding, because they have different causes and different answers."""

    group_id: str
    chain_version: int = CHAIN_VERSION
    n_attested: int = 0
    keyed: bool = False
    #: Part keyed and part not. Different from rotation, which is several
    #: key_ids all keyed, and the difference matters: rotation is the system
    #: working, mixed keying means part of the chain can be recomputed by
    #: anybody with SQL access.
    mixed_keying: bool = False
    #: Every key that signed part of this chain, in chain order. Rotation is
    #: visible here rather than inferred.
    key_ids: list[str] = field(default_factory=list)
    #: Verifications whose digest no longer matches their content.
    altered: list[dict] = field(default_factory=list)
    #: Links whose prev_digest does not match the digest before them.
    broken_links: list[dict] = field(default_factory=list)
    #: Links whose verification row is gone. Nothing deletes from
    #: `fact_verification` by design, so unlike a missing fact there is no
    #: erasure that accounts for this.
    vanished: list[int] = field(default_factory=list)
    #: Links signed with a key_id this process does not have. Not a finding
    #: about the data: it is this report declining to claim something it
    #: cannot check.
    unverifiable: list[dict] = field(default_factory=list)

    def is_clean(self) -> bool:
        """Whether anything was found wrong. Deliberately not false for
        `unverifiable`: a link that could not be checked is an absence of
        evidence, and reporting it as tampering would be a false accusation
        every time a key is retired."""
        return not (self.altered or self.broken_links or self.vanished)

    @property
    def strength(self) -> str:
        """What this chain is actually worth, in one phrase.

        A report that said "verified" without this is the line a reader would
        quote, and unkeyed and unwitnessed it would be wrong.
        """
        caveat = ""
        if self.unverifiable:
            caveat = (
                f"; {len(self.unverifiable)} link(s) were signed with a key "
                f"this process does not have and were not checked at all"
            )
        if self.mixed_keying:
            return (
                "part of this chain is keyed and part is not; the unkeyed part "
                "is only as strong as an external record of the head" + caveat
            )
        if self.keyed:
            return (
                f"keyed, so a digest cannot be recomputed without the matching "
                f"key in {KEYS_ENV}" + caveat
            )
        return (
            "unkeyed, so this is only as strong as a record of the head kept "
            "outside this database" + caveat
        )


def report(conn, group_id: str) -> Report:
    """Verify a scope's verification chain, and say what the result is worth.

    Three separate questions, because an answer that merges them cannot be
    acted on: has any verification changed, is the chain continuous, and is
    every link's row still there.
    """
    rows = conn.execute(
        """SELECT verification_id, seq, prev_digest, digest, alg, key_id
             FROM public.verification_attestation
            WHERE group_id = %s AND chain_version = %s
            ORDER BY seq""",
        (group_id, CHAIN_VERSION),
    ).fetchall()

    out = Report(group_id=group_id, n_attested=len(rows))
    if not rows:
        out.keyed = bool(keys())
        return out

    algs = {r[4] for r in rows}
    out.keyed = algs == {"hmac-sha256"}
    out.mixed_keying = len(algs) > 1
    out.key_ids = list(dict.fromkeys(r[5] for r in rows))

    available = keys()
    expected_prev = GENESIS
    for verification_id, seq, prev_digest, stored, _alg, key_id in rows:
        if prev_digest != expected_prev:
            out.broken_links.append({
                "seq": seq, "verification_id": verification_id,
                "expected": expected_prev, "found": prev_digest,
            })
        expected_prev = stored

        try:
            body = canonical(claims_of(conn, group_id, verification_id))
        except VAttestError:
            out.vanished.append(verification_id)
            continue

        # The key named ON this link, not the current one. That is the whole
        # mechanism of rotation: a link signed last quarter is checked with
        # last quarter's key, so a new key does not turn the entire audit
        # trail into forgeries.
        if key_id == UNKEYED:
            material: str | None = None
        elif key_id in available:
            material = available[key_id]
        else:
            out.unverifiable.append({
                "seq": seq, "verification_id": verification_id,
                "key_id": key_id,
            })
            continue

        # Recomputed against the stored prev_digest rather than the expected
        # one, so a break earlier in the chain does not report every link
        # after it as altered. One cause, one finding.
        if digest(prev_digest, body, material) != stored:
            out.altered.append({
                "seq": seq, "verification_id": verification_id,
                "key_id": key_id,
            })

    _logger.info(
        "verification_chain_verified",
        extra={"fields": {
            "group_id": group_id, "n_attested": out.n_attested,
            "altered": len(out.altered),
            "broken_links": len(out.broken_links),
            "vanished": len(out.vanished),
            "unverifiable": len(out.unverifiable),
            "keyed": out.keyed, "key_ids": out.key_ids,
        }},
    )
    return out
