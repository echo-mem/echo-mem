"""A bundle crosses an organisational boundary, and must not land anywhere a
read can reach until somebody says so.

Two deployments against one test database, which is the honest way to test
this: the thing that makes cross-deployment import hard is that an AGE
`graphid` is unique per DATABASE rather than globally, so two stores WILL issue
the same id for unrelated facts. A test using two databases would never
collide and would pass while the collision shipped. Here both exporters
genuinely produce origin id 1, and the test asserts the importer keeps them
apart.

The tests that matter are the refusals. A correct import is one path; a bundle
with a bad signature writing nothing, a replayed bundle_id, and a quarantined
scope staying out of `readable_scopes` are each a different way for this to be
worse than not having it.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from echo_memory.exchange import bundle as bundle_format
from echo_memory.exchange import importer, redact, sign
from echo_memory.infra.db import GRAPH_NAME as GRAPH
from echo_memory.infra.db import connect
from echo_memory.ingestion.write_episode import write_episode

SEED = "33" * 32
PARTNER_SEED = "44" * 32

EXPORTER_ENV = {
    sign.KEYS_ENV: f"partner-2026:{SEED}",
    sign.KEY_ID_ENV: "partner-2026",
}
# A second, unrelated signer. Its bundles must not verify against the first
# one's anchor, which is what makes "a trusted key set" mean anything.
OTHER_ENV = {
    sign.KEYS_ENV: f"partner-2026:{PARTNER_SEED}",
    sign.KEY_ID_ENV: "partner-2026",
}

DEPLOY_A = "dep-partner-a"
DEPLOY_B = "dep-partner-b"
LOCAL = "dep-ours"

ACCOUNT_A = "acct-partner"
ACCOUNT_US = "acct-ours"

SOURCE_SCOPE = f"acct:{ACCOUNT_A}:memory:shared"
OUR_SHARED_SCOPE = f"acct:{ACCOUNT_US}:memory:shared"

#: What `config.group_id("shared")` resolves to, which is the engine's own
#: scope shape rather than the cloud's. The two coexist on purpose: the engine
#: never constructs an account scope and the CLI never constructs a quarantine
#: one, so a CLI export reads from this and an import writes to the other.
CLI_SHARED_SCOPE = f"user:{ACCOUNT_US}:shared"


class HashedEmbedder:
    """A deterministic embedder over arbitrary text.

    `fake_embedder.VectorEmbedder` raises on anything it was not given a
    vector for, which is right for tests about similarity thresholds and wrong
    here: an import writes whatever claims the bundle holds, and registering
    each one would make every test about the embedder's bookkeeping. This
    derives a unit vector from the text instead, so the numbers are
    meaningless and reproducible, which is all these tests need.
    """

    dimension = 384

    def embed(self, text: str) -> list[float]:
        raw = (hashlib.sha256(text.encode()).digest() * 12)[: self.dimension]
        values = [b - 127.5 for b in raw]
        norm = sum(v * v for v in values) ** 0.5
        return [v / norm for v in values]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]


def _seed(conn, group_id: str, claims: list[tuple[str, str, str, str]]) -> list[str]:
    """Write claims as (source, target, relation, text) into a scope."""
    embedder = HashedEmbedder()
    written = []
    for source, target, relation, text in claims:
        result = write_episode(
            conn, group_id, "s-seed",
            [{"name": source, "type": "component"}, {"name": target, "type": "service"}],
            [{"source": source, "target": target, "relation_type": relation,
              "fact": text, "confidence": "extracted"}],
            None, embedder, project="seeded", agent_id="seeder", assume_new=True,
        )
        assert not result.get("error"), result
        written.extend(result["edges_created"])
    return written


def _export(conn, group_id: str, *, deployment: str, account: str,
            policy: str = "full", env=None):
    """Build and sign a bundle, the way the CLI does."""
    manifest, blob = bundle_format.build(
        conn, group_id, deployment_id=deployment, account=account,
        memory="shared", policy=redact.POLICIES[policy],
    )
    signature = sign.sign(manifest.unsigned(), env=env or EXPORTER_ENV)
    return manifest.signed(signature), blob


def _anchor(env=None) -> dict[str, sign.TrustedKey]:
    return {"partner-2026": sign.trust_anchor("partner-2026", env=env or EXPORTER_ENV)}


def _import(conn, manifest, facts, *, account=ACCOUNT_US, memory="shared",
            trusted=None):
    return importer.import_bundle(
        conn,
        manifest=manifest,
        facts=facts,
        account=account,
        memory=memory,
        embedder=HashedEmbedder(),
        trusted_keys=_anchor() if trusted is None else trusted,
        deployment_id=LOCAL,
    )


def _facts_in(conn, group_id: str) -> list[str]:
    rows = conn.execute(
        f"""SELECT (properties ->> '"fact"'::agtype)
              FROM {GRAPH}."FACT"
             WHERE (properties ->> '"group_id"'::agtype) = %s""",
        (group_id,),
    ).fetchall()
    return [r[0] for r in rows]


def _total_facts(conn) -> int:
    return conn.execute(f'SELECT count(*) FROM {GRAPH}."FACT"').fetchone()[0]


CLAIMS = [
    ("billing", "Razorpay", "uses", "billing uses Razorpay for recurring subscriptions"),
    ("deploys", "main", "runs_from", "deploys run only from the main branch"),
    ("ingest", "pgvector", "uses", "ingest writes embeddings through pgvector"),
]


# --- the happy path, and where the facts are not ------------------------


def test_an_imported_bundle_lands_in_quarantine_and_not_in_our_shared_scope(migrated_db):
    """The whole containment claim in one test. Imported facts must be
    somewhere ordinary retrieval is not given, and the destination's own shared
    scope is exactly where they must not be."""
    with connect(migrated_db) as conn:
        _seed(conn, SOURCE_SCOPE, CLAIMS)
        _seed(conn, OUR_SHARED_SCOPE, [("cloud", "Vercel", "deploys_to", "cloud deploys to Vercel")])

        manifest, blob = _export(conn, SOURCE_SCOPE, deployment=DEPLOY_A, account=ACCOUNT_A)
        parsed_manifest, facts = bundle_format.parse(
            bundle_format.canonical(manifest.to_dict()), blob
        )
        result = _import(conn, parsed_manifest, facts)

        quarantine = importer.quarantine_group_id(
            ACCOUNT_US, "shared", manifest.bundle_id
        )
        assert result["group_id"] == quarantine
        assert result["n_written"] == 3
        assert result["state"] == "quarantined"

        in_quarantine = _facts_in(conn, quarantine)
        assert len(in_quarantine) == 3
        assert "billing uses Razorpay for recurring subscriptions" in in_quarantine

        ours = _facts_in(conn, OUR_SHARED_SCOPE)
        assert ours == ["cloud deploys to Vercel"], (
            "an imported fact reached the destination's ordinary shared scope"
        )


def test_imported_facts_are_inferred_and_carry_their_origin(migrated_db):
    """`confidence` says how the writer came by a claim, and this store did not
    extract an imported one - it accepted somebody's assertion. The origin is
    qualified by the exporter's deployment id, because a graphid alone is
    ambiguous across deployments."""
    with connect(migrated_db) as conn:
        origin_ids = _seed(conn, SOURCE_SCOPE, CLAIMS)
        manifest, blob = _export(conn, SOURCE_SCOPE, deployment=DEPLOY_A, account=ACCOUNT_A)
        _parsed, facts = bundle_format.parse(
            bundle_format.canonical(manifest.to_dict()), blob
        )
        result = _import(conn, manifest, facts)

        rows = conn.execute(
            f"""SELECT * FROM cypher('{GRAPH}', $$
                MATCH ()-[e:FACT]->()
                WHERE e.group_id = $gid
                RETURN e.confidence, e.provenance, e.agent_id
            $$, %s) AS (confidence agtype, provenance agtype, agent_id agtype)""",
            (json.dumps({"gid": result["group_id"]}),),
        ).fetchall()

        assert len(rows) == 3
        for confidence, provenance, agent_id in rows:
            assert confidence == '"inferred"', confidence
            assert str(provenance).count("origin") >= 1
            assert DEPLOY_A in str(provenance)
            # Not an agent in this deployment. Nothing here asserted the claim.
            assert str(agent_id) == f'"import:{DEPLOY_A}"'

        origins = {json.loads(str(p))["origin"] for _c, p, _a in rows}
        assert origins == {f"{DEPLOY_A}:{eid}" for eid in origin_ids}


def test_the_origins_t_valid_is_kept_without_rewriting_the_local_one(migrated_db):
    """The local t_valid is when the fact arrived here, is attested, and must
    not be rewritten to the exporter's time. The exporter's value survives in
    provenance, so nothing is lost."""
    with connect(migrated_db) as conn:
        _seed(conn, SOURCE_SCOPE, CLAIMS[:1])
        manifest, blob = _export(conn, SOURCE_SCOPE, deployment=DEPLOY_A, account=ACCOUNT_A)
        _p, facts = bundle_format.parse(bundle_format.canonical(manifest.to_dict()), blob)
        result = _import(conn, manifest, facts)

        row = conn.execute(
            f"""SELECT * FROM cypher('{GRAPH}', $$
                MATCH ()-[e:FACT]->() WHERE e.group_id = $gid
                RETURN e.provenance, e.t_valid
            $$, %s) AS (provenance agtype, t_valid agtype)""",
            (json.dumps({"gid": result["group_id"]}),),
        ).fetchone()
        provenance = json.loads(str(row[0]))
        assert provenance["origin_t_valid"] == facts[0]["t_valid"]
        assert "session_id" in provenance, (
            "the provenance merge dropped the key the attestation covers"
        )


def test_the_attestation_chain_of_the_quarantine_scope_is_clean(migrated_db):
    """The provenance merge rewrites a map the digest partly covers. It is safe
    because `attestation.claims_of` reads only provenance.session_id and that
    key is preserved, but "is safe because" is a claim and this is the check."""
    from echo_memory import attestation

    with connect(migrated_db) as conn:
        _seed(conn, SOURCE_SCOPE, CLAIMS)
        manifest, blob = _export(conn, SOURCE_SCOPE, deployment=DEPLOY_A, account=ACCOUNT_A)
        _p, facts = bundle_format.parse(bundle_format.canonical(manifest.to_dict()), blob)
        result = _import(conn, manifest, facts)

        report = attestation.report(conn, result["group_id"])
        assert report.n_attested == 3
        assert report.is_clean(), (
            f"altered={report.altered} broken={report.broken_links} "
            f"vanished={report.vanished}"
        )


def test_the_bundle_never_carries_the_exporters_group_id(migrated_db):
    """A group id is the exporter's tenancy boundary. Carrying it would invite
    an importer to write into a scope named by the sender."""
    with connect(migrated_db) as conn:
        _seed(conn, SOURCE_SCOPE, CLAIMS)
        _manifest, blob = _export(conn, SOURCE_SCOPE, deployment=DEPLOY_A, account=ACCOUNT_A)

        assert SOURCE_SCOPE.encode() not in blob
        assert b"group_id" not in blob


def test_a_minimal_policy_export_carries_no_author(migrated_db):
    with connect(migrated_db) as conn:
        _seed(conn, SOURCE_SCOPE, CLAIMS)
        _m, blob = _export(
            conn, SOURCE_SCOPE, deployment=DEPLOY_A, account=ACCOUNT_A, policy="minimal"
        )
        assert b"agent_id" not in blob
        assert b"seeder" not in blob


def test_a_superseded_version_of_a_claim_is_not_exported(migrated_db):
    """Only live facts leave. The history of a claim is a changelog of the
    exporter's own revisions."""
    with connect(migrated_db) as conn:
        _seed(conn, SOURCE_SCOPE, [("billing", "Razorpay", "uses", "billing uses Razorpay")])
        _seed(conn, SOURCE_SCOPE, [("billing", "Razorpay", "uses", "billing uses Razorpay v2")])

        _m, blob = _export(conn, SOURCE_SCOPE, deployment=DEPLOY_A, account=ACCOUNT_A)
        assert b"billing uses Razorpay v2" in blob
        assert blob.count(b"billing uses Razorpay") == 1


