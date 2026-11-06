"""`echo-memory verify`: has anything in this store been changed behind its back.

The operator surface for `echo_memory.attestation`. It answers three separate
questions, and prints them separately because an answer that merges them
cannot be acted on: has any fact changed since it was written, is the chain
continuous, and does every fact have a link at all.

It also always prints what the answer is worth. A chain that is neither keyed
nor witnessed outside this database detects corruption and nothing more, and a
report that said "verified" without that sentence would be the line somebody
quotes.
"""

from echo_memory import attestation


def _strength_note(report) -> str:
    if report.keyed and not report.mixed_keying:
        return f"Keyed: a digest cannot be recomputed without {attestation.KEY_ENV}."
    return (
        f"Unkeyed: anybody who can write to this database can also recompute "
        f"these digests, so this is worth exactly as much as a record of the "
        f"head kept somewhere outside it. `verify --head` prints that record, "
        f"and {attestation.KEY_ENV} removes the need for one."
    )


def _head(conn, group_id: str) -> int:
    seq, digest = attestation.head(conn, group_id)
    if seq == 0:
        print(f"{group_id} has no attested facts yet.")
        return 0
    print(f"{group_id}\n  seq {seq}\n  {digest}\n")
    print(
        "Record this outside the database - a ticket, a commit message, a log "
        "this database cannot reach. Any later change to any fact in this "
        "scope changes it, so a copy kept elsewhere is what makes a silent "
        "rewrite impossible."
    )
    return 0


def _backfill(conn, group_id: str) -> int:
    pending = attestation.unattested(conn, group_id)
    if not pending:
        print(f"Every fact in {group_id} is already attested.")
        return 0
    n = attestation.backfill(conn, group_id)
    print(
        f"Attested {n} fact(s) in {group_id} that had no digest.\n\n"
        f"This is a baseline from now, not a statement about the past: a fact "
        f"attested today says nothing about whether it changed yesterday. "
        f"Each row records its own attested_at so that stays visible."
    )
    return 0


def run(args, config, conn) -> int:
    group_id = config.group_id(args.scope)

    if args.head:
        return _head(conn, group_id)
    if args.backfill:
        return _backfill(conn, group_id)

    report = attestation.report(conn, group_id)
    pending = attestation.unattested(conn, group_id)

    if report.n_attested == 0 and not pending:
        print(f"{group_id} holds no facts, so there is nothing to verify.")
        return 0

    if report.n_attested == 0:
        print(
            f"{len(pending)} fact(s) in {group_id} and none of them attested.\n\n"
            f"Facts written before attestation shipped have no digest. "
            f"`verify --backfill` gives them one from now."
        )
        return 1

    print(f"{report.n_attested} attested fact(s) in {group_id}.")

    if report.altered:
        unexplained = [a for a in report.altered if a["explanation"] is None]
        print(
            f"\n{len(report.altered)} fact(s) no longer match what was "
            f"attested, {len(unexplained)} of them unexplained:"
        )
        for a in report.altered:
            why = a["explanation"] or "NO RECORDED MUTATION ACCOUNTS FOR THIS"
            print(f"  seq {a['seq']:>6}  {a['edge_id']}  {why}")
        if unexplained:
            print(
                "\nAn unexplained change is the finding this command exists "
                "for: the fact is not what was written and nothing in the "
                "audit log says it should have changed."
            )

    if report.broken_links:
        print(f"\n{len(report.broken_links)} broken link(s) in the chain:")
        for b in report.broken_links:
            print(f"  seq {b['seq']:>6}  expected {b['expected'][:24]}... found {b['found'][:24]}...")

    if report.vanished:
        print(
            f"\n{len(report.vanished)} attested fact(s) are gone and no "
            f"recorded erasure accounts for them "
            f"({report.erased} fact(s) erased in total)."
        )
    elif report.erased:
        print(f"\n{report.erased} fact(s) erased on request, which accounts for every gap.")

    if pending:
        print(
            f"\n{len(pending)} fact(s) have no digest at all. "
            f"`verify --backfill` gives them one from now."
        )

    if report.is_clean():
        print("\nNothing has changed behind this store's back.")
    print(f"\n{_strength_note(report)}")

    return 0 if report.is_clean() else 1
