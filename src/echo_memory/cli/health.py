"""echo-memory health: whether this graph is in good shape, and what to do about it.

Every other command answers a question you already had. This one exists to be
run when you have no question - to make the graph's condition legible without
querying it, and to say plainly when something is wrong that you would otherwise
discover weeks later.

The failure it is built against is real and recent. Criterion 6 read 0/3 for
nine days while the store looked healthy by every number the CLI reported: 160
facts, 24 projects, no duplicates, no bad merges. What those numbers hid was
that 142 of the facts were bulk imports, the last organic write was six days
old, and only one of two wired agents had ever written anything. Each of those
was visible in the data and none of them was surfaced.

The score is a heuristic, and deliberately a blunt one. It is not a measurement
of memory quality - nothing here knows whether a recalled fact was useful. It
weights the things that are checkable and that silently rot: whether anything is
still being written, whether more than one agent writes, whether entities are
connected, and whether the review queue is being kept up with.

Nothing here is gated. The paid tiers sell hosting and the things that only
exist when several people share a graph; diagnostics about your own data are not
a thing to withhold from the person whose data it is."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from echo_memory.cli import adopt
from echo_memory.cli.graph import fetch_graph
from echo_memory.graph import communities
from echo_memory.infra.db import GRAPH_NAME as GRAPH
from echo_memory.infra.project import UNKNOWN as UNKNOWN_AGENT
from echo_memory.trial import check as trial_check
from echo_memory.trial import reads as trial_reads

# Days without a written fact before the store is treated as going cold. Chosen
# against observed behaviour rather than theory: this store wrote on three
# consecutive days and then stopped, and a week of silence is long enough that
# the habit is gone rather than the week being quiet.
STALE_AFTER_DAYS = 7

# Pairs waiting on a human before the queue counts as unattended. Under a dozen
# is an afternoon; sixty is a queue nobody is going to open.
REVIEW_BACKLOG = 20


def _last_write(conn, group_ids: list[str]) -> tuple[datetime | None, int, int]:
    """When a fact was last created, and how many were organic vs imported.

    Imports are swept from memory files that already existed. Counting them as
    activity is what let a store with six days of silence look busy."""
    rows = conn.execute(
        """
        SELECT
            max(timestamp) FILTER (WHERE session_id NOT LIKE '%%bootstrap%%'
                                     AND session_id NOT LIKE '%%backfill%%'),
            count(*) FILTER (WHERE session_id NOT LIKE '%%bootstrap%%'
                               AND session_id NOT LIKE '%%backfill%%'),
            count(*) FILTER (WHERE session_id LIKE '%%bootstrap%%'
                                OR session_id LIKE '%%backfill%%')
        FROM public.audit_entry
        WHERE mutation_type = 'created' AND group_id = ANY(%s)
        """,
        (group_ids,),
    ).fetchone()
    return (rows[0], rows[1] or 0, rows[2] or 0) if rows else (None, 0, 0)


def _writers(conn, group_ids: list[str]) -> dict[str, int]:
    """Facts written per agent id, read from the graph rather than from
    `fetch_graph`, whose rows do not carry the author."""
    rows = conn.execute(
        f"""SELECT * FROM cypher('{GRAPH}', $$
            MATCH ()-[e:FACT]->()
            WHERE e.group_id IN $gids AND e.t_invalid IS NULL
            RETURN e.agent_id
        $$, %s) AS (agent_id agtype)""",
        (json.dumps({"gids": list(group_ids)}),),
    ).fetchall()
    counts: dict[str, int] = {}
    for (agent_id,) in rows:
        # An absent agent_id arrives as Python None, and str(None) is "None" -
        # which was reported to the user as though a tool were literally named
        # that. Apache AGE drops a null-valued property at CREATE, so this is
        # the shape a fact written without an author actually has. Fold it into
        # the same placeholder migration 0003 used, so one name means one thing.
        name = UNKNOWN_AGENT if agent_id is None else str(agent_id).strip('"')
        counts[name] = counts.get(name, 0) + 1
    return counts


def stale_writer(conn, group_ids: list[str]) -> str | None:
    """The version that last wrote here, if it is not the version asking.

    An MCP stdio server holds the code it imported at spawn: upgrading the
    package, editable install or not, does not change what that process runs.
    Nothing anywhere says so, and this store has paid for it twice - 30 facts
    with no author over sixteen days, and 7 with no project. Both times the code
    on disk was correct and the tests passed.

    Comparing the newest audit entry's writer against this process's own stamp
    is the only way to see it from outside. The stamp cannot be the installed
    version, which never moves in an editable checkout and so once sat on both
    sides of this comparison: it is the install version plus a source digest.

    A null is uninformative only before the column existed, which is what
    schema_moment records. After that moment a write with no version is not a
    gap in the record, it is the record: the writer predates the column. The
    first version of this check skipped every null and so could not see the
    very servers it was built for.
    """
    from echo_memory import __writer_version__

    row = conn.execute(
        """SELECT ae.writer_version
             FROM public.audit_entry ae
             LEFT JOIN public.schema_moment m ON m.name = 'writer_version_expected'
            WHERE ae.group_id = ANY(%s)
              AND (ae.writer_version IS NOT NULL
                   OR (m.occurred_at IS NOT NULL AND ae."timestamp" > m.occurred_at))
            ORDER BY ae."timestamp" DESC LIMIT 1""",
        (group_ids,),
    ).fetchone()
    if row is None or row[0] == __writer_version__:
        return None
    # A write with no version, made after this database gained the column, is
    # proof the writer predates it. Skipping those was the original mistake:
    # the server that caused 30 authorless facts stamps nothing at all, so it
    # was invisible to the detector built to find it.
    return str(row[0]) if row[0] is not None else "before versions were recorded"


def _connectivity(graph: dict) -> dict:
    """How connected the graph actually is, reported by median rather than mean.

    Mean degree was quoted as 2.11 for weeks and is misleading: the degree
    distribution is heavy-tailed, so the mean is held up by a handful of hubs
    while the typical node has one edge. Measured on the author's store, 208
    nodes: median degree 1, 64% with exactly one edge, 83% with two or fewer,
    and the top 5% of nodes holding 32% of all degree.

    That distinction decides real work. A node of degree 1 is a leaf and can
    never be traversed THROUGH, so multi-hop retrieval over a graph that is
    mostly leaves dead-ends immediately - which is why Personalized PageRank
    stayed deferred, and why reification was measured before being built.
    """
    degree: dict[str, int] = {n["id"]: 0 for n in graph["nodes"]}
    for fact in graph["facts"]:
        for endpoint in (fact["source_id"], fact["target_id"]):
            if endpoint in degree:
                degree[endpoint] += 1
    values = sorted(degree.values())
    if not values:
        return {"median_degree": 0, "leaf_share": 0.0}
    mid = len(values) // 2
    median = (
        values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2
    )
    leaves = sum(1 for v in values if v <= 1)
    return {"median_degree": median, "leaf_share": leaves / len(values)}


def _orphans(graph: dict) -> list[dict]:
    """Entities no fact connects to anything. A node with no edges cannot be
    reached by traversal and contributes nothing a flat list would not."""
    connected = {f["source_id"] for f in graph["facts"]}
    connected |= {f["target_id"] for f in graph["facts"]}
    return [n for n in graph["nodes"] if n["id"] not in connected]



def gate_conversion(conn) -> dict:
    """How often the Stop gate produced a fact, not how often it fired.

    The project's own headline finding compared 0 of 16 memory files converted
    against hooks firing 24 of 24. Those are different outcomes - one is
    end-to-end capture, the other is a trigger - and a reviewer of the paper
    drawn from this record rejected the comparison on exactly that ground. He
    was right, and this is the number that replaces it: both sides measured as
    knowledge actually stored.

    The Stop gate is the strongest mechanism here. It returns a block decision,
    which refuses to let a session end and hands control back with the unwritten
    list as the next instruction. If structural enforcement produces writes
    anywhere, it produces them here.

    Measured 2026-09-13 on this store: 7 firings, 1 followed by a fact from the
    same session. Structural TRIGGERING is reliable - the gate fired every time
    it should. Structural CAPTURE is not, because the last step still hands a
    model a choice about what is worth remembering, and there is no hook for
    that.

    **Three outcomes, not two, and the third is the honest majority.**

      wrote     a fact was written by that session after the firing
      closed    a queued document was closed - the gate's own instruction for
                content already recorded
      unknown   neither. The logs cannot say whether anything was owed.

    The last one is not a failure and was reported as one. A session that made
    118 edits and wrote nothing looks damning, but edit count is evidence of
    ACTIVITY, not of an unmet need to remember: nothing here establishes that
    any of those edits produced something worth keeping. Calling every non-write
    a miss assumes the denominator this instrument cannot measure. A reviewer
    made that point about the paper and it applies to the metric itself.

    So what is reported is new-write incidence and correct disposition, with
    the remainder named as unresolved rather than counted against anything.

    **And the denominator comes from a different instrument.** "Eight firings,
    eight successes" is not a triggering rate: a session the gate never fired
    for leaves no row in the firing log, so misses cannot appear in it.
    session_activity is written by a different hook and records sessions that
    did work, which makes it an independent census. The two disagree, and that
    disagreement is the finding: neither log is complete, so no rate computed
    from either alone should be read as one.

    A firing counts as converted if a fact was written OR a queued document was
    closed after it. Writing alone was the first version and it was wrong in a
    way the gate demonstrated within the hour: its own instruction says that
    when a file's content is already recorded the right action is to mark it
    done, not write it twice. A session that did exactly that scored as a
    failure. A metric that punishes the behaviour its own instruction asks for
    measures compliance with itself, not capture.

    Undercounts by construction even so, in two ways. A fact written later,
    under a different session id, is not attributed to the gate that asked for
    it - 16 files were once drained by hand ten days afterwards. And closures
    made before migration 0020 have no recorded author, so every firing older
    than it can only be credited through a write. Read it as a floor.
    """
    fired = conn.execute(
        "SELECT session_id, at, n_files, project FROM public.stop_gate_fired"
    ).fetchall()
    converted = 0
    wrote_n = 0
    closed_n = 0
    closed_anon_n = 0
    had_queue = 0
    for session_id, at, n_files, project in fired:
        wrote = conn.execute(
            """SELECT 1 FROM public.audit_entry
               WHERE session_id = %s AND mutation_type = 'created' AND timestamp > %s
               LIMIT 1""",
            (session_id, at),
        ).fetchone()
        closed = conn.execute(
            """SELECT 1 FROM public.pending_ingest
               WHERE ingested_by_session = %s AND ingested_at > %s LIMIT 1""",
            (session_id, at),
        ).fetchone()
        # A closure in this firing's project, after it, with no session
        # recorded. Known outcome, unknown author - which is not the same thing
        # as no closure, and reporting it as one is the contradiction a reviewer
        # found: prose saying nothing was closed beside a row saying "already
        # recorded; closed".
        #
        # An UPPER BOUND, and deliberately labelled as one in the output. The
        # gate records how many files were queued and never which, so a closure
        # days later by an unrelated session in the same project matches this
        # too. The same looseness turned an earlier version of this metric into
        # 8 of 8. It is reported beside the attributable count rather than
        # added to it.
        closed_anon = conn.execute(
            """SELECT 1 FROM public.pending_ingest
               WHERE ingested_by_session IS NULL AND ingested_at > %s
                 AND project = %s LIMIT 1""",
            (at, project),
        ).fetchone()
        if wrote:
            wrote_n += 1
        if closed:
            closed_n += 1
        elif closed_anon:
            closed_anon_n += 1
        if wrote or closed:
            converted += 1
        if n_files:
            had_queue += 1

    # The independent census. Sessions the activity counter saw, how many of
    # them the gate also saw, and firings the counter missed.
    active = conn.execute(
        "SELECT session_id FROM public.session_activity"
    ).fetchall()
    active_ids = {r[0] for r in active}
    fired_ids = {sid for sid, _at, _n, _p in fired}
    return {
        "active_sessions": len(active_ids),
        "active_and_fired": len(active_ids & fired_ids),
        "fired_not_active": len(fired_ids - active_ids),
        "fired": len(fired),
        "converted": converted,
        "wrote": wrote_n,
        "closed": closed_n,
        # Outcome known, attribution missing. Separated because collapsing them
        # makes a documented closure look like a documented absence.
        "closed_unattributed": closed_anon_n,
        # Firings where a document was actually queued, so something was known
        # to be owed. The rest fired on "did work, recorded nothing", where
        # whether anything was worth keeping is exactly what is not established.
        "with_queue": had_queue,
    }


def collect(conn, config, today: datetime | None = None) -> dict:
    """Everything health reports, as data. Rendering is separate so `--json`
    and the human view can never drift."""
    today = today or datetime.now(UTC)
    group_ids = [config.group_id(s) for s in ("solo", "shared")]

    graphs = {s: fetch_graph(conn, config.group_id(s)) for s in ("solo", "shared")}
    nodes = [n for g in graphs.values() for n in g["nodes"]]
    facts = [f for g in graphs.values() for f in g["facts"]]

    last, organic, imported = _last_write(conn, group_ids)
    days_quiet = (today - last).days if last else None

    edges = [(f["source_id"], f["target_id"]) for f in facts]
    detected = communities.detect({n["id"]: n["name"] for n in nodes}, edges)

    writers = _writers(conn, group_ids)
    wired = {c["agent_id"] for c in adopt.adopted_clients()}
    silent = sorted(wired - {a for a, n in writers.items() if n})

    report = trial_check.build_report(conn, config, today=today.date())
    read_stats = trial_reads.summary(conn, group_ids)
    orphans = [n for g in graphs.values() for n in _orphans(g)]

    return {
        "nodes": len(nodes),
        "facts": len(facts),
        "organic_writes": organic,
        "imported_writes": imported,
        "last_write": last.date().isoformat() if last else None,
        "days_since_write": days_quiet,
        "writers": writers,
        "silent_agents": silent,
        "orphans": [{"name": n["name"], "type": n["type"]} for n in orphans],
        "components": len(set(detected["components"].values())) if nodes else 0,
        "clusters": len(detected["communities"]),
        **_connectivity({"nodes": nodes, "facts": facts}),
        "unreviewed_pairs": report["n_open_pairs"],
        "unattributed_facts": report.get("unattributed_facts", 0),
        "duplicates": report["counts"]["duplicates"],
        "bad_merges": report["counts"]["bad_merges"],
        "reads": read_stats,
        "unreturned": trial_reads.unreturned(conn, group_ids),
        "stale_writer": stale_writer(conn, group_ids),
        "gate": gate_conversion(conn),
    }


def score(h: dict) -> int:
    """0-100, and honestly a heuristic.

    Deductions are sized by how badly the thing distorts what the store appears
    to be, not by how hard it is to fix. Silence and single-authorship are the
    two that made a store look healthy while criterion 6 could not move, so they
    cost the most."""
    if not h["facts"]:
        return 0
    points = 100
    quiet = h["days_since_write"]
    if quiet is None:
        points -= 40
    elif quiet > STALE_AFTER_DAYS:
        points -= min(30, 10 + (quiet - STALE_AFTER_DAYS) * 2)
    real_writers = [a for a, n in h["writers"].items() if n and a != "unknown"]
    if len(real_writers) < 2:
        points -= 20
    if h["silent_agents"]:
        points -= 10
    if h["facts"]:
        orphan_share = len(h["orphans"]) / max(h["nodes"], 1)
        points -= int(min(15, orphan_share * 50))
    if h["unreviewed_pairs"] > REVIEW_BACKLOG:
        points -= 10
    points -= min(15, h["unattributed_facts"])
    points -= h["duplicates"] * 5
    points -= h["bad_merges"] * 10
    return max(0, min(100, points))


def findings(h: dict) -> tuple[list[str], list[str], list[str]]:
    """(strong, attention, recommendations). Attention items name the number and
    the thing to do; a diagnostic that does not tell you the next move is a
    complaint."""
    strong, attention, rec = [], [], []

    if h["duplicates"] == 0:
        strong.append("no duplicate nodes confirmed")
    if h["bad_merges"] == 0:
        strong.append("no bad merges confirmed")
    if h["unattributed_facts"] == 0 and h["facts"]:
        strong.append("every fact has a recorded author")
    # Only a strength when the clusters are actually joined-up. Many small
    # clusters over a graph of leaves is fragmentation, and calling it
    # "structure forming" is the kind of flattering number this command exists
    # to stop reporting - it read as a strength for weeks over a store whose
    # median node had one edge.
    if h["clusters"] > 1 and h.get("leaf_share", 1.0) < 0.5:
        strong.append(f"{h['clusters']} distinct clusters, so structure is forming")

    # First, because it explains other findings rather than adding to them: a
    # stale writer is why a store has facts with no author or no project, and
    # every other line here is describing its output.
    if h.get("stale_writer"):
        from echo_memory import __writer_version__

        attention.append(
            f"the last write came from version {h['stale_writer']}, but "
            f"{__writer_version__} is running"
        )
        rec.append(
            "An MCP stdio server holds the code it imported when the client "
            "started it, so upgrading the package - editable install or not - "
            "changes nothing until the client is restarted. That gap has cost "
            "this project 30 facts with no author and 7 with no project. "
            "Restart the client that owns the server."
        )

    quiet = h["days_since_write"]
    if not h["facts"]:
        attention.append("no facts recorded yet")
        rec.append("Write something: memory only pays back what it was given.")
    elif quiet is None or quiet > STALE_AFTER_DAYS:
        attention.append(
            f"{quiet} days since the last write (last: {h['last_write']})"
            if quiet is not None else "nothing has ever been written"
        )
        rec.append(
            "Writes are agent-discretionary, so silence usually means nothing is "
            "prompting them rather than that nothing happened."
        )

    if h["imported_writes"] > h["organic_writes"]:
        attention.append(
            f"{h['imported_writes']} of {h['imported_writes'] + h['organic_writes']} "
            "facts came from bulk import, not from working"
        )

    real_writers = [a for a, n in h["writers"].items() if n and a != "unknown"]
    if len(real_writers) < 2:
        attention.append(
            f"only {real_writers[0]} has ever written, so recall cannot be cross-tool"
            if real_writers else
            "nothing has written a fact yet, so recall cannot be cross-tool"
        )
        rec.append("Run `echo-memory adopt` to wire another client, then use it.")

    if h["silent_agents"]:
        attention.append(
            "wired but has never written: " + ", ".join(h["silent_agents"])
        )
        rec.append(
            "A wired client that writes nothing usually lacks instructions rather "
            "than access - `echo-memory skill` prints them."
        )

    if h["orphans"]:
        attention.append(
            f"{len(h['orphans'])} entities connect to nothing "
            f"(e.g. {', '.join(o['name'] for o in h['orphans'][:3])})"
        )
        rec.append("Orphans are unreachable by traversal; relate them or let them go.")

    r = h.get("reads") or {}
    if r.get("reads") and not r.get("saves"):
        attention.append(
            f"{r['reads']} reads cost ~{r['injected_tokens']:,} injected tokens and "
            "produced no recorded save"
        )
        rec.append(
            "A read that helped is only visible if record_recall_save is called. "
            "Zero saves against real read volume means either the recall is not "
            "useful or nobody is reporting when it is - and those need different fixes."
        )

    u = h.get("unreturned") or {}
    if u.get("active") and u.get("unreturned"):
        share = 100 * u["unreturned"] // u["active"]
        attention.append(
            f"{u['unreturned']} of {u['active']} active facts were returned to "
            f"nobody in {u['days']} days ({share}%)"
        )
        # Deliberately no pruning advice. Every "forgetting layer" in the
        # agent-memory literature answers this with a policy - a decay curve, a
        # TTL, an eviction rule - chosen before anyone counted. A fact nothing
        # returned may be the one that matters next week, or retrieval may be
        # failing to reach it, and those need opposite responses.
        rec.append(
            "A fact nothing returns is either unneeded or unreachable, and the two "
            "want opposite fixes - check whether a question you would expect to hit "
            "one actually does before treating this as dead weight."
        )

    if h["unreviewed_pairs"] > REVIEW_BACKLOG:
        attention.append(f"{h['unreviewed_pairs']} similar pairs awaiting review")
        rec.append("Run `echo-memory trial check` to judge them before the queue is ignored.")

    if h.get("leaf_share", 0) >= 0.5:
        attention.append(
            f"{h['leaf_share']:.0%} of entities have one edge or none "
            f"(median degree {h['median_degree']:g})"
        )
        rec.append(
            "Most entities are mentioned once, so traversal has nowhere to go. "
            "Reuse existing entities when writing rather than minting new ones; "
            "query_memory first to see what is already named."
        )

    if h["unattributed_facts"]:
        attention.append(f"{h['unattributed_facts']} facts have no recorded author")
        # Deliberately not "run alembic upgrade head". Migration 0011 is what
        # turns these into 'unknown' in the first place, so once it has run the
        # advice is unfollowable: the schema is already at head and nothing
        # further will change the number. Telling someone to re-run a migration
        # that just ran is the same unactionable dead end record_recall_save
        # used to hand an agent for a fact_id that was never wrong.
        #
        # The information is gone. Who wrote these is only knowable from the
        # session that wrote them, and it has exited. What IS actionable is
        # stopping the supply: a Claude Code MCP server imports this package
        # once at spawn and keeps that code for its whole life, so a client
        # left open across an upgrade goes on writing the old shape.
        rec.append(
            "These predate attribution and cannot be recovered - they will never "
            "count toward a cross-tool save. Restart any long-running MCP client "
            "so it stops writing more."
        )

    return strong, attention, rec


def render(h: dict) -> str:
    strong, attention, rec = findings(h)
    n = score(h)
    bar = "#" * (n // 5) + "." * (20 - n // 5)
    lines = [
        "Echo Memory - knowledge health",
        "",
        f"  {n}/100  [{bar}]",
        (
            f"  {h['facts']} facts, {h['nodes']} entities, "
            f"{h['clusters']} clusters, {h['components']} unrelated groups"
        ),
        # Always shown, warning or not. A threshold decides when to complain;
        # it should not decide whether the reader gets to see the number.
        (
            f"  {h['organic_writes']} written while working, last on {h['last_write']}"
            f" ({h['days_since_write']}d ago)"
            if h["last_write"] else "  nothing written while working yet"
        ),
        "",
    ]
    r = h.get("reads") or {}
    if r.get("reads"):
        hit = round(100 * r["reads_with_facts"] / r["reads"])
        lines.append(
            f"  read {r['reads']} times in {r['days']}d, {hit}% returned something, "
            f"~{r['injected_tokens']:,} tokens injected, {r['saves']} save(s) recorded"
        )
        # Who did the reading. One tool reading a hundred times and three tools
        # reading thirty each are the same number and completely different
        # products, and the second is the one being claimed.
        by_agent = r.get("by_agent") or {}
        if by_agent:
            lines.append(
                "    by tool: " + ", ".join(f"{a}={n}" for a, n in by_agent.items())
            )
        lines.append("")
    elif r:
        lines.append(f"  no reads recorded in {r['days']}d")
        lines.append("")

    # Capture, measured on the outcome rather than on the trigger. A hook that
    # fires is not a fact that exists, and reporting the firing count alone is
    # how this project came to believe structural capture was solved.
    g = h.get("gate") or {}
    if g.get("fired"):
        anon = g.get("closed_unattributed", 0)
        unresolved = g["fired"] - g["converted"]
        lines.append(
            f"  stop gate fired {g['fired']}x: {g['wrote']} wrote a fact, "
            f"{g['closed']} closed a document, {unresolved} neither"
        )
        if anon:
            lines.append(
                f"    up to {anon} of those {unresolved} saw a document closed in the "
                "same project with no session recorded - outcome known, author not"
            )
        lines.append(
            f"    {g['with_queue']} of {g['fired']} fired with something queued; for the "
            "rest, whether anything was worth keeping is not established"
        )
        # Two instruments, two counts. Printed together because either alone
        # reads as a rate and neither is one.
        lines.append(
            f"    the activity counter saw {g['active_sessions']} working session(s), "
            f"{g['active_and_fired']} of which the gate also saw"
            + (f"; {g['fired_not_active']} firing(s) the counter missed"
               if g["fired_not_active"] else "")
        )
        lines.append("")

    if strong:
        lines.append("  strong")
        lines += [f"    + {s}" for s in strong]
        lines.append("")
    if attention:
        lines.append("  attention")
        lines += [f"    ! {a}" for a in attention]
        lines.append("")
    if rec:
        lines.append("  what to do")
        lines += [f"    - {r}" for r in rec]
        lines.append("")
    if not attention:
        lines.append("  Nothing needs attention.")
        lines.append("")
    return "\n".join(lines)
