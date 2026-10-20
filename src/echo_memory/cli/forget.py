"""`echo-memory forget`: the operator surface for erasure.

Deliberately not an MCP tool, and there is a test that reads the registered
tool list so adding it later fails loudly. Irreversible deletion of a whole
scope, one prompt injection away from any agent holding a key, is not a trade
worth making. The standing rule that anything an agent does as part of a loop
must be reachable over MCP does not apply: erasure answers a legal request
from a person, and is never a step in an agent's loop.

Dry run by default. There is no undo, the plan is cheap to compute, and an
operator who sees "this would remove 412 facts" before it happens is the
difference between a correct erasure and an incident. `--confirm` is the only
thing that deletes.
"""

from echo_memory import erasure

# How many facts to print in a dry run before summarising. Enough to see what
# the request actually matched, short enough to read.
PREVIEW = 10


def _preview(conn, group_id: str, edge_ids: list[str]) -> list[str]:
    if not edge_ids:
        return []
    rows = conn.execute(
        f"""SELECT (properties ->> '"fact"'::agtype)
              FROM {erasure.GRAPH}."FACT"
             WHERE id = ANY(%s::text[]::graphid[])
             ORDER BY id
             LIMIT %s""",
        (edge_ids, PREVIEW),
    ).fetchall()
    return [r[0] for r in rows]


def run(args, config, conn) -> int:
    group_id = config.group_id(args.scope)

    if args.log:
        rows = erasure.history(conn, group_id)
        if not rows:
            print(f"No erasure has been recorded for {group_id}.")
            return 0
        print(f"{len(rows)} erasure(s) recorded for {group_id}:\n")
        for r in rows:
            when = (r["completed_at"] or r["requested_at"]).date().isoformat()
            mark = "" if r["status"] == "completed" else f"  [{r['status'].upper()}]"
            print(
                f"  {r['id']:>4}  {when}  {r['verb']:<6} "
                f"{r['n_edges']} fact(s), {r['n_nodes']} entit(y/ies), "
                f"{r['n_audit_rows']} audit row(s) redacted{mark}"
            )
            detail = [x for x in (r["reason"], r["request_ref"], r["requested_by"]) if x]
            if detail:
                print(f"        {' | '.join(detail)}")
        print(
            "\nA backup taken before any of these still contains what it "
            "erased. Replay them after any restore."
        )
        return 0

    if args.search:
        found = erasure.candidates(conn, group_id, args.search)
        if not found:
            print(f"No fact in {group_id} mentions {args.search!r}.")
            return 0
        print(
            f"{len(found)} fact(s) in {group_id} mention {args.search!r}. "
            f"A text match is not a subject, so nothing is erased from this "
            f"list automatically: read them and pass the ones that match to "
            f"`forget --fact <id>`.\n"
        )
        for edge_id, text in found:
            print(f"  {edge_id}  {text}")
        return 0

    try:
        target = erasure.plan(
            conn,
            group_id,
            fact=args.fact,
            entity=args.entity,
            scope=args.whole_scope,
        )
    except erasure.ErasureError as e:
        print(f"error: {e}")
        return 1

    if target.is_empty():
        print(f"Nothing to erase in {group_id}.")
        return 0

    what = {
        "fact": f"the fact {args.fact} and every other version of its triple",
        "entity": f"the entity {args.entity!r}, its aliases and every fact touching it",
        "scope": f"everything in {group_id}",
    }[target.verb]

    if not args.confirm:
        print(
            f"Would erase {what}:\n"
            f"  {target.n_edges} fact(s), every version\n"
            f"  {target.n_nodes} entit(y/ies)\n"
            f"and the embeddings, audit text, causal quotes and corpus "
            f"statistics derived from them.\n"
        )
        for text in _preview(conn, group_id, target.edge_ids):
            print(f"  - {text}")
        if target.n_edges > PREVIEW:
            print(f"  ... and {target.n_edges - PREVIEW} more")
        print("\nNothing has been erased. There is no undo, so re-run with --confirm to do it.")
        return 0

    try:
        result = erasure.erase(
            conn,
            group_id,
            target,
            requested_by=args.by or "cli",
            reason=args.reason,
            request_ref=args.ref,
        )
    except erasure.ErasureError as e:
        print(f"error: {e}")
        return 1

    erasure.vacuum(conn, group_id)

    print(
        f"Erased {result['edges']} fact(s) and {result['nodes']} entit(y/ies) "
        f"from {group_id}.\n"
        f"  {result['audit_rows_redacted']} audit row(s) redacted, kept as "
        f"evidence a mutation happened\n"
        f"  corpus statistics dropped; the next write rebuilds them from what "
        f"is left\n"
        f"  recorded as erasure {result['erasure_id']}"
    )
    if erasure.subject_digest(target.subject) is None and target.subject:
        print(
            f"\nNo {erasure.KEY_ENV} is set, so erasure {result['erasure_id']} "
            f"records counts but cannot later prove which subject it was for."
        )
    print(
        "\nBackups taken before now still contain this data, and restoring one "
        "brings it back. Replay the erasure after any restore."
    )
    return 0