# --- colliding origin ids ----------------------------------------------


def test_colliding_origin_ids_from_two_deployments_do_not_collide_locally(migrated_db):
    """The reason an imported fact gets a NEW local graphid.

    A graphid is unique per database, so two deployments issue the same id for
    unrelated facts. Both bundles here genuinely carry the same origin id; the
    import must keep them apart, and `provenance.origin` is what does it.
    """
    second_scope = "acct:acct-partner-b:memory:shared"
    with connect(migrated_db) as conn:
        a_ids = _seed(conn, SOURCE_SCOPE, [("alpha", "one", "uses", "alpha uses one")])
        b_ids = _seed(conn, second_scope, [("beta", "two", "uses", "beta uses two")])

        first, first_blob = _export(
            conn, SOURCE_SCOPE, deployment=DEPLOY_A, account=ACCOUNT_A
        )
        second, second_blob = _export(
            conn, second_scope, deployment=DEPLOY_B, account="acct-partner-b"
        )

        # Make the collision real rather than hoping for one: both bundles now
        # claim origin id 1, which is what two independent deployments at the
        # same point in their life would actually produce.
        def _force(manifest, blob, origin: str):
            _m, facts = bundle_format.parse(
                bundle_format.canonical(manifest.to_dict()), blob
            )
            for fact in facts:
                fact["origin_fact_id"] = origin
            new_blob = b"".join(bundle_format.canonical(f) + b"\n" for f in facts)
            rebuilt = bundle_format.Manifest(**{
                **manifest.to_dict(),
                "content_digest": bundle_format.digest_of(new_blob),
                "signature": None,
            })
            return rebuilt.signed(sign.sign(rebuilt.unsigned(), env=EXPORTER_ENV)), facts

        assert a_ids != b_ids, "the two seeded facts already share an id"
        first, first_facts = _force(first, first_blob, "1")
        second, second_facts = _force(second, second_blob, "1")

        first_result = _import(conn, first, first_facts)
        second_result = _import(conn, second, second_facts)

        assert first_result["group_id"] != second_result["group_id"]
        assert first_result["n_written"] == 1
        assert second_result["n_written"] == 1

        origins = {}
        for result in (first_result, second_result):
            row = conn.execute(
                f"""SELECT * FROM cypher('{GRAPH}', $$
                    MATCH ()-[e:FACT]->() WHERE e.group_id = $gid
                    RETURN id(e), e.provenance
                $$, %s) AS (edge_id agtype, provenance agtype)""",
                (json.dumps({"gid": result["group_id"]}),),
            ).fetchone()
            origins[str(row[0])] = json.loads(str(row[1]))["origin"]

        assert len(origins) == 2, "two imported facts share one local graphid"
        assert set(origins.values()) == {f"{DEPLOY_A}:1", f"{DEPLOY_B}:1"}, (
            "the deployment qualifier did not keep two identical origin ids apart"
        )


