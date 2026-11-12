"""The wire format: a manifest, a facts blob, and a digest that binds them.

A bundle is two byte strings. The facts blob is newline delimited JSON, one
redacted claim per line. The manifest is a single JSON object that says where
the bundle came from, which redaction policy produced it, how many claims it
holds, and the sha256 of the facts blob.

The digest is what makes the pair a unit. `sign` signs the manifest, the
manifest carries `content_digest`, and the digest covers the facts - so one
signature over a small object transitively covers however many facts the
bundle holds. An importer that verifies the signature and then recomputes the
digest has checked the whole file, and it has to do both: a valid signature
over a manifest paired with somebody else's facts blob proves nothing.

## Canonical bytes, and why it is not a detail

`canonical` is deterministic JSON: sorted keys, no whitespace variance, no
ASCII escaping. A digest taken over non-canonical JSON verifies on the machine
that produced it and nowhere else, because two dict orderings of the same
claim hash differently - and the failure arrives as "this bundle is corrupt"
at the recipient, which is indistinguishable from an attack.

`attestation.canonical` made the same decision for the same reason and this
matches it, deliberately rather than by coincidence.

## What never leaves

**`group_id`.** Not a policy choice - a rule of the format. A group id is the
exporter's tenancy boundary, it means nothing to the recipient, and carrying
it would invite an importer to write facts into a scope named by the sender.

**An AGE `graphid` as an id.** A graphid is unique per DATABASE, not globally.
Two deployments will issue the same one for unrelated facts, so the origin id
travels as `origin_fact_id` and is only ever used to build a qualified
provenance string. Nothing joins on it.

**A superseded version of a claim.** Only live facts are exported. The history
of a claim is a changelog of the exporter's own revisions, and a recipient has
no use for a belief that was withdrawn before it was sent.

**The origin's attestation chain.** A chain is per scope, so an imported fact
cannot carry one; the local chain it joins is the importer's. What the manifest
records instead is the exporter's chain head at build time, which an importer
compares against a head the exporter published somewhere this database cannot
reach. That is the external-witness argument migration 0030 makes, applied
across an organisational boundary.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from echo_memory import attestation
from echo_memory.exchange import redact
from echo_memory.exchange.redact import Policy
from echo_memory.infra.db import GRAPH_NAME as GRAPH
from echo_memory.infra.logging import get_logger

_logger = get_logger("exchange.bundle")

#: The format a reader must understand to read a bundle. Bumped when a change
#: would make an older importer misread a newer file, never for an addition an
#: older importer can ignore.
FORMAT_VERSION = 1

#: How many claims one bundle may carry.
#:
#: The bound that matters is not memory, it is the scope's advisory lock.
#: `import_bundle` writes every fact through `write_episode` inside ONE
#: transaction, by necessity - a failure part way has to leave neither a
#: registry row nor a partial scope - and `write_episode` takes that lock as
#: its first statement. So a bundle's fact count is a duration this import
#: holds a scope against every other writer in it.
#:
#: Measured on 2026-11-12 against the test database: 200 imported facts in
#: 3.39s, 16.9 ms each, with an in-process hash embedder on a nearly empty
#: graph. Both of those flatter the number. A network embedder is several
#: times slower per call, and `write_episode`'s entity resolution does a
#: vector search per endpoint, which gets slower as the scope fills. So
#: 10,000 claims is about 2.8 minutes of held lock at best and plausibly three
#: times that in a real deployment, and 10 million - which nothing refused
#: before this - is a lock held for days and a list of dicts nothing bounded.
#:
#: 10,000 is a backstop and not a quota. For scale: this project's own largest
#: live scope holds 1,485 facts, so the ceiling is roughly seven times the
#: biggest thing that exists to export. A partner with more than that to send
#: splits the bundle, which also gives each half its own signature, digest and
#: promotion decision - a better shape than one file nobody can review.
MAX_FACTS = 10_000

#: How many bytes the facts blob may be. 16 MiB.
#:
#: This one IS memory. The caller reads the whole blob before `parse` sees it,
#: `parse` then holds it as a list of dicts, and `import_bundle` holds that
#: list for the length of the transaction - so the resident cost is several
#: times this number, and the blob is the only input an outside organisation
#: chooses the size of.
#:
#: Measured, so the interaction with MAX_FACTS is stated rather than left to be
#: discovered: a realistic exported claim canonicalises to 400 bytes, so 16 MiB
#: is about 42,000 of them and MAX_FACTS always binds first. A claim at
#: `importer.MAX_STRING_LEN` on its fact text AND both endpoint names is
#: 12,299 bytes, and 16 MiB is 1,364 of those - so a bundle whose claims
#: average over about 1.6 KiB is refused below MAX_FACTS. That is the right way
#: round: the pathological bundle is the one that hits the memory bound early.
MAX_BLOB_BYTES = 16 * 1024 * 1024

#: How many bytes the manifest may be. 64 KiB.
#:
#: The manifest is a fixed set of scalar fields plus two small maps, and it is
#: read before anything else, so this is the cheapest refusal in the file.
#: Measured: a fully populated signed manifest - long account and deployment
#: ids, a chain head, an ed25519 signature - is 681 bytes, so 64 KiB is about
#: 96 times anything legitimate. The same number and the same argument as
#: `events.envelope.MAX_PAYLOAD_BYTES`, which bounds the other thing an
#: outsider hands this store.
MAX_MANIFEST_BYTES = 64 * 1024

#: Every field of an exported claim, in the order the format documents them.
#: `agent_id` is absent: it is present only when a policy keeps it, and a
#: format that listed it would imply it is normally there.
FACT_FIELDS = (
    "origin_fact_id",
    "relation_type",
    "fact",
    "confidence",
    "causal_hint",
    "t_valid",
    "source",
    "target",
    "source_type",
    "target_type",
    "provenance",
)


class BundleError(Exception):
    """A bundle cannot be read, and the message says what about it is wrong."""


def canonical(obj) -> bytes:
    """The bytes a digest is taken over.

    JSON with sorted keys and no insignificant whitespace, because the digest
    has to be reproducible by a different version of this code on a different
    machine in a different organisation. Anything that lets two serialisations
    of the same object differ turns a verification failure into a coin flip,
    and a recipient cannot tell a coin flip from tampering.

    `ensure_ascii=False` so a non-ASCII claim is one UTF-8 string rather than
    a pile of escapes whose exact form depends on the encoder. The bytes are
    UTF-8 either way; this keeps them the shorter and more obviously stable of
    the two.
    """
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def digest_of(facts_bytes: bytes) -> str:
    """sha256 over the facts blob, prefixed with the algorithm.

    Prefixed for the reason `attestation.digest` prefixes its own: a bare hex
    string cannot say what produced it, and a format that has to change hash
    one day should not have to guess what the old values were.
    """
    return "sha256:" + hashlib.sha256(facts_bytes).hexdigest()


@dataclass(frozen=True)
class Manifest:
    """What a bundle says about itself.

    Frozen. The manifest is the signed object, so a manifest that could be
    mutated after signing would let a caller change what a signature covers
    without the signature noticing - which is the whole thing a signature is
    for. Attaching a signature produces a new manifest; see `signed`.
    """

    format_version: int
    bundle_id: str
    #: Who built this. One half of the qualifier that keeps colliding origin
    #: ids apart on the importer's side, so it has to be stable per deployment
    #: and distinct between deployments. Not verified by anything here: the
    #: signature is what identifies a sender, and this is a label.
    deployment_id: str
    account: str
    memory: str
    created_at: str
    redaction_policy_id: str
    fact_count: int
    content_digest: str
    #: The exporter's attestation head for the source scope, as
    #: {"seq", "digest", "keyed"}, or None when the scope has no chain.
    source_chain_head: dict | None = None
    #: {"alg", "key_id", "value"}, or None for an unsigned bundle. `build`
    #: always returns None here: signing is a separate step with a separate
    #: key, and a builder that signed would need a key to produce a draft.
    signature: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    def unsigned(self) -> dict:
        """The manifest as the bytes a signature is taken over.

        Without `signature`, because a signature cannot cover itself. Every
        other field is included, so a bundle cannot be re-labelled with a
        different account, policy or digest and keep its signature.
        """
        body = self.to_dict()
        body.pop("signature", None)
        return body

    def signed(self, signature: dict) -> Manifest:
        """This manifest with a signature attached."""
        return Manifest(**{**self.to_dict(), "signature": signature})

    @classmethod
    def from_dict(cls, body: dict) -> Manifest:
        missing = [f for f in cls.__dataclass_fields__ if f not in body]
        # format_version first, so a file from a future version is refused as
        # a version problem rather than as a pile of missing fields. A reader
        # that reported the second would send somebody looking for corruption.
        if "format_version" in missing:
            raise BundleError("manifest has no format_version")
        _check_version(body["format_version"])
        required = [f for f in missing if f not in ("source_chain_head", "signature")]
        if required:
            raise BundleError(
                f"manifest is missing {', '.join(required)}"
            )
        known = {k: v for k, v in body.items() if k in cls.__dataclass_fields__}
        manifest = cls(**known)
        _check_fact_count(manifest.fact_count)
        return manifest


def _check_fact_count(count) -> None:
    """Refuse a declared count before the blob behind it is looked at.

    On the MANIFEST, which is the first thing read and the smallest. A sender
    claiming 10 million claims is refused in the time it takes to parse 681
    bytes, without the blob being walked, hashed or held - which is the whole
    point of putting the ceiling on the declaration as well as on the facts.

    A count that is not an integer is a malformed manifest rather than an
    oversized one, and is refused as one: `fact_count` is compared against a
    parsed length and against `len(facts)` in the importer, and a string that
    happened to compare would be a worse failure than this.
    """
    if isinstance(count, bool) or not isinstance(count, int):
        raise BundleError(
            f"manifest fact_count is a {type(count).__name__}, expected an "
            f"integer"
        )
    if count < 0:
        raise BundleError(f"manifest fact_count is {count}, which is not a count")
    if count > MAX_FACTS:
        raise BundleError(
            f"manifest declares {count} facts, over the {MAX_FACTS} limit. "
            f"This store refuses the bundle rather than importing a prefix of "
            f"it: a partner with more to send splits it, which also gives each "
            f"half its own signature and its own promotion decision."
        )


def _check_version(version) -> None:
    if version != FORMAT_VERSION:
        raise BundleError(
            f"bundle format_version {version!r}, this build reads "
            f"{FORMAT_VERSION}. A newer bundle is not read on a best-effort "
            f"basis: a field this version does not understand may be the one "
            f"that says what may not leave."
        )


def _value(raw):
    """One agtype property as a plain Python value.

    An absent AGE property comes back as the agtype `null` rather than SQL
    NULL, and the two have to read the same or the exported bytes would depend
    on which code path produced them. `attestation._value` makes the same
    conversion for the same reason; see the note there.
    """
    if raw is None or str(raw) == "null":
        return None
    return json.loads(str(raw))


def live_facts(conn, group_id: str) -> list[dict]:
    """Every active fact in this scope, in the export row shape.

    Read through Cypher rather than off the edge table, because `provenance`
    is an agtype map and an agtype map cannot be cast to text - the
    per-property projection is the only form that works. `attestation.claims_of`
    reads facts the same way and for the same reason.

    The cost that comes with that: AGE expands the MATCH before filtering, so
    this walks every FACT edge in the database rather than using the
    (group_id, ...) indexes migration 0013 created. Acceptable here and
    nowhere on a read path - an export is one operator-invoked pass over one
    scope, not something a query does.
    """
    rows = conn.execute(
        f"""SELECT * FROM cypher('{GRAPH}', $$
            MATCH (a)-[e:FACT]->(b)
            WHERE e.group_id = $gid AND e.t_invalid IS NULL
            RETURN id(e), e.relation_type, e.fact, e.confidence, e.causal_hint,
                   e.t_valid, a.name, b.name, a.type, b.type, e.provenance,
                   e.agent_id
        $$, %s) AS (origin_fact_id agtype, relation_type agtype, fact agtype,
                     confidence agtype, causal_hint agtype, t_valid agtype,
                     source agtype, target agtype, source_type agtype,
                     target_type agtype, provenance agtype, agent_id agtype)""",
        (json.dumps({"gid": group_id}),),
    ).fetchall()

    out = []
    for row in rows:
        values = [_value(x) for x in row]
        fact = dict(zip((*FACT_FIELDS, "agent_id"), values, strict=True))
        # A graphid arrives as a number and leaves as a string. It is an
        # opaque label on the other side of the boundary, not an integer the
        # recipient does arithmetic on, and a JSON number large enough to lose
        # precision in a 53 bit float is a silent corruption of an id.
        fact["origin_fact_id"] = str(fact["origin_fact_id"])
        out.append(fact)
    return sorted(out, key=lambda f: int(f["origin_fact_id"]))


def build(
    conn,
    group_id: str,
    *,
    deployment_id: str,
    account: str,
    memory: str,
    policy: Policy | str | None = None,
) -> tuple[Manifest, bytes]:
    """A bundle from one scope: the manifest and the facts blob.

    Redaction happens here, before the digest is computed, so the digest - and
    therefore the signature over the manifest that carries it - covers exactly
    what will be sent. See the order argument in `redact`.

    The manifest comes back unsigned. `sign.sign` attaches a signature to the
    returned manifest, which is a separate step because it needs a key and a
    caller producing a draft to look at should not.
    """
    chosen = policy if isinstance(policy, Policy) else redact.get(policy)

    rows = live_facts(conn, group_id)
    kept = [f for f in (redact.apply(chosen, row) for row in rows) if f is not None]

    # One canonical line per claim, and the digest over the whole blob. Per
    # line rather than over one big array so that a reader can stream the file
    # and so that a diff between two bundles is per fact.
    facts_bytes = b"".join(canonical(fact) + b"\n" for fact in kept)

    seq, head_digest = attestation.head(conn, group_id)
    chain_head = (
        {"seq": seq, "digest": head_digest, "keyed": attestation.keyed()}
        if seq
        else None
    )

    manifest = Manifest(
        format_version=FORMAT_VERSION,
        bundle_id=str(uuid.uuid4()),
        deployment_id=deployment_id,
        account=account,
        memory=memory,
        created_at=datetime.now(UTC).isoformat(),
        redaction_policy_id=chosen.policy_id,
        fact_count=len(kept),
        content_digest=digest_of(facts_bytes),
        source_chain_head=chain_head,
    )
    _logger.info(
        "bundle_built",
        extra={"fields": {
            "bundle_id": manifest.bundle_id, "group_id": group_id,
            "policy": chosen.policy_id, "n_read": len(rows),
            "n_exported": len(kept),
        }},
    )
    return manifest, facts_bytes


def parse(manifest_bytes: bytes, facts_bytes: bytes) -> tuple[Manifest, list[dict]]:
    """Read a bundle, refusing anything that does not hang together.

    Three checks, each of which has to name what differed rather than say the
    bundle is bad. An operator holding a file that will not import needs to
    know whether they have a version problem, a truncated download or a
    tampered blob, and those have three different answers.

    This does NOT verify a signature. A caller that parses and does not also
    call `sign.verify` has checked that the file is internally consistent,
    which an attacker who rewrote both halves can also arrange. `importer`
    verifies first and parses second, deliberately in that order.

    ## The size refusals come first, and that order is the point

    Nothing bounded a bundle before this: a 10 million claim file was read
    whole into memory, hashed, held as a list of dicts and written inside one
    transaction holding the scope's advisory lock. So the ceilings are checked
    before the work they would bound - the manifest's length before it is
    parsed, its declared `fact_count` before the blob is touched, the blob's
    length before it is split, and the parsed count before anything is
    returned. A bundle over the limit costs this function two `len()` calls.

    The declared count and the real one are BOTH checked against the ceiling,
    and then against each other by the caller. A manifest claiming 10 facts in
    a blob of 10,000 is an attack on exactly the reader that trusts the cheap
    number, which is why the cheap number is not the only one checked.
    """
    if len(manifest_bytes) > MAX_MANIFEST_BYTES:
        raise BundleError(
            f"manifest is {len(manifest_bytes)} bytes, over the "
            f"{MAX_MANIFEST_BYTES} limit"
        )
    if len(facts_bytes) > MAX_BLOB_BYTES:
        raise BundleError(
            f"facts blob is {len(facts_bytes)} bytes, over the "
            f"{MAX_BLOB_BYTES} limit. Nothing has been parsed: the length is "
            f"checked before the blob is split, because reading it is the cost "
            f"this refuses."
        )
    try:
        body = json.loads(manifest_bytes)
    except json.JSONDecodeError as e:
        raise BundleError(f"manifest is not JSON: {e}") from None
    if not isinstance(body, dict):
        raise BundleError(f"manifest is a {type(body).__name__}, expected an object")

    manifest = Manifest.from_dict(body)

    computed = digest_of(facts_bytes)
    if computed != manifest.content_digest:
        raise BundleError(
            f"content digest mismatch: the manifest says "
            f"{manifest.content_digest}, the facts blob hashes to {computed}. "
            f"The manifest and the facts do not belong together - either the "
            f"blob was altered after the manifest was written, or two bundles "
            f"have been mixed up."
        )

    facts: list[dict] = []
    for n, line in enumerate(facts_bytes.splitlines(), start=1):
        if not line.strip():
            continue
        # Counted as they are built rather than after. `from_dict` already
        # refused an oversized DECLARED count, so reaching this means the
        # manifest lied about how many claims the blob holds - and the whole
        # reason to check the real count too is that the cheap one is the
        # sender's word. Refusing mid-loop is what keeps the list bounded.
        if len(facts) >= MAX_FACTS:
            raise BundleError(
                f"facts blob holds more than the {MAX_FACTS} limit, while the "
                f"manifest declares {manifest.fact_count}. Stopped at line "
                f"{n} rather than reading the rest."
            )
        try:
            fact = json.loads(line)
        except json.JSONDecodeError as e:
            raise BundleError(f"facts line {n} is not JSON: {e}") from None
        if not isinstance(fact, dict):
            raise BundleError(
                f"facts line {n} is a {type(fact).__name__}, expected an object"
            )
        facts.append(fact)

    # The digest already proves the blob is the one the manifest was written
    # for, so a count mismatch here means the exporter itself disagreed about
    # how many facts it wrote. That is a bug at the sender, not tampering, and
    # importing under it would mean trusting a count nothing else checks.
    if len(facts) != manifest.fact_count:
        raise BundleError(
            f"fact count mismatch: the manifest says {manifest.fact_count}, "
            f"the facts blob holds {len(facts)}"
        )

    return manifest, facts
