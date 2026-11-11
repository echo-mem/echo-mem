"""`echo-memory exchange`: the operator surface for cross-organisation bundles.

Deliberately not MCP tools, and there is a test that reads the published tool
list so registering one later fails loudly. `cli/forget.py` records the
reasoning and it applies here with one word changed: handing an organisation's
memory to another organisation, or deciding to believe what another
organisation sent, one prompt injection away from any agent holding a key, is
not a trade worth making. The standing rule that anything an agent does as
part of a loop must be reachable over MCP does not apply, because none of
these is a step in an agent's loop - each is a person deciding something about
another company.

## Which commands need --confirm, and why it is not all of them

    export    --confirm   data leaves and cannot be recalled
    import    --confirm   another organisation's claims enter the store
    promote   --confirm   those claims become answers this store will give
    revoke                only ever makes less readable
    reject                only ever makes less readable

The asymmetry is the point. A gate belongs in front of the direction that
cannot be undone, and friction in front of the safe direction is friction in
the wrong place: an operator who has just realised a bundle should not have
been trusted should be able to revoke it in one command, at two in the
morning, without reading a prompt first.
"""

from __future__ import annotations

import json
from pathlib import Path

from echo_memory.exchange import bundle as bundle_format
from echo_memory.exchange import importer, redact, sign

#: How many claims a dry run prints before summarising. Enough to see what the
#: bundle actually holds, short enough to read.
PREVIEW = 10


def _print_preview(claims: list[dict]) -> None:
    for claim in claims[:PREVIEW]:
        print(f"  - {claim.get('fact')}")
    if len(claims) > PREVIEW:
        print(f"  ... and {len(claims) - PREVIEW} more")


def _trusted(args) -> dict[str, sign.TrustedKey]:
    """The trust anchors this import will be verified against.

    Assembled from what the operator passed and from nowhere else. There is no
    fallback to an environment variable or a stored set, because the one way to
    get this wrong is to end up verifying a bundle against a key that travelled
    with it - and a default is how that happens by accident.
    """
    spec = args.trust or ""
    if args.trust_file:
        spec = ",".join(
            filter(None, (spec, Path(args.trust_file).read_text().strip()))
        )
    return sign.parse_trusted(spec) if spec.strip() else {}


# --- export -------------------------------------------------------------