# --- the refusals -------------------------------------------------------


def test_a_bundle_with_a_bad_signature_writes_nothing(migrated_db):
    """Counted before and after. A refusal that left facts behind would be
    worse than no verification at all, because nothing would clean them up."""
    with connect(migrated_db) as conn:
        _seed(conn, SOURCE_SCOPE, CLAIMS)
        manifest, blob = _export(
            conn, SOURCE_SCOPE, deployment=DEPLOY_A, account=ACCOUNT_A, env=OTHER_ENV
        )
        _p, facts = bundle_format.parse(bundle_format.canonical(manifest.to_dict()), blob)

        before = _total_facts(conn)
        with pytest.raises(importer.ImportRefused, match="signature rejected"):
            _import(conn, manifest, facts)
        after = _total_facts(conn)

        assert after == before, f"{after - before} fact(s) were written by a refused import"
        assert importer.listing(conn) == [], "a refused bundle was registered"


def test_an_unsigned_bundle_is_refused(migrated_db):
    with connect(migrated_db) as conn:
        _seed(conn, SOURCE_SCOPE, CLAIMS)
        manifest, blob = bundle_format.build(
            conn, SOURCE_SCOPE, deployment_id=DEPLOY_A, account=ACCOUNT_A,
            memory="shared", policy=redact.POLICIES["full"],
        )
        _p, facts = bundle_format.parse(bundle_format.canonical(manifest.to_dict()), blob)

        before = _total_facts(conn)
        with pytest.raises(importer.ImportRefused, match="unsigned"):
            _import(conn, manifest, facts)
        assert _total_facts(conn) == before


