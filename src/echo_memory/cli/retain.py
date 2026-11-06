"""`echo-memory retain`: how long this scope keeps a fact, and enforcing it.

The operator surface for `echo_memory.retention`. Same shape as `forget` and
for the same reasons: not an MCP tool, because enforcement deletes customer
data permanently and a tool one prompt injection away from any agent holding a
key is not a trade worth making; and a dry run by default, because there is no
undo and the plan is cheap to compute.

With no flags it answers the question an auditor actually asks - how long do
you keep this, and who decided - rather than doing anything.
"""

from echo_memory import retention


def _show(conn, group_id: str) -> int:
    live = retention.policy(conn, group_id)
    if live is None:
        print(
            f"{group_id} has no retention policy.\n\n"
            f"That is not the same as keeping facts forever: it means nobody "
            f"has decided. Set one with\n"
            f"  echo-memory retain --days N --reason TEXT --by WHO"
        )
        return 0

    print(f"{group_id} keeps a fact for {live.summary}\n")

    overdue = retention.due(conn, group_id, live.max_age_days)
    if overdue:
        print(
            f"{len(overdue)} fact(s) are past it and have not been removed. "
            f"`retain --enforce` shows what would go."
        )
    else:
        print("Nothing is past it.")

    past = [r for r in retention.history(conn, group_id) if r["retired"]]
    if past:
        print(f"\n{len(past)} earlier polic(y/ies):")
        for r in past:
            print(
                f"  {r['max_age_days']:>5} days  {r['from']} to {r['retired']}"
                f"  {r['reason']} ({r['decided_by']})"
            )
    return 0


def _set(args, conn, group_id: str) -> int:
    if not args.reason or not args.by:
        print(
            "error: --days needs --reason and --by.\n"
            "\"We delete after 90 days\" is not a compliance answer; the "
            "purpose and the person who decided are the parts nobody can "
            "reconstruct later."
        )
        return 1
    try:
        live = retention.set_policy(conn, group_id, args.days, args.reason, args.by)
    except retention.RetentionError as e:
        print(f"error: {e}")
        return 1
    print(f"{group_id} now keeps a fact for {live.summary}\n")
    overdue = retention.due(conn, group_id, live.max_age_days)
    print(
        f"Nothing has been deleted. {len(overdue)} fact(s) are already past "
        f"this; `retain --enforce` shows what would go."
        if overdue
        else "Nothing is past it, so enforcing it now would delete nothing."
    )
    return 0


def _enforce(args, conn, group_id: str) -> int:
    try:
        out = retention.enforce(
            conn, group_id,
            dry_run=not args.confirm,
            requested_by=args.by or "cli",
            ticket=args.ref,
        )
    except retention.RetentionError as e:
        print(f"error: {e}")
        return 1

    if out.is_empty():
        print(
            f"Nothing in {group_id} is older than {out.policy.max_age_days} "
            f"days, so there is nothing to remove."
        )
        return 0

    if out.dry_run:
        print(
            f"Would erase {out.n_due} fact(s) from {group_id}, older than the "
            f"{out.policy.max_age_days} days this scope keeps them, plus any "
            f"entity left with no facts and the embeddings, audit text and "
            f"corpus statistics derived from them.\n\n"
            f"Nothing has been erased. There is no undo, so re-run with "
            f"--confirm to do it."
        )
        return 0

    print(
        f"Erased {out.erased} fact(s) and {out.nodes_erased} "
        f"entit(y/ies) from {group_id} under policy {out.policy.id}.\n"
        f"  recorded in erasure_event, the same place a subject deletion "
        f"request is recorded\n"
        f"\nBackups taken before now still contain this data, and restoring "
        f"one brings it back."
    )
    return 0


def run(args, config, conn) -> int:
    group_id = config.group_id(args.scope)
    if args.enforce:
        return _enforce(args, conn, group_id)
    if args.days is not None:
        return _set(args, conn, group_id)
    return _show(conn, group_id)