def _export(args, config, conn) -> int:
    group_id = config.group_id(args.scope)
    try:
        policy = redact.get(args.policy)
    except KeyError as e:
        print(f"error: {e}")
        return 2

    rows = bundle_format.live_facts(conn, group_id)
    report = redact.audit(policy, rows)

    print(
        f"Exporting from {group_id} under policy {report['policy_id']!r}:\n"
        f"  {report['n_in']} live fact(s) in the scope\n"
        f"  {report['n_out']} would be sent\n"
        f"  {report['n_dropped']} held back "
        f"({', '.join(f'{k}: {v}' for k, v in report['dropped'].items() if v) or 'none'})"
    )
    if report["relations_dropped"]:
        print(f"  relations held back: {', '.join(report['relations_dropped'])}")
    if report["entity_types_dropped"]:
        print(f"  entity types held back: {', '.join(report['entity_types_dropped'])}")
    for rule, n in report["stripped"].items():
        if n:
            print(f"  {rule} stripped from {n} fact(s)")
    if report["preview"]:
        print("\nHeld back:")
        for text in report["preview"]:
            print(f"  - {text}")

    if not report["n_out"]:
        print("\nNothing would be sent. No bundle written.")
        return 0

    if not args.confirm:
        print(
            f"\nThis would write a bundle of {report['n_out']} fact(s) to "
            f"{args.out}.\n"
            f"\nOnce a bundle is handed over it cannot be recalled. Facts in "
            f"it are\nin the recipient's store, in whatever their agents read "
            f"from it, and in\nanything downstream that kept the output. "
            f"Revoking your own copy does not\nreach any of that, and neither "
            f"does erasing the facts here. This is the\nlast point at which "
            f"the decision is still yours.\n"
            f"\nNothing has been written. Re-run with --confirm to build it."
        )
        return 0

    manifest, blob = bundle_format.build(
        conn,
        group_id,
        deployment_id=args.deployment_id,
        account=args.account or config.user_id,
        memory=args.memory or args.scope,
        policy=policy,
    )

    if args.unsigned:
        print(
            "\nwarning: --unsigned. No importer will accept this bundle, and "
            "a recipient\nwho does has no way to tell it came from you."
        )
    else:
        try:
            manifest = manifest.signed(
                sign.sign(manifest.unsigned(), alg=args.alg, env=None)
            )
        except sign.SignatureError as e:
            print(f"error: {e}")
            return 1

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / f"{manifest.bundle_id}.manifest.json"
    facts_path = out / f"{manifest.bundle_id}.facts.ndjson"
    # The manifest is written as canonical bytes, which is what was signed. A
    # pretty-printed copy would not verify, and the recipient could not tell
    # that from tampering.
    manifest_path.write_bytes(bundle_format.canonical(manifest.to_dict()))
    facts_path.write_bytes(blob)

    importer.register_export(
        conn,
        manifest,
        group_id=group_id,
        peer_deployment=args.peer_deployment,
        peer_account=args.peer_account,
        detail={"policy_audit": {k: report[k] for k in ("dropped", "stripped", "n_in")}},
    )

    signature = manifest.signature or {}
    print(
        f"\nWrote bundle {manifest.bundle_id}\n"
        f"  {manifest_path}\n"
        f"  {facts_path}\n"
        f"  {manifest.fact_count} fact(s), digest {manifest.content_digest}\n"
        f"  signed: {signature.get('alg', 'NO')} "
        f"{signature.get('key_id', '')}".rstrip()
    )
    if manifest.source_chain_head:
        print(
            f"  attestation head at build time: seq "
            f"{manifest.source_chain_head['seq']}, "
            f"{manifest.source_chain_head['digest']}"
        )
    if not args.unsigned and signature.get("key_id"):
        print(
            f"\nThe recipient needs your trust anchor to verify this. Print it "
            f"with:\n"
            f"  echo-memory exchange anchor --key-id {signature['key_id']}\n"
            f"Send it over a channel the bundle does not travel on, or the "
            f"signature\nproves nothing."
        )
    print("\nRecorded as an export. This deployment cannot know what happens to the file.")
    return 0


# --- import -------------------------------------------------------------