def test_an_empty_trusted_set_refuses_and_writes_nothing(migrated_db):
    with connect(migrated_db) as conn:
        _seed(conn, SOURCE_SCOPE, CLAIMS)
        manifest, blob = _export(conn, SOURCE_SCOPE, deployment=DEPLOY_A, account=ACCOUNT_A)
        _p, facts = bundle_format.parse(bundle_format.canonical(manifest.to_dict()), blob)

        before = _total_facts(conn)
        with pytest.raises(importer.ImportRefused, match="no trusted keys"):
            _import(conn, manifest, facts, trusted={})
        assert _total_facts(conn) == before


def test_tampered_claims_under_a_valid_signature_are_refused(migrated_db):
    """The signature covers the manifest and the manifest carries the digest,
    so rewriting a claim without re-signing has to be caught by the recompute."""
    with connect(migrated_db) as conn:
        _seed(conn, SOURCE_SCOPE, CLAIMS)
        manifest, blob = _export(conn, SOURCE_SCOPE, deployment=DEPLOY_A, account=ACCOUNT_A)
        _p, facts = bundle_format.parse(bundle_format.canonical(manifest.to_dict()), blob)
        facts[0]["fact"] = "billing uses Stripe for recurring subscriptions"

        before = _total_facts(conn)
        with pytest.raises(importer.ImportRefused, match="content digest mismatch"):
            _import(conn, manifest, facts)
        assert _total_facts(conn) == before


def test_a_replayed_bundle_id_is_refused(migrated_db):
    """A bundle is a file, and a file gets replayed. Without this the second
    import doubles every fact as new local nodes nothing can tell apart."""
    with connect(migrated_db) as conn:
        _seed(conn, SOURCE_SCOPE, CLAIMS)
        manifest, blob = _export(conn, SOURCE_SCOPE, deployment=DEPLOY_A, account=ACCOUNT_A)
        _p, facts = bundle_format.parse(bundle_format.canonical(manifest.to_dict()), blob)

        _import(conn, manifest, facts)
        after_first = _total_facts(conn)

        with pytest.raises(importer.ImportRefused, match="already import"):
            _import(conn, manifest, facts)

        assert _total_facts(conn) == after_first, "a replay wrote facts"
        assert len(importer.listing(conn)) == 1


def test_a_fact_this_store_cannot_hold_refuses_the_whole_bundle(migrated_db):
    """All or nothing. Dropping the three claims that were too long would make
    the imported scope a subset nobody can identify."""
    with connect(migrated_db) as conn:
        _seed(conn, SOURCE_SCOPE, CLAIMS)
        manifest, blob = _export(conn, SOURCE_SCOPE, deployment=DEPLOY_A, account=ACCOUNT_A)
        _p, facts = bundle_format.parse(bundle_format.canonical(manifest.to_dict()), blob)
        facts[1]["fact"] = "x" * 5000
        blob = b"".join(bundle_format.canonical(f) + b"\n" for f in facts)
        rebuilt = bundle_format.Manifest(**{
            **manifest.to_dict(),
            "content_digest": bundle_format.digest_of(blob),
            "signature": None,
        })
        rebuilt = rebuilt.signed(sign.sign(rebuilt.unsigned(), env=EXPORTER_ENV))

        before = _total_facts(conn)
        with pytest.raises(importer.ImportRefused, match="refuses a fact over"):
            _import(conn, rebuilt, facts)
        assert _total_facts(conn) == before
        assert importer.listing(conn) == [], "a refused bundle was registered"


