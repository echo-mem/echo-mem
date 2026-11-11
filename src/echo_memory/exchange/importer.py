"""Accepting a bundle: verify, refuse a replay, quarantine, promote, revoke.

An imported fact is somebody else's claim. It arrives in a scope ordinary
retrieval is never given, it is written at `confidence="inferred"` whatever the
exporter called it, and it becomes readable only when a named verifier says so.

## Why a quarantine scope rather than a flag on each fact

Scope is the boundary this store already enforces. `query_memory` takes one
group_id, `query_across` takes a list the caller was authorised for, and every
index is keyed on it. A quarantine scope therefore inherits an exclusion that
is already load-bearing rather than adding a new condition every read path has
to remember - and `retrieval/envelope.py` makes the same point from the other
direction: you cannot be injected by a fact you cannot retrieve.

A flag on each fact would need every reader to filter on it. There are several
readers, one of them in another repository, and the failure mode of a forgotten
filter is imported text reaching a model's context with nothing to stop it.

What makes this work is that `readable_scopes` is the only function that hands
out an import scope, and it hands out promoted ones. A caller that builds the
list for `query_across` from this function cannot include a quarantined bundle
by accident, because the string it would need is not in the list.

## Two facts about the domain that shape everything here

**An AGE `graphid` is unique per DATABASE, not globally.** Two deployments will
issue the same id for unrelated facts, so an imported fact gets a NEW local
graphid and keeps its origin only as `provenance.origin`, the string
`"<deployment_id>:<original_fact_id>"`. Nothing joins on an origin id.

**An attestation chain is per scope.** An imported fact cannot carry the
origin's chain; the chain it joins is this deployment's, built as `write_episode`
attests each write. The manifest records the exporter's chain head instead, so
an importer verifies the BUNDLE against a head the exporter published somewhere
this database cannot reach rather than verifying each fact.

## Where the capability check is not

`promote` takes a `verifier_id` and records it. It does not check whether that
verifier is allowed to promote anything, because the engine has no concept of a
principal: every function here takes a connection and strings, and the thing
that knows who is asking is cloud. The capability is `roles.trust.verify` there.

Requiring the argument anyway is the point. A promotion with no verifier
recorded is a trust decision nobody made, and six months later the only
question anybody asks about an imported scope is who accepted it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from echo_memory.exchange import bundle as bundle_format
from echo_memory.exchange import sign as signing
from echo_memory.infra.db import GRAPH_NAME as GRAPH
from echo_memory.infra.logging import get_logger
from echo_memory.ingestion.write_episode import (
    MAX_STRING_LEN,
    VALID_CAUSAL_HINTS,
    write_episode,
)
from echo_memory.trust import states

_logger = get_logger("exchange.importer")

#: What an imported claim's `confidence` is set to, whatever the bundle says.
#:
#: `confidence` records how the writer came by a claim, and this store did not
#: extract an imported one from anything it read - it accepted somebody else's
#: assertion. 'inferred' is the closest of the three values migration 0001
#: permits. The real signal is elsewhere and is stronger: the scope is not
#: readable, the content class is IMPORTED_UNTRUSTED, and the trust state is
#: UNVERIFIED until a verifier changes it.
IMPORTED_CONFIDENCE = "inferred"

#: The entity type an imported endpoint gets when the bundle names none.
#: `unknown` rather than an empty string, which AGE would drop entirely, and
#: which is the same representation of "nobody said" that migration 0003
#: backfilled for `project`.
UNKNOWN_TYPE = "unknown"

STATE_CREATED = "created"
STATE_QUARANTINED = "quarantined"
STATE_PROMOTED = "promoted"
STATE_REVOKED = "revoked"
STATE_REJECTED = "rejected"


class ImportRefused(Exception):
    """A bundle was not accepted, and the message says why.

    One exception type for every refusal, because the caller's response to all
    of them is the same: nothing was written, show the operator the reason.
    """


def _segment(name: str, what: str) -> str:
    """One piece of an import scope, checked for the delimiter it sits between.

    A colon is this format's separator AND a legal character in an account id,
    a memory name and a bundle id, so a name carrying one supplies its own
    separators. That is not a cosmetic problem: `readable_scopes` selects on
    `starts_with(group_id, quarantine_prefix(account, memory))`, so a memory
    called `main:import:x:memory:other` produces

        acct:A:memory:main:import:x:memory:other:import:b

    which starts with the prefix for memory `main` and is therefore returned
    to a reader of `main` as one of ITS promoted imports. One segment earlier
    the same trick crosses accounts: an account id of
    `victim:memory:main:import:pad` puts its own promoted scope in
    `readable_scopes(conn, "victim", "main")`. Measured both, against a real
    database, before this check existed.

    Refused rather than escaped or replaced. An escape would make two
    different names produce the same scope on the way in and different ones on
    the way out, and silently repairing a name is how a function stops being
    able to tell a typo from a value it minted itself - the same call
    `echo_cloud.tenancy._segment` makes, and this is deliberately the same
    rule as the `_KEY` and `_BUNDLE` checks there rather than a second
    opinion about what a safe name is.
    """
    text = str(name)
    if ":" in text:
        raise ImportRefused(
            f"an import scope's {what} must not contain ':', which is this "
            f"format's separator: {text!r} would supply its own segments and "
            f"land the bundle in a scope prefix-matching another "
            f"{'account' if what == 'account' else 'memory'}'s imports."
        )
    if not text.strip():
        raise ImportRefused(f"an import scope needs a {what}")
    return text


def quarantine_prefix(account: str, memory: str) -> str:
    """The prefix every import scope for this memory shares.

    Exists so that the shape of an import scope is derived in exactly one
    place. A second copy of this string somewhere that reads them would go
    blind the moment either copy changed, and it would go blind silently -
    `readable_scopes` would return an empty list and look like a memory with
    no imports rather than a reader that cannot see them.

    Both halves are checked here rather than at the call sites, because this
    is the one place the shape is derived and therefore the one place that
    knows what a segment is. See `_segment`.
    """
    return (
        f"acct:{_segment(account, 'account')}"
        f":memory:{_segment(memory, 'memory')}:import:"
    )


def quarantine_group_id(account: str, memory: str, bundle_id: str) -> str:
    """The scope one imported bundle's facts are written to.

    This string is deliberately NOT in the list an ordinary read is given.
    `query_memory` is called with one scope and `query_across` with the scopes
    a principal was authorised for; neither is ever handed an import scope
    until `readable_scopes` includes it, which happens only after a verifier
    promotes the bundle.

    Per bundle rather than per peer, so that revoking one bundle cannot take
    another bundle's facts with it and promoting one cannot carry another in.

    The bundle id is the one segment an outside organisation chooses, since it
    arrives in a signed manifest, so it is checked for the separator with the
    other two. A bundle id may hold anything else a text column can: it is a
    key and a suffix, never a prefix, so it cannot reach past itself.
    """
    return quarantine_prefix(account, memory) + _segment(bundle_id, "bundle_id")


# --- the registry -------------------------------------------------------


_ROW_KEYS = (
    "bundle_id", "direction", "group_id", "state", "fact_count",
    "peer_deployment", "peer_account", "format_version", "content_digest",
    "signature_alg", "signature_key_id", "redaction_policy_id",
    "created_at", "promoted_at", "revoked_at", "source_chain_head", "detail",
)

_ROW_COLUMNS = """SELECT bundle_id, direction, group_id, state, fact_count,
                         peer_deployment, peer_account, format_version,
                         content_digest, signature_alg, signature_key_id,
                         redaction_policy_id, created_at, promoted_at,
                         revoked_at, source_chain_head, detail
                    FROM public.bundle_registry"""


def _row(conn, group_id: str, bundle_id: str) -> dict | None:
    """One registry row by its full key, or None.

    Both halves, always. Since migration 0038 a `bundle_id` is unique only
    within a scope, and this is the lookup the replay check uses - so a query
    on the id alone would read another account's row and refuse an import on
    the strength of it, which is the disclosure 0038 exists to close.
    """
    row = conn.execute(
        _ROW_COLUMNS + " WHERE group_id = %s AND bundle_id = %s",
        (group_id, bundle_id),
    ).fetchone()
    if row is None:
        return None
    return dict(zip(_ROW_KEYS, row, strict=True))


def _rows_for_id(conn, bundle_id: str) -> list[dict]:
    """Every scope's row for one bundle id, oldest first.

    This is the deployment-wide read, and it is only for an OPERATOR surface:
    `listing` is already unscoped and the CLI runs as the deployment rather
    than as a tenant. Nothing on a tenant path calls it.
    """
    rows = conn.execute(
        _ROW_COLUMNS + " WHERE bundle_id = %s ORDER BY created_at, group_id",
        (bundle_id,),
    ).fetchall()
    return [dict(zip(_ROW_KEYS, r, strict=True)) for r in rows]


def get(conn, bundle_id: str, *, group_id: str | None = None) -> dict:
    """One registry row, by bundle id and optionally by scope.

    `group_id` is optional because the operator surfaces have only ever had
    the id: `echo-memory exchange show <bundle_id>` is what an operator holds
    after a partner names a file. With 0038's composite key that is no longer
    guaranteed unique, so a bare id resolves to the single row when there is
    one and is REFUSED when there are several, naming the scopes so the
    operator can repeat the call with one.

    Refused rather than resolved by any rule - newest, or first - because
    `promote` and `revoke` are trust decisions. Guessing which account's
    import a verifier meant to accept is the one error here that cannot be
    noticed afterwards: the scope either became readable or did not, and
    nothing in the row says it was the wrong one.
    """
    if group_id is not None:
        found = _row(conn, group_id, bundle_id)
        if found is None:
            raise ImportRefused(
                f"no bundle {bundle_id!r} is recorded in {group_id!r}"
            )
        return found

    candidates = _rows_for_id(conn, bundle_id)
    if not candidates:
        raise ImportRefused(f"no bundle {bundle_id!r} is recorded here")
    if len(candidates) > 1:
        scopes = ", ".join(repr(c["group_id"]) for c in candidates)
        raise ImportRefused(
            f"bundle {bundle_id!r} is recorded in {len(candidates)} scopes "
            f"({scopes}). Name one with --scope: which import a trust "
            f"decision applies to is not something this can guess, because a "
            f"promotion of the wrong one is not visible afterwards."
        )
    return candidates[0]


def listing(conn, *, state: str | None = None, limit: int = 100) -> list[dict]:
    """Every bundle this deployment has sent or accepted, newest first."""
    rows = conn.execute(
        """SELECT bundle_id, direction, group_id, state, fact_count,
                  peer_deployment, peer_account, redaction_policy_id,
                  created_at, promoted_at, revoked_at, detail
             FROM public.bundle_registry
            WHERE (%s::text IS NULL OR state = %s)
            ORDER BY created_at DESC LIMIT %s""",
        (state, state, limit),
    ).fetchall()
    keys = (
        "bundle_id", "direction", "group_id", "state", "fact_count",
        "peer_deployment", "peer_account", "redaction_policy_id",
        "created_at", "promoted_at", "revoked_at", "detail",
    )
    return [dict(zip(keys, r, strict=True)) for r in rows]


def register_export(
    conn,
    manifest: bundle_format.Manifest,
    *,
    group_id: str,
    peer_deployment: str | None = None,
    peer_account: str | None = None,
    detail: dict | None = None,
) -> dict:
    """Record that a bundle was built and left.

    State 'created' and never anything else, because this deployment cannot
    know what happened to a file after it was written. An export row that
    claimed 'delivered' would be asserting something nothing here observed.

    This is the exporter's only durable record of what it handed over, and it
    is the thing that answers "what did we send them" once the file is gone.
    """
    signature = manifest.signature or {}
    conn.execute(
        """INSERT INTO public.bundle_registry
               (bundle_id, direction, group_id, peer_deployment, peer_account,
                format_version, content_digest, signature_alg,
                signature_key_id, fact_count, redaction_policy_id, state,
                source_chain_head, detail)
           VALUES (%s, 'export', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                   %s::jsonb, %s::jsonb)""",
        (
            manifest.bundle_id, group_id, peer_deployment, peer_account,
            manifest.format_version, manifest.content_digest,
            signature.get("alg"), signature.get("key_id"),
            manifest.fact_count, manifest.redaction_policy_id, STATE_CREATED,
            json.dumps(manifest.source_chain_head)
            if manifest.source_chain_head else None,
            json.dumps(detail) if detail else None,
        ),
    )
    _logger.info(
        "bundle_exported",
        extra={"fields": {
            "bundle_id": manifest.bundle_id, "group_id": group_id,
            "fact_count": manifest.fact_count,
            "policy": manifest.redaction_policy_id,
            "peer": peer_account or peer_deployment,
        }},
    )
    return get(conn, manifest.bundle_id, group_id=group_id)


# --- accepting a bundle -------------------------------------------------


def _check_importable(facts: list[dict]) -> None:
    """Refuse the whole bundle rather than skip a fact this store cannot hold.

    Up front, before anything is written, and all-or-nothing. A bundle is
    untrusted input, and silently dropping the three claims that were too long
    would make the imported scope a subset nobody can identify - the operator
    sees 197 of 200 facts and no record of which three are missing or why.
    """
    for n, fact in enumerate(facts, start=1):
        where = f"fact {n} (origin {fact.get('origin_fact_id')!r})"
        text = fact.get("fact")
        if not isinstance(text, str) or not text.strip():
            raise ImportRefused(f"{where} has no fact text")
        if len(text) > MAX_STRING_LEN:
            raise ImportRefused(
                f"{where} is {len(text)} characters; this store refuses a fact "
                f"over {MAX_STRING_LEN}"
            )
        for end in ("source", "target"):
            name = fact.get(end)
            if not isinstance(name, str) or not name.strip():
                raise ImportRefused(f"{where} has no {end}")
            if len(name) > MAX_STRING_LEN:
                raise ImportRefused(f"{where} has a {end} name that is too long")
        if not fact.get("relation_type"):
            raise ImportRefused(f"{where} has no relation_type")
        hint = fact.get("causal_hint")
        if hint is not None and hint not in VALID_CAUSAL_HINTS:
            raise ImportRefused(
                f"{where} carries causal_hint {hint!r}, which this store does "
                f"not traverse. Expected one of {sorted(VALID_CAUSAL_HINTS)} "
                f"or none."
            )


def _set_origin(conn, edge_id: str, origin: str, origin_t_valid) -> None:
    """Record where an imported fact came from, on the fact itself.

    `write_episode` writes `provenance` as {session_id, source_episode_id} and
    takes no argument for anything else, so the origin is merged in afterwards.
    The whole map is rewritten rather than one key, because AGE has no nested
    property assignment, and the existing keys are read back first so that
    neither is lost.

    This is safe against the attestation and that is not an assumption. The
    digest covers `provenance.session_id` and nothing else of this map (see
    `attestation.claims_of`), so adding keys beside a preserved session_id
    leaves every digest unchanged. Verified against a real database: the chain
    reports clean after this runs, and there is a test that asserts it.

    `origin_t_valid` is here because it cannot be anywhere else. The local
    `t_valid` is when the fact arrived in this deployment, which is the honest
    local answer and is also attested, so it must not be rewritten to the
    origin's time. Keeping the origin's value in provenance means a reader can
    still ask when the exporter believed it, and nothing is lost.
    """
    row = conn.execute(
        f"""SELECT * FROM cypher('{GRAPH}', $$
            MATCH ()-[e:FACT]->() WHERE id(e) = $eid RETURN e.provenance
        $$, %s) AS (provenance agtype)""",
        (json.dumps({"eid": int(edge_id)}),),
    ).fetchone()
    existing = json.loads(str(row[0])) if row and str(row[0]) != "null" else {}
    if not isinstance(existing, dict):
        existing = {}
    existing["origin"] = origin
    if origin_t_valid is not None:
        existing["origin_t_valid"] = origin_t_valid
    conn.execute(
        f"""SELECT * FROM cypher('{GRAPH}', $$
            MATCH ()-[e:FACT]->() WHERE id(e) = $eid
            SET e.provenance = $provenance
            RETURN id(e)
        $$, %s) AS (edge_id agtype)""",
        (json.dumps({"eid": int(edge_id), "provenance": existing}),),
    ).fetchone()


def import_bundle(
    conn,
    *,
    manifest: bundle_format.Manifest,
    facts: list[dict],
    account: str,
    memory: str,
    embedder,
    trusted_keys,
    deployment_id: str,
) -> dict:
    """Verify a bundle and write its facts into a quarantine scope.

    The order of the first three steps is the whole safety argument, and it is
    load-bearing rather than tidy: a bad signature, a digest mismatch or a
    replayed bundle_id each has to be found BEFORE the first row is written, or
    the refusal leaves facts behind that nothing will clean up.

      1. the signature, against `trusted_keys` the caller supplied
      2. the content digest, recomputed from the claims themselves
      3. the bundle_id, against what has already been imported

    Then, inside one transaction so a failure part way leaves neither a
    registry row nor a partial scope:

      4. register the bundle as quarantined
      5. write each fact through `write_episode`, at IMPORTED_CONFIDENCE, with
         provenance.origin qualified by the exporter's deployment id

    Facts go through `write_episode` and never through raw Cypher, which is
    what gets them an embedding, an audit entry and a link in this scope's
    attestation chain. An import written with Cypher would be the one kind of
    fact in the store that nothing can verify.

    One episode per fact, deliberately. Batched, a fact the writer treats as a
    restatement of an earlier one in the same bundle shortens `edges_created`,
    and the origin ids would then be matched to the wrong edges by position -
    which is a mislabelled provenance, the one field an imported fact exists to
    carry correctly.
    """
    ok, reason = signing.check(manifest, trusted_keys=trusted_keys)
    if not ok:
        _logger.warning(
            "bundle_rejected",
            extra={"fields": {"bundle_id": manifest.bundle_id, "reason": reason}},
        )
        raise ImportRefused(f"signature rejected: {reason}")

    recomputed = bundle_format.digest_of(
        b"".join(bundle_format.canonical(f) + b"\n" for f in facts)
    )
    if recomputed != manifest.content_digest:
        raise ImportRefused(
            f"content digest mismatch: the signed manifest says "
            f"{manifest.content_digest}, these claims hash to {recomputed}. "
            f"The signature covers the manifest, so a manifest that does not "
            f"match its facts means the two halves were paired by somebody "
            f"other than the signer."
        )
    if len(facts) != manifest.fact_count:
        raise ImportRefused(
            f"fact count mismatch: the signed manifest says "
            f"{manifest.fact_count}, {len(facts)} claims were supplied"
        )

    # Derived before the replay check, because the scope is now half the key
    # that check looks up. It depends on nothing the bundle says: `account`
    # and `memory` are this importer's own arguments, and the manifest's
    # copies of those fields reach only the registry row and the provenance
    # string. A hostile exporter has no influence on the scope name.
    group_id = quarantine_group_id(account, memory, manifest.bundle_id)

    seen = _row(conn, group_id, manifest.bundle_id)
    if seen is not None:
        raise ImportRefused(
            f"bundle {manifest.bundle_id!r} was already {seen['direction']}ed "
            f"into {group_id} on {seen['created_at']} and is {seen['state']!r}. "
            f"Refusing to import it again: a second import would write every "
            f"fact a second time, as new local nodes that nothing afterwards "
            f"can tell apart from the first copy."
        )

    _check_importable(facts)

    session_id = f"import:{manifest.bundle_id}"
    signature = manifest.signature or {}

    written: list[str] = []
    restated = 0

    with conn.transaction():
        conn.execute(
            """INSERT INTO public.bundle_registry
                   (bundle_id, direction, group_id, peer_deployment,
                    peer_account, format_version, content_digest,
                    signature_alg, signature_key_id, fact_count,
                    redaction_policy_id, state, source_chain_head, detail)
               VALUES (%s, 'import', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       %s::jsonb, %s::jsonb)""",
            (
                manifest.bundle_id, group_id, manifest.deployment_id,
                manifest.account, manifest.format_version,
                manifest.content_digest, signature.get("alg"),
                signature.get("key_id"), manifest.fact_count,
                manifest.redaction_policy_id, STATE_QUARANTINED,
                json.dumps(manifest.source_chain_head)
                if manifest.source_chain_head else None,
                json.dumps({
                    "imported_into": deployment_id,
                    "source_memory": manifest.memory,
                    "signature_verified": reason,
                }),
            ),
        )

        for fact in facts:
            source, target = str(fact["source"]), str(fact["target"])
            entities = [
                {"name": source, "type": str(fact.get("source_type") or UNKNOWN_TYPE)},
                {"name": target, "type": str(fact.get("target_type") or UNKNOWN_TYPE)},
            ]
            # One entity, not two, when a claim's endpoints are the same name.
            # write_episode validates fact endpoints against the entity names
            # rather than against positions, so a duplicate is harmless, but a
            # repeated name in `entities` makes the episode describe two things
            # where the bundle described one.
            if source == target:
                entities = entities[:1]

            result = write_episode(
                conn,
                group_id,
                session_id,
                entities,
                [{
                    "source": source,
                    "target": target,
                    "relation_type": str(fact["relation_type"]),
                    "fact": str(fact["fact"]),
                    "confidence": IMPORTED_CONFIDENCE,
                    "causal_hint": fact.get("causal_hint"),
                }],
                None,
                embedder,
                # The exporter is the author of record. agent_id must not be an
                # agent in THIS deployment: nothing here asserted the claim,
                # and attributing it to a local agent is how an imported
                # sentence later reads as something this store observed.
                agent_id=f"import:{manifest.deployment_id}",
                project=f"import:{manifest.account}",
                assume_new=True,
            )

            if result.get("error"):
                # Inside the transaction, so this rolls back the registry row
                # and every fact written so far. A half-imported bundle whose
                # registry row says 'quarantined' is worse than no import: the
                # replay refusal would then block the retry that would fix it.
                raise ImportRefused(
                    f"refusing the bundle: origin fact "
                    f"{fact.get('origin_fact_id')!r} could not be written "
                    f"({result['error']}). Nothing has been imported."
                )

            edges = result.get("edges_created") or []
            if not edges:
                # A claim this scope already holds word for word. Counted
                # rather than passed over silently, for the reason
                # write_episode reports `restated` at all: "stored nothing
                # because you already told me" and "stored nothing" are
                # different answers and only one lets a caller stop looking.
                restated += 1
                continue

            edge_id = edges[0]
            _set_origin(
                conn,
                edge_id,
                f"{manifest.deployment_id}:{fact.get('origin_fact_id')}",
                fact.get("t_valid"),
            )
            written.append(edge_id)

    summary = {
        "bundle_id": manifest.bundle_id,
        "group_id": group_id,
        "state": STATE_QUARANTINED,
        "n_claims": len(facts),
        "n_written": len(written),
        "n_restated": restated,
        "peer_deployment": manifest.deployment_id,
        "peer_account": manifest.account,
        "redaction_policy_id": manifest.redaction_policy_id,
        "source_chain_head": manifest.source_chain_head,
        "signature": reason,
        "content_class": "imported-untrusted",
        "trust_state": states.UNVERIFIED,
        "note": (
            f"{len(written)} fact(s) are in {group_id}, which ordinary "
            f"retrieval is not given. They become readable when a verifier "
            f"promotes bundle {manifest.bundle_id}."
        ),
    }
    _logger.info("bundle_imported", extra={"fields": {
        k: summary[k] for k in
        ("bundle_id", "group_id", "n_claims", "n_written", "n_restated")
    }})
    return summary


# --- the trust decisions ------------------------------------------------


def _transition(
    conn,
    bundle_id: str,
    *,
    group_id: str | None = None,
    to: str,
    from_states: tuple[str, ...],
    stamp: str | None,
    detail: dict,
) -> dict:
    """Move one bundle's state, and read it back.

    Read back rather than trusting the UPDATE's rowcount, because this schema
    has taught that lesson more than once: a statement that affects no rows is
    not an error, and a trust decision that quietly did nothing is the kind of
    thing nobody notices until the scope turns out to have been readable, or
    not, for a month.

    The scope the UPDATE names comes from the row `get` resolved, not from the
    argument. That is the point of resolving first: with 0038's composite key
    an UPDATE on `bundle_id` alone would promote or revoke every account's
    copy of the bundle at once, and the read-back would not notice because the
    row it reads would be one of the ones that moved correctly.
    """
    current = get(conn, bundle_id, group_id=group_id)
    if current["direction"] != "import":
        raise ImportRefused(
            f"bundle {bundle_id!r} is an export. There is nothing here to "
            f"promote or revoke: what left this deployment is in somebody "
            f"else's store now."
        )
    if current["state"] not in from_states:
        raise ImportRefused(
            f"bundle {bundle_id!r} is {current['state']!r}; this needs it to "
            f"be one of {', '.join(from_states)}"
        )

    stamped = f", {stamp} = now()" if stamp else ""
    conn.execute(
        f"""UPDATE public.bundle_registry
               SET state = %s{stamped},
                   detail = COALESCE(detail, '{{}}'::jsonb) || %s::jsonb
             WHERE group_id = %s AND bundle_id = %s AND state = %s""",
        (to, json.dumps(detail), current["group_id"], bundle_id, current["state"]),
    )

    after = get(conn, bundle_id, group_id=current["group_id"])
    if after["state"] != to:
        raise ImportRefused(
            f"bundle {bundle_id!r} is still {after['state']!r} after asking "
            f"for {to!r}; nothing changed"
        )
    return after


def promote(conn, bundle_id: str, *, verifier_id: str, group_id: str | None = None) -> dict:
    """Make an imported bundle's scope readable, on a named verifier's say-so.

    `verifier_id` is required and recorded. The engine cannot check whether
    that verifier is entitled to promote anything - it has no concept of a
    principal, only a connection and strings - so the capability check lives in
    cloud, as `roles.trust.verify`.

    Requiring the argument regardless is not theatre. A promotion is the moment
    another organisation's claims become answers this store will give, and the
    only question anybody asks about an imported scope afterwards is who
    accepted it. An unattributed promotion cannot answer that, and there is a
    test that this refuses one.

    What promotion does NOT do is check the claims. The signature says who sent
    them and the digest says they arrived unaltered; neither says they are
    true. Promotion is a person deciding to believe a partner, recorded.
    """
    if not verifier_id or not str(verifier_id).strip():
        raise ImportRefused(
            "promote needs a verifier_id. Promotion makes another "
            "organisation's claims readable here, and a trust decision with "
            "nobody recorded against it is a decision nobody made."
        )
    result = _transition(
        conn, bundle_id, group_id=group_id,
        to=STATE_PROMOTED,
        from_states=(STATE_QUARANTINED,),
        stamp="promoted_at",
        detail={
            "promoted_by": str(verifier_id).strip(),
            "promoted_at": datetime.now(UTC).isoformat(),
        },
    )
    _logger.info("bundle_promoted", extra={"fields": {
        "bundle_id": bundle_id, "group_id": result["group_id"],
        "verifier_id": verifier_id,
    }})
    return result


def reject(conn, bundle_id: str, *, verifier_id: str, reason: str,
           group_id: str | None = None) -> dict:
    """Refuse an imported bundle a verifier has looked at and does not accept.

    Distinct from `revoke`, and the difference is not bookkeeping: nothing was
    ever trusted, so nothing was withdrawn and no answer this store gave was
    based on it. A reader of the log can tell "we looked and said no" from "we
    said yes and then changed our mind", and only the second raises the
    question of what was already acted on.

    The facts stay in the quarantine scope. Removing them is `forget --scope`,
    which is erasure and is a separate, operator-confirmed act with no undo.
    """
    if not verifier_id or not str(verifier_id).strip():
        raise ImportRefused("reject needs a verifier_id, for the same reason promote does")
    result = _transition(
        conn, bundle_id, group_id=group_id,
        to=STATE_REJECTED,
        from_states=(STATE_QUARANTINED,),
        stamp=None,
        detail={
            "rejected_by": str(verifier_id).strip(),
            "rejected_reason": reason,
            "rejected_at": datetime.now(UTC).isoformat(),
        },
    )
    _logger.info("bundle_rejected_by_verifier", extra={"fields": {
        "bundle_id": bundle_id, "verifier_id": verifier_id,
    }})
    return result


def revoke(conn, bundle_id: str, *, reason: str, group_id: str | None = None) -> dict:
    """Withdraw a bundle. Its scope stops being readable from now on.

    `readable_scopes` stops returning the scope, so every subsequent read is
    as if the import had never been promoted. The facts stay on the graph, in
    the quarantine scope, because deleting them is erasure and erasure is a
    separate operator-confirmed act with no undo.

    ## What revocation cannot do

    **Facts already returned into an agent's context cannot be recalled.** A
    claim this store handed to a model last week is in that conversation's
    history, in whatever the agent wrote from it, and in any downstream system
    that stored the output. Revoking the bundle does not reach any of them.

    That is a property of having handed data over, not a gap in this
    implementation, and no amount of further work here would change it - the
    same statement is true of every system that returns data to a caller it
    does not control. The reason it is written here is that it has to be shown
    to a user at the point it still matters, which is the EXPORT confirmation
    rather than this function: once a bundle has been built and sent, the
    recipient's revocation is theirs and the sender has none. `cli/exchange.py`
    prints it before an export is written.

    **It does not reach the exporter's copy.** Revoking an import is this
    deployment declining to believe a partner. The partner still holds what
    they sent, and if the direction that matters is the other one, the question
    is what was exported and to whom - which is what the export rows answer.
    """
    result = _transition(
        conn, bundle_id, group_id=group_id,
        to=STATE_REVOKED,
        from_states=(STATE_QUARANTINED, STATE_PROMOTED),
        stamp="revoked_at",
        detail={
            "revoked_reason": reason,
            "revoked_at": datetime.now(UTC).isoformat(),
        },
    )
    _logger.info("bundle_revoked", extra={"fields": {
        "bundle_id": bundle_id, "group_id": result["group_id"],
        "reason": reason,
    }})
    return result


def readable_scopes(conn, account: str, memory: str) -> list[str]:
    """The import scopes a read for this memory may include: promoted ones.

    This is the function a caller uses to decide what to pass to
    `query_across`, and it is the only place an import scope is handed out. A
    caller that builds its scope list from here cannot include a quarantined,
    revoked or rejected bundle by accident, because the string it would need is
    not in the returned list.

    Matched by prefix with `starts_with` rather than LIKE, so that an account
    or memory name containing a `%` is a name rather than a wildcard. The
    prefix itself comes from `quarantine_prefix`, which is the single place the
    shape of an import scope is derived.

    The prefix match is unaffected by 0038's composite key, and deliberately
    so: what makes two accounts' copies of one bundle separate here is that
    their scopes have different prefixes, which was already true. The key
    change is what lets both rows exist.

    `starts_with` is not an indexable predicate, so this is a sequential scan
    over the registry. Measured at 5,000 rows it is a Seq Scan with or without
    an index on group_id, which is why 0038 dropped the index 0035 added for
    this query. The scan is a real cost on a fan-out read and is recorded here
    rather than hidden: the registry holds one row per bundle ever exchanged,
    so it is small for a long time, and the fix is a different predicate
    rather than another index.
    """
    prefix = quarantine_prefix(account, memory)
    rows = conn.execute(
        """SELECT group_id FROM public.bundle_registry
            WHERE direction = 'import'
              AND state = 'promoted'
              AND starts_with(group_id, %s)
            ORDER BY promoted_at""",
        (prefix,),
    ).fetchall()
    return [r[0] for r in rows]