def _import(args, config, conn) -> int:
    try:
        manifest, claims = bundle_format.parse(
            Path(args.manifest).read_bytes(), Path(args.facts).read_bytes()
        )
    except bundle_format.BundleError as e:
        print(f"error: {e}")
        return 1
    except OSError as e:
        print(f"error: {e}")
        return 2

    trusted = _trusted(args)
    ok, reason = sign.check(manifest, trusted_keys=trusted)

    account = args.account or config.user_id
    memory = args.memory or args.scope
    quarantine = importer.quarantine_group_id(account, memory, manifest.bundle_id)

    print(
        f"Bundle {manifest.bundle_id}\n"
        f"  from deployment {manifest.deployment_id!r}, account "
        f"{manifest.account!r}, memory {manifest.memory!r}\n"
        f"  built {manifest.created_at} under policy "
        f"{manifest.redaction_policy_id!r}\n"
        f"  {manifest.fact_count} claim(s), digest {manifest.content_digest}\n"
        f"  signature: {'OK - ' if ok else 'REFUSED - '}{reason}"
    )
    if manifest.source_chain_head:
        head = manifest.source_chain_head
        print(
            f"  the exporter's chain head at build time: seq {head['seq']}, "
            f"{head['digest']}\n"
            f"  compare that against a head they published somewhere this "
            f"database\n  cannot reach. An attestation chain is per scope, so "
            f"these facts do not\n  carry theirs and each one cannot be "
            f"verified individually."
        )

    if not ok:
        print("\nNothing has been imported.")
        return 1

    print(f"\nWould write {manifest.fact_count} claim(s) into {quarantine}:")
    _print_preview(claims)
    print(
        f"\nThat scope is not in the list an ordinary read is given. The facts "
        f"become\nreadable only when a verifier promotes the bundle:\n"
        f"  echo-memory exchange promote {manifest.bundle_id} --verifier WHO --confirm"
    )

    if not args.confirm:
        print("\nNothing has been imported. Re-run with --confirm to write it.")
        return 0

    from echo_memory.ingestion.embeddings import LocalEmbedder

    embedder = LocalEmbedder()
    # The first call loads the model. Out here so a slow import is slow for a
    # reason the operator can see, rather than inside the transaction.
    embedder.embed("warm")

    try:
        result = importer.import_bundle(
            conn,
            manifest=manifest,
            facts=claims,
            account=account,
            memory=memory,
            embedder=embedder,
            trusted_keys=trusted,
            deployment_id=args.deployment_id,
        )
    except importer.ImportRefused as e:
        print(f"\nerror: {e}")
        return 1

    print(
        f"\nImported {result['n_written']} fact(s) into {result['group_id']}\n"
        f"  {result['n_restated']} claim(s) were already there word for word\n"
        f"  confidence: {importer.IMPORTED_CONFIDENCE}, trust state: "
        f"{result['trust_state']}, content class: {result['content_class']}\n"
        f"  state: {result['state']}, so nothing reads them yet"
    )
    return 0


# --- the trust decisions ------------------------------------------------


def _promote(args, config, conn) -> int:
    try:
        row = importer.get(conn, args.bundle_id)
    except importer.ImportRefused as e:
        print(f"error: {e}")
        return 1

    print(
        f"Bundle {row['bundle_id']} from {row['peer_deployment']!r} "
        f"({row['peer_account']!r})\n"
        f"  {row['fact_count']} fact(s) in {row['group_id']}\n"
        f"  state: {row['state']}\n"
        f"  signed by {row['signature_key_id']!r} using {row['signature_alg']!r}"
    )

    if not args.confirm:
        print(
            f"\nPromoting makes those facts answers this store will give. The "
            f"signature\nsays who sent them and the digest says they arrived "
            f"unaltered; neither says\nthey are true. This is a decision to "
            f"believe {row['peer_account']!r}, recorded\nagainst "
            f"{args.verifier!r}.\n"
            f"\nNothing has changed. Re-run with --confirm to promote it."
        )
        return 0

    try:
        row = importer.promote(conn, args.bundle_id, verifier_id=args.verifier)
    except importer.ImportRefused as e:
        print(f"error: {e}")
        return 1

    print(
        f"\nPromoted {row['bundle_id']} by {row['detail']['promoted_by']}.\n"
        f"  {row['group_id']} is now in the list a read for this memory is given."
    )
    return 0


def _revoke(args, config, conn) -> int:
    try:
        row = importer.revoke(conn, args.bundle_id, reason=args.reason)
    except importer.ImportRefused as e:
        print(f"error: {e}")
        return 1

    print(
        f"Revoked {row['bundle_id']}.\n"
        f"  {row['group_id']} is no longer readable, from now on\n"
        f"  its {row['fact_count']} fact(s) are still on the graph; removing "
        f"them is\n  `echo-memory forget --scope`, which is erasure and has no undo\n"
        f"\nThis does not recall facts already returned into an agent's "
        f"context. Those\nare in that conversation's history, in whatever the "
        f"agent wrote from them, and\nin anything downstream that kept the "
        f"output. That is a property of having\nhanded data over rather than "
        f"something more work here would fix."
    )
    return 0