# --- promotion, revocation, and what a read may see --------------------


def test_readable_scopes_excludes_a_quarantined_bundle_and_includes_a_promoted_one(
    migrated_db,
):
    """The containment claim as a caller actually experiences it. This is the
    function that decides what reaches `query_across`."""
    with connect(migrated_db) as conn:
        _seed(conn, SOURCE_SCOPE, CLAIMS)
        manifest, blob = _export(conn, SOURCE_SCOPE, deployment=DEPLOY_A, account=ACCOUNT_A)
        _p, facts = bundle_format.parse(bundle_format.canonical(manifest.to_dict()), blob)
        result = _import(conn, manifest, facts)

        assert importer.readable_scopes(conn, ACCOUNT_US, "shared") == [], (
            "a quarantined bundle's scope was offered to a reader"
        )

        importer.promote(conn, manifest.bundle_id, verifier_id="ayush@ateleir")
        assert importer.readable_scopes(conn, ACCOUNT_US, "shared") == [
            result["group_id"]
        ]


def test_promote_requires_a_verifier_id(migrated_db):
    """A promotion makes another organisation's claims readable here. A trust
    decision with nobody recorded against it is a decision nobody made."""
    with connect(migrated_db) as conn:
        _seed(conn, SOURCE_SCOPE, CLAIMS)
        manifest, blob = _export(conn, SOURCE_SCOPE, deployment=DEPLOY_A, account=ACCOUNT_A)
        _p, facts = bundle_format.parse(bundle_format.canonical(manifest.to_dict()), blob)
        _import(conn, manifest, facts)

        for empty in ("", "   "):
            with pytest.raises(importer.ImportRefused, match="verifier_id"):
                importer.promote(conn, manifest.bundle_id, verifier_id=empty)

        with pytest.raises(TypeError):
            importer.promote(conn, manifest.bundle_id)  # type: ignore[call-arg]

        assert importer.get(conn, manifest.bundle_id)["state"] == "quarantined"
        assert importer.readable_scopes(conn, ACCOUNT_US, "shared") == []


def test_a_promotion_records_who_made_it(migrated_db):
    with connect(migrated_db) as conn:
        _seed(conn, SOURCE_SCOPE, CLAIMS)
        manifest, blob = _export(conn, SOURCE_SCOPE, deployment=DEPLOY_A, account=ACCOUNT_A)
        _p, facts = bundle_format.parse(bundle_format.canonical(manifest.to_dict()), blob)
        _import(conn, manifest, facts)

        row = importer.promote(conn, manifest.bundle_id, verifier_id="ayush@ateleir")
        assert row["state"] == "promoted"
        assert row["promoted_at"] is not None
        assert row["detail"]["promoted_by"] == "ayush@ateleir"


def test_revoke_makes_the_scope_unreadable(migrated_db):
    with connect(migrated_db) as conn:
        _seed(conn, SOURCE_SCOPE, CLAIMS)
        manifest, blob = _export(conn, SOURCE_SCOPE, deployment=DEPLOY_A, account=ACCOUNT_A)
        _p, facts = bundle_format.parse(bundle_format.canonical(manifest.to_dict()), blob)
        result = _import(conn, manifest, facts)
        importer.promote(conn, manifest.bundle_id, verifier_id="ayush@ateleir")
        assert importer.readable_scopes(conn, ACCOUNT_US, "shared") == [result["group_id"]]

        row = importer.revoke(conn, manifest.bundle_id, reason="partner withdrew it")
        assert row["state"] == "revoked"
        assert row["revoked_at"] is not None
        assert row["detail"]["revoked_reason"] == "partner withdrew it"

        assert importer.readable_scopes(conn, ACCOUNT_US, "shared") == []
        # The facts are still there. Removing them is erasure, which is a
        # separate operator-confirmed act with no undo.
        assert len(_facts_in(conn, result["group_id"])) == 3


def test_a_quarantined_bundle_can_be_revoked_without_being_promoted_first(migrated_db):
    with connect(migrated_db) as conn:
        _seed(conn, SOURCE_SCOPE, CLAIMS)
        manifest, blob = _export(conn, SOURCE_SCOPE, deployment=DEPLOY_A, account=ACCOUNT_A)
        _p, facts = bundle_format.parse(bundle_format.canonical(manifest.to_dict()), blob)
        _import(conn, manifest, facts)

        assert importer.revoke(conn, manifest.bundle_id, reason="no")["state"] == "revoked"
        with pytest.raises(importer.ImportRefused, match="quarantined"):
            importer.promote(conn, manifest.bundle_id, verifier_id="ayush@ateleir")


def test_a_rejected_bundle_never_becomes_readable(migrated_db):
    """Distinct from revoked: nothing was ever trusted, so no answer this
    store gave was based on it."""
    with connect(migrated_db) as conn:
        _seed(conn, SOURCE_SCOPE, CLAIMS)
        manifest, blob = _export(conn, SOURCE_SCOPE, deployment=DEPLOY_A, account=ACCOUNT_A)
        _p, facts = bundle_format.parse(bundle_format.canonical(manifest.to_dict()), blob)
        _import(conn, manifest, facts)

        row = importer.reject(
            conn, manifest.bundle_id, verifier_id="ayush@ateleir", reason="unsourced"
        )
        assert row["state"] == "rejected"
        assert row["detail"]["rejected_by"] == "ayush@ateleir"
        assert importer.readable_scopes(conn, ACCOUNT_US, "shared") == []


def test_one_memorys_promoted_import_is_not_readable_from_another(migrated_db):
    """The prefix is per memory, so promoting a bundle into `shared` must not
    make it visible to a read scoped to `dev`."""
    with connect(migrated_db) as conn:
        _seed(conn, SOURCE_SCOPE, CLAIMS)
        manifest, blob = _export(conn, SOURCE_SCOPE, deployment=DEPLOY_A, account=ACCOUNT_A)
        _p, facts = bundle_format.parse(bundle_format.canonical(manifest.to_dict()), blob)
        _import(conn, manifest, facts, memory="shared")
        importer.promote(conn, manifest.bundle_id, verifier_id="ayush@ateleir")

        assert importer.readable_scopes(conn, ACCOUNT_US, "dev") == []
        assert importer.readable_scopes(conn, "acct-someone-else", "shared") == []
        assert len(importer.readable_scopes(conn, ACCOUNT_US, "shared")) == 1


def test_an_export_row_cannot_be_promoted(migrated_db):
    """What left this deployment is in somebody else's store now."""
    with connect(migrated_db) as conn:
        _seed(conn, SOURCE_SCOPE, CLAIMS)
        manifest, _blob = _export(conn, SOURCE_SCOPE, deployment=LOCAL, account=ACCOUNT_US)
        importer.register_export(conn, manifest, group_id=SOURCE_SCOPE, peer_account="them")

        with pytest.raises(importer.ImportRefused, match="is an export"):
            importer.promote(conn, manifest.bundle_id, verifier_id="ayush@ateleir")


def test_an_export_is_recorded_as_created_and_never_as_delivered(migrated_db):
    """This deployment cannot know what happened to a file after it wrote it."""
    with connect(migrated_db) as conn:
        _seed(conn, SOURCE_SCOPE, CLAIMS)
        manifest, _blob = _export(conn, SOURCE_SCOPE, deployment=LOCAL, account=ACCOUNT_US)
        row = importer.register_export(
            conn, manifest, group_id=SOURCE_SCOPE, peer_account="acct-them",
        )

        assert row["state"] == "created"
        assert row["direction"] == "export"
        assert row["fact_count"] == 3
        assert row["signature_key_id"] == "partner-2026"
        assert row["source_chain_head"]["seq"] == 3


def test_promoting_an_unknown_bundle_says_so(migrated_db):
    with connect(migrated_db) as conn, pytest.raises(
        importer.ImportRefused, match="no bundle"
    ):
        importer.promote(conn, "not-a-bundle", verifier_id="ayush@ateleir")


# --- the operator surface, and the gates on it --------------------------


def test_no_tool_published_over_mcp_can_exchange_knowledge():
    """Handing one organisation's memory to another, or deciding to believe
    what another organisation sent, must not be one prompt injection away from
    any agent holding a key.

    Reads the tools the server actually publishes rather than the source, so
    this fails when somebody registers one, not when somebody names a function.
    """
    from echo_memory import server

    published = {t.name for t in server.server._tool_manager.list_tools()}

    assert published, "precondition: the server publishes some tools"
    forbidden = {
        "exchange", "export_bundle", "import_bundle", "promote", "promote_bundle",
        "revoke", "revoke_bundle", "reject_bundle", "trust", "verify_bundle",
    }
    assert not (published & forbidden), (
        f"knowledge exchange must not be reachable over MCP: "
        f"{sorted(published & forbidden)}"
    )


def _cli_env(monkeypatch, migrated_db) -> None:
    monkeypatch.setenv("ECHO_MEMORY_USER_ID", ACCOUNT_US)
    monkeypatch.setenv("ECHO_MEMORY_AGENT_ID", "claude-code")
    monkeypatch.setenv("ECHO_MEMORY_DATABASE_URL", migrated_db)
    monkeypatch.setenv(sign.KEYS_ENV, EXPORTER_ENV[sign.KEYS_ENV])
    monkeypatch.setenv(sign.KEY_ID_ENV, EXPORTER_ENV[sign.KEY_ID_ENV])
    # The CLI reaches for the real model. These tests are about the gates, not
    # about embedding quality, and loading a transformer to prove that
    # --confirm is load-bearing would make the gate tests the slowest in the
    # suite.
    monkeypatch.setattr(
        "echo_memory.ingestion.embeddings.LocalEmbedder", HashedEmbedder
    )