def _reject(args, config, conn) -> int:
    try:
        row = importer.reject(
            conn, args.bundle_id, verifier_id=args.verifier, reason=args.reason
        )
    except importer.ImportRefused as e:
        print(f"error: {e}")
        return 1
    print(
        f"Rejected {row['bundle_id']} by {row['detail']['rejected_by']}: "
        f"{args.reason}\n"
        f"  nothing was ever readable, so nothing has been withdrawn\n"
        f"  the facts stay in {row['group_id']}; removing them is erasure"
    )
    return 0


# --- reading the log ----------------------------------------------------


def _list(args, config, conn) -> int:
    rows = importer.listing(conn, state=args.state)
    if not rows:
        where = f" in state {args.state!r}" if args.state else ""
        print(f"No bundle{where} has been recorded here.")
        return 0

    print(f"{len(rows)} bundle(s):\n")
    for row in rows:
        when = row["created_at"].date().isoformat()
        peer = row["peer_account"] or row["peer_deployment"] or "-"
        print(
            f"  {row['bundle_id']}  {when}  {row['direction']:<6} "
            f"{row['state']:<12} {row['fact_count']:>4} fact(s)  {peer}"
        )
        print(f"        {row['group_id']}  policy {row['redaction_policy_id']!r}")
        detail = row["detail"] or {}
        for key in ("promoted_by", "rejected_by", "revoked_reason", "rejected_reason"):
            if detail.get(key):
                print(f"        {key}: {detail[key]}")
    print(
        "\nAn export row says what left, not that it arrived: this deployment "
        "cannot\nknow what happened to a file after it wrote it."
    )
    return 0


def _anchor(args, config, conn) -> int:
    """Print the trust anchor a partner needs to verify this deployment's
    bundles, in the form `parse_trusted` reads."""
    try:
        anchor = sign.trust_anchor(args.key_id)
    except sign.SignatureError as e:
        print(f"error: {e}")
        return 1

    print(f"{anchor.key_id}:{anchor.alg}:{anchor.material.hex()}")
    print(f"\n{anchor.describe()}")
    if anchor.alg == sign.ALG_HMAC:
        print(
            "\nwarning: this is a SHARED SECRET, not a public key. Anybody "
            "holding it can\nsign bundles as you. Do not publish it."
        )
    else:
        print(
            "\nThis is a public key and is safe to publish. Send it over a "
            "channel your\nbundles do not travel on, or a recipient cannot "
            "tell your anchor from one an\nattacker supplied."
        )
    return 0


_SUBCOMMANDS = {
    "export": _export,
    "import": _import,
    "promote": _promote,
    "revoke": _revoke,
    "reject": _reject,
    "list": _list,
    "anchor": _anchor,
}


def run(args, config, conn) -> int:
    return _SUBCOMMANDS[args.exchange_command](args, config, conn)