def test_export_without_confirm_writes_nothing(migrated_db, monkeypatch, tmp_path, capsys):
    """The gate. Data that has left cannot be recalled, and the audit is cheap
    to compute, so showing it first is free."""
    from echo_memory.cli.main import main

    with connect(migrated_db) as conn:
        _seed(conn, CLI_SHARED_SCOPE, CLAIMS)
    _cli_env(monkeypatch, migrated_db)

    code = main([
        "--scope", "shared", "exchange", "export",
        "--out", str(tmp_path), "--deployment-id", LOCAL, "--policy", "full",
    ])

    out = capsys.readouterr().out
    assert code == 0
    assert list(tmp_path.iterdir()) == [], "a dry run wrote a bundle"
    assert "3 would be sent" in out
    assert "cannot be recalled" in out, (
        "the export confirmation does not say that handing data over is final"
    )
    assert "Re-run with --confirm" in out
    with connect(migrated_db) as conn:
        assert importer.listing(conn) == []


def test_export_with_confirm_writes_a_signed_bundle_and_records_it(
    migrated_db, monkeypatch, tmp_path, capsys
):
    from echo_memory.cli.main import main

    with connect(migrated_db) as conn:
        _seed(conn, CLI_SHARED_SCOPE, CLAIMS)
    _cli_env(monkeypatch, migrated_db)

    code = main([
        "--scope", "shared", "exchange", "export",
        "--out", str(tmp_path), "--deployment-id", LOCAL, "--policy", "full",
        "--peer-account", "acct-them", "--confirm",
    ])
    assert code == 0

    manifests = list(tmp_path.glob("*.manifest.json"))
    blobs = list(tmp_path.glob("*.facts.ndjson"))
    assert len(manifests) == 1 and len(blobs) == 1

    manifest, claims = bundle_format.parse(
        manifests[0].read_bytes(), blobs[0].read_bytes()
    )
    assert len(claims) == 3
    assert sign.verify(manifest, trusted_keys=_anchor())

    with connect(migrated_db) as conn:
        rows = importer.listing(conn)
    assert len(rows) == 1
    assert rows[0]["direction"] == "export"
    assert rows[0]["state"] == "created"
    assert rows[0]["peer_account"] == "acct-them"


def test_the_manifest_on_disk_is_the_bytes_that_were_signed(
    migrated_db, monkeypatch, tmp_path
):
    """A pretty-printed copy would fail verification in a way the recipient
    could not distinguish from tampering."""
    from echo_memory.cli.main import main

    with connect(migrated_db) as conn:
        _seed(conn, CLI_SHARED_SCOPE, CLAIMS)
    _cli_env(monkeypatch, migrated_db)
    main([
        "--scope", "shared", "exchange", "export", "--out", str(tmp_path),
        "--deployment-id", LOCAL, "--confirm",
    ])

    raw = next(tmp_path.glob("*.manifest.json")).read_bytes()
    manifest, _ = bundle_format.parse(raw, next(tmp_path.glob("*.facts.ndjson")).read_bytes())
    assert raw == bundle_format.canonical(manifest.to_dict())


def test_import_without_confirm_writes_nothing(migrated_db, monkeypatch, tmp_path, capsys):
    from echo_memory.cli.main import main

    with connect(migrated_db) as conn:
        _seed(conn, SOURCE_SCOPE, CLAIMS)
        manifest, blob = _export(conn, SOURCE_SCOPE, deployment=DEPLOY_A, account=ACCOUNT_A)
        before = _total_facts(conn)
    manifest_path = tmp_path / "m.json"
    facts_path = tmp_path / "f.ndjson"
    manifest_path.write_bytes(bundle_format.canonical(manifest.to_dict()))
    facts_path.write_bytes(blob)
    anchor = sign.trust_anchor("partner-2026", env=EXPORTER_ENV)
    _cli_env(monkeypatch, migrated_db)

    code = main([
        "--scope", "shared", "exchange", "import",
        "--manifest", str(manifest_path), "--facts", str(facts_path),
        "--trust", f"{anchor.key_id}:{anchor.alg}:{anchor.material.hex()}",
        "--deployment-id", LOCAL,
    ])

    out = capsys.readouterr().out
    assert code == 0
    assert "signature: OK" in out
    assert "Nothing has been imported" in out
    with connect(migrated_db) as conn:
        assert _total_facts(conn) == before
        assert importer.listing(conn) == []


def test_a_cli_round_trip_quarantines_then_promotes(
    migrated_db, monkeypatch, tmp_path, capsys
):
    """Export, import, promote, through main(), because the layer where
    --confirm lives is the only thing between a typo and a trust decision."""
    from echo_memory.cli.main import main

    with connect(migrated_db) as conn:
        _seed(conn, SOURCE_SCOPE, CLAIMS)
        manifest, blob = _export(conn, SOURCE_SCOPE, deployment=DEPLOY_A, account=ACCOUNT_A)
    manifest_path = tmp_path / "m.json"
    facts_path = tmp_path / "f.ndjson"
    manifest_path.write_bytes(bundle_format.canonical(manifest.to_dict()))
    facts_path.write_bytes(blob)
    anchor = sign.trust_anchor("partner-2026", env=EXPORTER_ENV)
    trust = f"{anchor.key_id}:{anchor.alg}:{anchor.material.hex()}"
    _cli_env(monkeypatch, migrated_db)

    assert main([
        "--scope", "shared", "exchange", "import",
        "--manifest", str(manifest_path), "--facts", str(facts_path),
        "--trust", trust, "--deployment-id", LOCAL, "--confirm",
    ]) == 0

    quarantine = importer.quarantine_group_id(ACCOUNT_US, "shared", manifest.bundle_id)
    with connect(migrated_db) as conn:
        assert len(_facts_in(conn, quarantine)) == 3
        assert importer.readable_scopes(conn, ACCOUNT_US, "shared") == []

    # Promotion is a trust grant, so it has a gate of its own.
    capsys.readouterr()
    assert main([
        "exchange", "promote", manifest.bundle_id, "--verifier", "ayush@ateleir",
    ]) == 0
    assert "Nothing has changed" in capsys.readouterr().out
    with connect(migrated_db) as conn:
        assert importer.readable_scopes(conn, ACCOUNT_US, "shared") == []

    assert main([
        "exchange", "promote", manifest.bundle_id, "--verifier", "ayush@ateleir",
        "--confirm",
    ]) == 0
    with connect(migrated_db) as conn:
        assert importer.readable_scopes(conn, ACCOUNT_US, "shared") == [quarantine]

    # Revoke has no gate, deliberately: it only ever makes less readable, and
    # friction in front of the safe direction is friction in the wrong place.
    capsys.readouterr()
    assert main(["exchange", "revoke", manifest.bundle_id, "--reason", "withdrawn"]) == 0
    assert "does not recall facts already returned" in capsys.readouterr().out
    with connect(migrated_db) as conn:
        assert importer.readable_scopes(conn, ACCOUNT_US, "shared") == []


def test_the_cli_refuses_a_bundle_whose_key_is_not_trusted(
    migrated_db, monkeypatch, tmp_path, capsys
):
    from echo_memory.cli.main import main

    with connect(migrated_db) as conn:
        _seed(conn, SOURCE_SCOPE, CLAIMS)
        manifest, blob = _export(conn, SOURCE_SCOPE, deployment=DEPLOY_A, account=ACCOUNT_A)
        before = _total_facts(conn)
    manifest_path = tmp_path / "m.json"
    facts_path = tmp_path / "f.ndjson"
    manifest_path.write_bytes(bundle_format.canonical(manifest.to_dict()))
    facts_path.write_bytes(blob)
    _cli_env(monkeypatch, migrated_db)

    # No --trust at all, which is an empty anchor set and must refuse.
    code = main([
        "--scope", "shared", "exchange", "import",
        "--manifest", str(manifest_path), "--facts", str(facts_path),
        "--deployment-id", LOCAL, "--confirm",
    ])
    out = capsys.readouterr().out
    assert code == 1
    assert "REFUSED" in out
    with connect(migrated_db) as conn:
        assert _total_facts(conn) == before


def test_the_anchor_command_prints_a_public_key_not_the_seed(
    migrated_db, monkeypatch, capsys
):
    from echo_memory.cli.main import main

    _cli_env(monkeypatch, migrated_db)
    assert main(["exchange", "anchor", "--key-id", "partner-2026"]) == 0

    out = capsys.readouterr().out
    assert SEED not in out, "the anchor command printed the private seed"
    assert "safe to publish" in out
    anchors = sign.parse_trusted(out.splitlines()[0])
    assert anchors["partner-2026"].alg == sign.ALG_ED25519


def test_list_says_an_export_row_is_not_evidence_of_delivery(
    migrated_db, monkeypatch, capsys
):
    from echo_memory.cli.main import main

    with connect(migrated_db) as conn:
        _seed(conn, SOURCE_SCOPE, CLAIMS)
        manifest, _blob = _export(conn, SOURCE_SCOPE, deployment=LOCAL, account=ACCOUNT_US)
        importer.register_export(conn, manifest, group_id=SOURCE_SCOPE, peer_account="them")
    _cli_env(monkeypatch, migrated_db)

    assert main(["exchange", "list"]) == 0
    out = capsys.readouterr().out
    assert manifest.bundle_id in out
    assert "cannot" in out and "what happened to a file" in out