def add_parser(sub) -> None:
    """Wire `exchange` into the CLI.

    Here rather than in `main.py` so that adding a subcommand does not mean
    editing a file that already holds thirty parsers, which is the reason
    `main.py` says its dispatch is a dict.
    """
    parser = sub.add_parser(
        "exchange",
        help="signed, redacted knowledge bundles between organisations",
    )
    commands = parser.add_subparsers(dest="exchange_command", required=True)

    export = commands.add_parser(
        "export", help="build a signed bundle from a scope. Dry run without --confirm"
    )
    export.add_argument(
        "--out", default=".", metavar="DIR", help="where to write the two files"
    )
    export.add_argument(
        "--policy", metavar="ID",
        help=f"redaction policy: {', '.join(sorted(redact.POLICIES))} "
             f"(default: {redact.DEFAULT_POLICY})",
    )
    export.add_argument(
        "--deployment-id", required=True, metavar="ID", dest="deployment_id",
        help="this deployment's identifier, which qualifies every origin id "
             "in the bundle. Stable per deployment and distinct between them",
    )
    export.add_argument("--account", metavar="ID", help="default: ECHO_MEMORY_USER_ID")
    export.add_argument("--memory", metavar="NAME", help="default: the scope name")
    export.add_argument(
        "--peer-account", metavar="ID", dest="peer_account",
        help="who this is for, recorded in the log and trusted for nothing",
    )
    export.add_argument(
        "--peer-deployment", metavar="ID", dest="peer_deployment",
        help="their deployment, recorded in the log",
    )
    export.add_argument(
        "--alg", choices=sign.ALGORITHMS,
        help=f"signature algorithm (default: {sign.preferred_alg()})",
    )
    export.add_argument(
        "--unsigned", action="store_true",
        help="build without signing. No importer will accept the result",
    )
    export.add_argument(
        "--confirm", action="store_true",
        help="actually write the bundle. Without it, nothing leaves",
    )

    imp = commands.add_parser(
        "import", help="verify a bundle and quarantine it. Dry run without --confirm"
    )
    imp.add_argument("--manifest", required=True, metavar="PATH")
    imp.add_argument("--facts", required=True, metavar="PATH")
    imp.add_argument(
        "--trust", metavar="SPEC",
        help="trust anchors as key_id:alg:hexkey[,...]. Obtain these from the "
             "sender over a channel the bundle did not travel on",
    )
    imp.add_argument(
        "--trust-file", metavar="PATH", dest="trust_file",
        help="the same, read from a file. Preferred: a key on a command line "
             "lands in shell history",
    )
    imp.add_argument(
        "--deployment-id", required=True, metavar="ID", dest="deployment_id",
        help="this deployment's identifier, recorded against the import",
    )
    imp.add_argument("--account", metavar="ID", help="default: ECHO_MEMORY_USER_ID")
    imp.add_argument("--memory", metavar="NAME", help="default: the scope name")
    imp.add_argument(
        "--confirm", action="store_true",
        help="actually import. Without it, nothing is written",
    )

    promote = commands.add_parser(
        "promote", help="make an imported bundle's facts readable"
    )
    promote.add_argument("bundle_id", metavar="BUNDLE_ID")
    promote.add_argument(
        "--verifier", required=True, metavar="WHO",
        help="who is accepting these claims. Required: a trust decision with "
             "nobody recorded against it is a decision nobody made",
    )
    promote.add_argument(
        "--confirm", action="store_true",
        help="actually promote. Without it, nothing becomes readable",
    )

    revoke = commands.add_parser(
        "revoke", help="withdraw a bundle. Its scope stops being readable"
    )
    revoke.add_argument("bundle_id", metavar="BUNDLE_ID")
    revoke.add_argument(
        "--reason", required=True, metavar="TEXT", help="recorded on the bundle"
    )

    reject = commands.add_parser(
        "reject", help="refuse a quarantined bundle a verifier has looked at"
    )
    reject.add_argument("bundle_id", metavar="BUNDLE_ID")
    reject.add_argument("--verifier", required=True, metavar="WHO")
    reject.add_argument("--reason", required=True, metavar="TEXT")

    listing = commands.add_parser("list", help="every bundle sent or accepted here")
    listing.add_argument(
        "--state", choices=(
            importer.STATE_CREATED, importer.STATE_QUARANTINED,
            importer.STATE_PROMOTED, importer.STATE_REVOKED,
            importer.STATE_REJECTED,
        ),
        help="only bundles in this state",
    )

    anchor = commands.add_parser(
        "anchor", help="the trust anchor a partner needs to verify your bundles"
    )
    anchor.add_argument("--key-id", required=True, metavar="ID", dest="key_id")


def dump_manifest(manifest: bundle_format.Manifest) -> str:
    """A manifest as readable JSON, for a person rather than for a digest.

    Never written to a file. The bytes on disk are canonical because that is
    what was signed, and a pretty-printed copy would fail verification in a
    way the recipient could not distinguish from tampering.
    """
    return json.dumps(manifest.to_dict(), indent=2, sort_keys=True)
