"""echo-memory: the CLI companion to the MCP server (CEO plan items 3 and
5, "why" and "export"). Reads the same ECHO_MEMORY_* env vars as the
server and resolves scope the same way, never a raw group_id: see the
design doc's Configuration section for why group_id is never typed or
constructed directly."""

import argparse
import json
import os
import sys
import time
from datetime import date
from pathlib import Path

from echo_memory.audit.get_audit_log import get_fact_history
from echo_memory.cli import (
    adopt,
    calibrate,
    health,
    initdb,
    merge,
    quickstart,
    reattribute_cmd,
    stop_gate,
    unmerge,
)
from echo_memory.cli import analyse as analyse_cmd
from echo_memory.cli import connect as connect_cmd
from echo_memory.cli import dashboard as dashboard_cmd
from echo_memory.cli import hooks as hooks_cmd
from echo_memory.cli import judge as judge_cmd
from echo_memory.cli import queue as queue_cmd
from echo_memory.cli import recall as recall_cmd
from echo_memory.cli import reconcile as reconcile_cmd
from echo_memory.cli import session_start as session_start_cmd
from echo_memory.cli import trial as trial_cmd
from echo_memory.cli.benchmark import render as render_benchmark
from echo_memory.cli.benchmark import run as run_benchmark
from echo_memory.cli.dashboard import fetch_dashboard
from echo_memory.cli.dashboard_html import render_dashboard
from echo_memory.cli.export import export_group
from echo_memory.cli.graph import fetch_graph, render_graph
from echo_memory.cli.install import install, render_install
from echo_memory.cli.skill import package as package_skill
from echo_memory.cli.skill import render_skill
from echo_memory.cli.status import fetch_status, render_status
from echo_memory.cli.why import render_history
from echo_memory.eval.external import DATASETS as external_datasets
from echo_memory.infra.config import ConfigError, load_config
from echo_memory.infra.db import connect
from echo_memory.infra.project import detect_project
from echo_memory.ingestion import bootstrap as bootstrap_mod
from echo_memory.trial import check, observations


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="echo-memory")
    parser.add_argument(
        "--scope", choices=["solo", "shared"], default="solo",
        help="memory scope to operate on (default: solo)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    why_parser = sub.add_parser("why", help="show a fact's audit trail")
    why_parser.add_argument("fact_id", help="fact id, as returned by query_memory")

    export_parser = sub.add_parser("export", help="markdown export of a scope's memory")
    export_parser.add_argument("--out", required=True, type=Path, help="output directory")

    graph_parser = sub.add_parser("graph", help="view a scope's memory graph")
    graph_mode = graph_parser.add_mutually_exclusive_group()
    graph_mode.add_argument(
        "--watch", action="store_true", help="refresh live in the terminal instead of printing once"
    )
    graph_mode.add_argument(
        "--html", type=Path, metavar="PATH",
        help="write a self-contained interactive HTML snapshot to PATH instead of printing to the terminal",
    )
    graph_parser.add_argument(
        "--interval", type=float, default=2.0, help="refresh interval in seconds (with --watch)"
    )

    sub.add_parser("status", help="v1a trial status: which Success Criteria are met so far")

    _add_trial_parser(sub)
    _add_project_parsers(sub)

    return parser


def _add_project_parsers(sub) -> None:
    """Project attribution and the automatic-capture queue. See
    infra/project.py for why the project is resolved from cwd rather than
    passed in, and ingestion/capture.py for what the queue is and isn't."""
    dash = sub.add_parser("dashboard", help="one page over every scope and project")
    dash.add_argument(
        "--out", type=Path, metavar="PATH", help="write a self-contained HTML snapshot here"
    )
    dash.add_argument(
        "--serve", action="store_true",
        help="serve it on localhost instead, regenerating on every reload so it stays live",
    )
    dash.add_argument("--port", type=int, default=8787, help="port for --serve (default: 8787)")
    dash.add_argument(
        "--open", action="store_true", dest="open_browser",
        help="open the page in your browser once it is ready",
    )

    reattr = sub.add_parser(
        "reattribute", help="set the project on facts written before projects were recorded"
    )
    reattr.add_argument(
        "--list", action="store_true", dest="list_sessions",
        help="show every session that has written to this scope and its current project",
    )
    reattr.add_argument("--session", metavar="ID", help="session whose facts to reattribute")
    reattr.add_argument("--project", metavar="NAME", help="project to attribute them to")
    reattr.add_argument(
        "--agent", action="store_true",
        help=(
            "work on authorship instead of project: recover the agent_id of facts "
            "whose session evidences one. Never guesses - a session with no "
            "attributed fact, or two, is reported as unrecoverable"
        ),
    )

    conn_parser = sub.add_parser(
        "connect",
        help="use the hosted service instead of running a database yourself",
    )
    conn_parser.add_argument(
        "api_key", nargs="?",
        help="a key from https://api.echo-mem.com (shown once, when created)",
    )
    conn_parser.add_argument(
        "--endpoint", metavar="URL",
        help=f"a different deployment (default: {connect_cmd.DEFAULT_ENDPOINT})",
    )

    qs = sub.add_parser(
        "quickstart",
        help="start the database, apply the schema, and say what to do next",
    )
    qs.add_argument(
        "--port", type=int, metavar="N",
        help=f"host port for the database (default: {quickstart.PORT}, "
             "or the next free one above it)",
    )

    # The other evaluation. Its questions are written before anything is
    # retrieved for them, and relevance is judged per fact rather than per
    # configuration, so a configuration's identity cannot reach the label.
    judge = sub.add_parser(
        "judge",
        help="evaluate retrieval on questions written before the answers were seen",
    )
    judge_sub = judge.add_subparsers(dest="judge_command", required=True)

    j_new = judge_sub.add_parser("new", help="record a question; retrieves nothing")
    j_new.add_argument("text", help="the question, in the words somebody would ask it")
    j_new.add_argument("--subject", metavar="NAME", help="what it is about, for your own sorting")

    judge_sub.add_parser(
        "list", help="questions recorded, and which have been opened for judging"
    )
    judge_sub.add_parser(
        "open",
        help="retrieve under every configuration and open judging; do this after the "
             "questions are written, never before",
    )
    j_pool = judge_sub.add_parser(
        "pool", help="judge the shuffled union of what every configuration returned"
    )
    j_pool.add_argument("--question", type=int, metavar="ID", help="just this one")
    j_pool.add_argument("--as", dest="judged_by", metavar="NAME",
                        help="judge as somebody else; only their own labels are skipped")
    j_exp = judge_sub.add_parser(
        "export", help="write the whole judging pass to a file to mark in an editor"
    )
    j_exp.add_argument("--out", metavar="FILE", help="write here instead of stdout")
    j_exp.add_argument("--question", type=int, metavar="ID", help="just this one")
    j_exp.add_argument("--as", dest="judged_by", metavar="NAME",
                       help="export what this judge has not labelled yet; omit for a "
                            "fresh judge, who gets the whole pool")

    j_imp = judge_sub.add_parser("import", help="read a marked file back")
    j_imp.add_argument("file", help="the file, with y or n between the brackets")
    j_imp.add_argument("--as", dest="judged_by", metavar="NAME",
                       help="attribute these labels to this judge")

    j_score = judge_sub.add_parser(
        "score", help="per-configuration metrics over one judge's labels"
    )
    j_score.add_argument(
        "--per-question", action="store_true",
        help="also show each question's reciprocal rank, so a reader can see "
             "whether an advantage is consistent or carried by two cases",
    )
    j_score.add_argument(
        "--by", dest="judged_by", metavar="NAME",
        help="whose labels to score against; required once more than one judge "
             "has labelled, because their labels are separate measurements",
    )

    j_cmp = judge_sub.add_parser(
        "compare",
        help="delta MRR between two configurations with a paired bootstrap interval, "
             "so a lead too small for ten questions to resolve reads as one",
    )
    j_cmp.add_argument("a", help="the baseline configuration, e.g. shipping")
    j_cmp.add_argument("b", help="the one being compared against it")
    j_cmp.add_argument("--by", dest="judged_by", metavar="NAME",
                       help="whose labels to compare under")

    judge_sub.add_parser("judges", help="who has labelled this scope, and how much")
    j_agree = judge_sub.add_parser(
        "agreement",
        help="how far two judges agree on the pairs they both labelled, as "
             "Cohen's kappa - raw agreement flatters when almost nothing is relevant",
    )
    j_agree.add_argument("a", help="one judge")
    j_agree.add_argument("b", help="the other")

    cal = sub.add_parser(
        "calibrate",
        help="what this store's own judgements say about the resolution thresholds",
    )
    cal.add_argument(
        "--sample-below", type=int, metavar="N",
        help="also draw N random pairs from BELOW the review bar, the only way to "
             "learn what the bar is missing",
    )

    mg = sub.add_parser(
        "merge", help="fold one node into another, once confirmed to be one entity"
    )
    mg.add_argument("--into", metavar="ID", required=True, help="the node that survives")
    mg.add_argument(
        "--from", metavar="ID", required=True, dest="from",
        help="the node folded in and deleted",
    )
    mg.add_argument("--session-id", metavar="ID", help="session to record in the audit log")

    un = sub.add_parser(
        "unmerge",
        help="take back an alias a node absorbed from a different entity",
    )
    un.add_argument(
        "--list", action="store_true", dest="list_aliases",
        help="show every node answering to another node's name",
    )
    un.add_argument("--node", metavar="ID", help="the node holding the wrong alias")
    un.add_argument("--alias", metavar="NAME", help="the alias to take back")
    un.add_argument("--session-id", metavar="ID", help="session to record in the audit log")

    notice = sub.add_parser(
        "notice", help="queue a memory file for ingestion (called by the capture hook)"
    )
    notice.add_argument("path", type=Path, help="the memory file that changed")
    notice.add_argument(
        "--project", metavar="NAME",
        help="project it belongs to (default: detected from the file's own path or cwd)",
    )

    pending = sub.add_parser("pending", help="memory files noticed but not yet in the graph")
    pending.add_argument("--project", metavar="NAME", help="only this project")
    pending.add_argument(
        "--done", nargs="+", metavar="PATH", help="mark these paths as ingested"
    )
    pending.add_argument(
        "--session", metavar="ID",
        help="the session closing them; the Stop gate puts this on the line it "
             "prints, so a closure can be credited to the firing that asked for it",
    )

    hooks_parser = sub.add_parser(
        "install-hooks",
        help="register every capture hook in ~/.claude/settings.json",
    )
    hooks_parser.add_argument(
        "--dry-run", action="store_true", help="show what would be registered, change nothing"
    )
    hooks_parser.add_argument(
        "--settings", type=Path, default=None,
        help="settings file to write (default: ~/.claude/settings.json)",
    )

    recon = sub.add_parser(
        "reconcile",
        help="re-notice memory files the capture hook missed or that changed on disk",
    )
    recon.add_argument("--project", metavar="NAME", help="only this project")
    recon.add_argument(
        "--quiet", action="store_true", help="say nothing (for the session-start hook)"
    )

    stop = sub.add_parser(
        "stop-check",
        help="what this project still owes the graph, for the Stop hook",
    )
    stop.add_argument(
        "--hook-json", action="store_true", help="emit Stop hook JSON instead of plain text"
    )
    stop.add_argument(
        "--session-id", default=None,
        help="the calling session, so the gate holds it open at most once",
    )

    rec = sub.add_parser(
        "recall", help="facts matching a prompt, for the UserPromptSubmit hook"
    )
    rec.add_argument("prompt", nargs="?", default="", help="prompt text (default: stdin)")
    rec.add_argument(
        "--hook-json", action="store_true",
        help="emit UserPromptSubmit hook JSON instead of plain text",
    )
    rec.add_argument("--top-k", type=int, default=recall_cmd.DEFAULT_TOP_K)
    rec.add_argument(
        "--session-id", default=None,
        help="the calling session, so a read can be tied to the write it produced",
    )

    ana = sub.add_parser(
        "analyse", help="first-run comprehension pass for an existing project"
    )
    ana.add_argument(
        "--done", action="store_true",
        help="record that the pass has run, so the session briefing stops asking",
    )
    ana.add_argument("--project", metavar="NAME", help="override the detected project")
    ana.add_argument(
        "--root", type=Path, help="project directory to read (default: the current one)"
    )

    brief = sub.add_parser(
        "session-brief",
        help="what memory knows about this project, for the SessionStart hook",
    )
    brief.add_argument(
        "--hook-json", action="store_true",
        help="emit Claude Code's SessionStart hook JSON instead of plain text",
    )
    brief.add_argument(
        "--project", metavar="NAME", help="override the detected project"
    )

    bench = sub.add_parser(
        "benchmark", help="cost and latency baseline for a real ingest + query cycle"
    )
    bench.add_argument(
        "--rounds", type=int, default=5, help="cycles to measure (default: 5)"
    )

    edit = sub.add_parser(
        "notice-edit",
        help="record that a session edited a file (PostToolUse hook; counts only)",
    )
    edit.add_argument("--session-id", required=True)
    edit.add_argument("--project", default=None, help="default: detected from cwd")

    sub.add_parser(
        "reindex",
        help="re-embed every fact with the current embedding text (run after an upgrade)",
    )

    ev = sub.add_parser(
        "eval", help="retrieval quality against this store, for comparing configurations"
    )
    ev.add_argument(
        "--limit", type=int, default=None,
        help="cases to score (default: every fact joining two distinct entities)",
    )
    ev.add_argument(
        "--ablate", action="store_true",
        help="also score the configurations each retrieval change was chosen against",
    )
    ev.add_argument(
        "--shape", choices=["all", "entity_pair", "entity_single", "prose", "multihop"],
        default="all",
        help="query shape (default: all four; one shape alone can invert a conclusion)",
    )
    ev.add_argument(
        "--context", action="store_true",
        help="report what a recall costs in tokens against injecting the whole "
             "scope, with the hit rate beside it",
    )
    ev.add_argument(
        "--sweep", action="store_true",
        help="with --context, measure the saving at several corpus sizes "
             "instead of only the current one; slow, because it rebuilds and "
             "re-queries scratch scopes rather than extrapolating",
    )
    # The other other evaluation. `eval` scores this store against itself,
    # which cannot be compared with anybody else's number; this runs a
    # published benchmark, on the corpus its published numbers were taken on.
    ext = sub.add_parser(
        "eval-external",
        help="run a published benchmark (LoCoMo, LongMemEval) through the real "
             "write and query path",
    )
    ext.add_argument("dataset", choices=sorted(external_datasets))
    ext.add_argument(
        "path",
        help="the dataset file, which you download yourself: neither corpus is "
             "redistributable here (see docs/BENCHMARKS.md for both URLs). "
             "tests/fixtures/ holds a small synthetic file in each schema",
    )
    ext.add_argument(
        "--per-type", type=int, default=0, metavar="N",
        help="LongMemEval only: take the first N instances of each question "
             "type. The honest way to run a subset, because the file is ordered "
             "by type and its first 70 instances are all one of them",
    )
    ext.add_argument(
        "--limit", type=int, default=0, metavar="N",
        help="take the first N instances. Smoke tests only: on LongMemEval a "
             "prefix is one question type rather than a sample",
    )
    ext.add_argument(
        "--top-k", type=int, default=30, metavar="K",
        help="facts per query (default: 30, the largest k reported)",
    )
    ext.add_argument("--json", metavar="PATH", default="",
                     help="write the machine-readable report here")
    ext.add_argument(
        "--results", metavar="PATH", default="",
        help="append each question's row as it is scored, so a killed run keeps "
             "what it measured",
    )
    ext.add_argument(
        "--force", action="store_true",
        help="run even though this database already holds memory outside the "
             "benchmark's own scopes",
    )

    bench.add_argument(
        "--group", metavar="ID", default="benchmark:scratch",
        help="scope to write throwaway probe facts into (default: a dedicated "
             "benchmark group, never your real memory)",
    )
    bench.add_argument(
        "--seed-facts", type=int, metavar="N", default=None,
        help="fill the scope to N facts before measuring, so read latency "
             "describes a store someone might have (default: 250; 0 measures "
             "whatever is already there)",
    )

    boot = sub.add_parser(
        "bootstrap", help="import the work that already exists on this machine"
    )
    boot.add_argument(
        "--force", action="store_true", help="sweep again even if discovery has already run"
    )
    boot.add_argument("--dry-run", action="store_true", help="list what would be queued, queue nothing")
    boot.add_argument(
        "--only", action="append", choices=list(bootstrap_mod.SOURCES), metavar="SOURCE",
        help=f"limit to one source; repeatable ({', '.join(bootstrap_mod.SOURCES)})",
    )

    health_parser = sub.add_parser(
        "health", help="whether this graph is in good shape, and what to do about it"
    )
    health_parser.add_argument(
        "--json", action="store_true", help="machine-readable output"
    )

    skill_parser = sub.add_parser(
        "skill",
        help="print the usage instructions, for a client that has no config file",
    )
    skill_parser.add_argument(
        "--for", dest="client", choices=["claude-desktop", "generic"],
        default="claude-desktop",
        help="who the instructions are addressed to (default: claude-desktop)",
    )
    skill_parser.add_argument(
        "--package", metavar="PATH", type=Path,
        help="write an uploadable skill zip here instead of printing the text",
    )

    adopt_parser = sub.add_parser(
        "adopt", help="wire every MCP client on this machine to one memory"
    )
    adopt_parser.add_argument(
        "--apply", action="store_true",
        help="actually write the config files (default: show what would change)",
    )
    adopt_parser.add_argument(
        "--force", action="store_true",
        help="repoint a client registered against a different database",
    )

    initdb_parser = sub.add_parser(
        "init-db", help="create or upgrade the schema (works from a pip install)"
    )
    initdb_parser.add_argument(
        "--check", action="store_true",
        help="report the schema version instead of changing anything",
    )

    inst = sub.add_parser(
        "install", help="wire Echo Memory into one project instead of every project"
    )
    inst.add_argument(
        "--global", action="store_true", dest="user_global",
        help="install the skill once for every project, in ~/.claude/skills, "
             "instead of into one repo",
    )
    inst.add_argument(
        "--no-bootstrap", action="store_true",
        help="skip the first-run sweep for work that already exists on this machine",
    )
    inst.add_argument(
        "root", nargs="?", type=Path, default=Path("."),
        help="project directory (default: the current one)",
    )
    inst.add_argument(
        "--for", dest="targets", choices=["claude", "cursor", "codex", "all"], default="claude",
        help="which tool to set up (default: claude)",
    )
    inst.add_argument(
        "--project", metavar="NAME",
        help="project name to attribute facts to (default: the directory's own name)",
    )


def _add_trial_parser(sub) -> None:
    """v1a criterion 6 is the only Success Criterion whose bars are human
    judgements (see docs/designs/echo-memory-design.md). These record them, so
    the v1a -> v1b gate is decided from what was actually observed during the
    trial rather than from what anyone remembers of it."""
    trial_parser = sub.add_parser("trial", help="record and review v1a exit-criteria observations")
    trial = trial_parser.add_subparsers(dest="trial_command", required=True)

    start = trial.add_parser("start", help="start the trial clock (the 3-week cap)")
    start.add_argument(
        "--on", type=date.fromisoformat, metavar="YYYY-MM-DD",
        help="start date, if the trial really began before you got round to recording it",
    )
    start.add_argument(
        "--restart", metavar="WHY",
        help="close the open run and begin a new one, recording why the old "
             "one stopped counting",
    )
    start.add_argument(
        "--cap-days", type=int, default=observations.DEFAULT_CAP_DAYS,
        help=f"hard cap in days (default: {observations.DEFAULT_CAP_DAYS})",
    )

    save = trial.add_parser(
        "save", help="log a recalled fact that saved re-explaining something"
    )
    save.add_argument("note", help="what it saved re-explaining")
    save.add_argument(
        "--from", dest="written_by", required=True, metavar="TOOL",
        help="the tool that originally recorded the fact (criterion 6 counts an instance "
             "only when this differs from --into)",
    )
    save.add_argument(
        "--into", dest="recalled_by", metavar="TOOL",
        help="the tool that recalled it (default: this CLI's ECHO_MEMORY_AGENT_ID)",
    )

    dup = trial.add_parser("dup", help="confirm two nodes are one entity split in two")
    dup.add_argument("node_a")
    dup.add_argument("node_b")
    dup.add_argument("note", help="why they're the same entity")

    not_dup = trial.add_parser("not-dup", help="dismiss a similar-looking pair as genuinely distinct")
    not_dup.add_argument("node_a")
    not_dup.add_argument("node_b")
    not_dup.add_argument("note", nargs="?", default="reviewed, distinct entities")

    bad_merge = trial.add_parser("bad-merge", help="record an entity resolution that merged two distinct entities")
    bad_merge.add_argument("audit_entry_id", type=int, help="as shown by `echo-memory trial check`")
    bad_merge.add_argument("note", help="which two entities were wrongly merged")

    merge_ok = trial.add_parser("merge-ok", help="confirm an entity resolution was correct")
    merge_ok.add_argument("audit_entry_id", type=int)
    merge_ok.add_argument("note", nargs="?", default="reviewed, correct merge")

    retract_parser = trial.add_parser(
        "retract",
        help="stop an observation counting, keeping the record of it and why",
    )
    retract_parser.add_argument("observation_id", type=int, help="as shown by `trial log`")
    retract_parser.add_argument("reason", help="why it should not count")

    check_parser = trial.add_parser("check", help="criterion 6 status and what's awaiting review")
    check_parser.add_argument(
        "--all", action="store_true", dest="include_exact",
        help="also review exact-name entity resolutions, excluded by default as near-always correct",
    )
    check_parser.add_argument(
        "--all-projects", action="store_true", dest="all_projects",
        help="also review node pairs spanning unrelated projects, suppressed by default "
             "because two codebases sharing vocabulary is not a split entity",
    )

    trial.add_parser("log", help="every trial observation recorded so far")


# Each command's logic lives in its own module; main.py wires parsers to them
# and nothing else. Adding a command should not mean editing a shared dispatch
# chain, which is how this file grew to hold twelve of them.
_PROJECT_COMMANDS = {
    "dashboard": dashboard_cmd.run,
    "reattribute": reattribute_cmd.run,
    "unmerge": unmerge.run,
    "calibrate": calibrate.run,
    "merge": merge.run,
    "notice": queue_cmd.run_notice,
    "pending": queue_cmd.run_pending,
    "judge": judge_cmd.run,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Before load_config, deliberately. quickstart exists for a machine where
    # nothing is configured yet, so requiring ECHO_MEMORY_DATABASE_URL to reach
    # the command that sets it up would be a circle.
    if args.command == "quickstart":
        return quickstart.run(args)

    # Same reason as quickstart: a machine using the hosted service has no
    # local database, so requiring a database URL to configure it is a circle.
    if args.command == "connect":
        return connect_cmd.run(args)

    try:
        config = load_config()
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if args.command == "status":
        conn = connect(config.database_url)
        print(render_status(fetch_status(conn, config), check.build_report(conn, config)))
        return 0

    if args.command == "trial":
        return trial_cmd.run(args, config, connect(config.database_url))

    if args.command == "install-hooks":
        scripts_dir = Path(__file__).resolve().parents[3] / "scripts"
        bin_path = str(Path(sys.argv[0]).resolve())
        if args.dry_run:
            entries = hooks_cmd.plan(config, scripts_dir, bin_path)
            print(hooks_cmd.render(
                {"path": str(args.settings or hooks_cmd.SETTINGS),
                 "events": [e["event"] for e in entries]}, dry_run=True), end="")
            return 0
        try:
            result = hooks_cmd.apply(config, scripts_dir, bin_path, args.settings)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(hooks_cmd.render(result), end="")
        return 0

    if args.command == "reconcile":
        conn = connect(config.database_url)
        result = reconcile_cmd.reconcile(conn, project=args.project)
        if not args.quiet:
            print(reconcile_cmd.render(result), end="")
        return 0

    if args.command == "stop-check":
        return stop_gate.run(args, config, connect(config.database_url))

    if args.command == "recall":
        prompt = args.prompt or sys.stdin.read()
        conn = connect(config.database_url)
        result = recall_cmd.recall_for_prompt(conn, config, prompt, args.top_k)
        context = recall_cmd.render_context(result)
        recall_cmd.record_read(conn, config, result, context, args.session_id)
        if args.hook_json:
            # Nothing relevant: stay silent rather than injecting an empty
            # block into every prompt the user types.
            if context:
                print(recall_cmd.render_hook_output(context))
        else:
            print(context or "(nothing relevant)")
        return 0

    if args.command == "analyse":
        project = args.project or config.project
        root = args.root or Path.cwd()
        conn = connect(config.database_url)
        sources = analyse_cmd.find_sources(root)
        if args.done:
            group_ids = [config.group_id(sc) for sc in ("solo", "shared")]
            n = conn.execute(
                """SELECT count(*) FROM public.audit_entry WHERE group_id = ANY(%s)""",
                (group_ids,),
            ).fetchone()[0]
            analyse_cmd.mark_analysed(conn, project, n, [s["path"] for s in sources])
            print(f"Recorded a comprehension pass for '{project}'.")
            return 0
        if analyse_cmd.has_been_analysed(conn, project):
            print(f"'{project}' has already had a comprehension pass. Re-run anyway:")
        print(analyse_cmd.render_instruction(project, sources))
        return 0

    if args.command == "session-brief":
        project = args.project or config.project
        conn = connect(config.database_url)
        brief = session_start_cmd.build_brief(conn, config, project, Path.cwd())
        context = session_start_cmd.render_brief(brief)
        print(session_start_cmd.render_hook_output(context) if args.hook_json else context)
        return 0

    if args.command == "notice-edit":
        from echo_memory.ingestion import activity

        # Never fails the edit that triggered it. Same contract as the capture
        # hook it runs beside: a memory side effect must not break the tool call
        # it is observing.
        try:
            conn = connect(config.database_url)
            activity.record_edit(conn, args.session_id, args.project or config.project)
        except Exception as e:  # noqa: BLE001
            # Swallowed on purpose, and logged to stderr rather than silently:
            # a database that is down must not break the edit being observed,
            # but it should still be findable when someone asks why the gate
            # went quiet.
            print(f"notice-edit skipped: {e}", file=sys.stderr)
        return 0

    if args.command == "reindex":
        from echo_memory.cli.reindex import reindex
        from echo_memory.cli.reindex import render as render_reindex
        from echo_memory.ingestion.embeddings import LocalEmbedder

        conn = connect(config.database_url)
        group_ids = [config.group_id(s) for s in ("solo", "shared")]

        def progress(done, total):
            print(f"  {done}/{total}", file=sys.stderr)

        print(render_reindex(reindex(conn, group_ids, LocalEmbedder(), progress)), end="")
        return 0

    if args.command == "eval":
        from echo_memory.eval.retrieval import (
            SHAPES,
            build_cases,
            corpus_tokens,
            render,
            render_context_saving,
            run,
        )
        from echo_memory.ingestion.embeddings import LocalEmbedder

        conn = connect(config.database_url)
        group_id = config.group_id(args.scope)
        shapes = SHAPES if args.shape == "all" else (args.shape,)

        embedder = LocalEmbedder()
        configs = [("shipping", {})]
        if args.ablate:
            configs += [
                ("with MMR", {"use_mmr": True}),
                ("static floor 0.15", {"floor": 0.15}),
                ("vector only", {"vector_only": True}),
                ("lexical only", {"lexical_only": True}),
                ("+ graph hop", {"graph_hops": 1}),
                ("+ BM25 lexical", {"lexical_bm25": True}),
            ]

        results = []
        for shape in shapes:
            cases = build_cases(conn, group_id, args.limit, shape=shape)
            if not cases:
                continue
            results += [run(conn, group_id, embedder, cases, n, **kw) for n, kw in configs]

        if not results:
            print(
                "no cases: this scope has no fact joining two distinct entities.\n"
                "A self-loop makes a query of one repeated word, which measures nothing.",
                file=sys.stderr,
            )
            return 1

        if args.context and args.sweep:
            from echo_memory.eval.sweep import measure
            from echo_memory.eval.sweep import render as render_sweep

            def sweeping(size, total):
                print(f"  building a {size:,} fact scope of {total:,}", file=sys.stderr)

            print(render_sweep(measure(conn, group_id, embedder, progress=sweeping)))
            return 0

        if args.context:
            # One row per shape, and only the shipping configuration: this
            # measures what memory costs to use, not which retrieval variant
            # wins, and mixing the two tables would invite reading a saving as
            # though it were an ablation result.
            facts, whole = corpus_tokens(conn, group_id)
            shipping = [r for r in results if r.name == "shipping"]
            print(render_context_saving(shipping, facts, whole))
            return 0

        print(render(results))
        return 0

    if args.command == "eval-external":
        from echo_memory.eval import external
        from echo_memory.ingestion.embeddings import LocalEmbedder

        try:
            external.check_flags(args.dataset, args.per_type)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

        if not Path(args.path).is_file():
            print(
                f"error: no such file: {args.path}\n"
                f"This corpus is not in the repository and is yours to fetch:\n"
                f"  {external.DATASET_URLS[args.dataset]}\n"
                f"tests/fixtures/ holds a small synthetic file in the same schema.",
                file=sys.stderr,
            )
            return 2

        conn = connect(config.database_url)
        prefix = external.PREFIXES[args.dataset]
        held = external.foreign_facts(conn, prefix)
        if held and not args.force:
            print(
                f"error: this database holds {held:,} fact(s) outside {prefix}: scopes.\n"
                f"A {args.dataset} run writes real facts through the real write path, so "
                f"anything\nit adds is indistinguishable from ordinary memory afterwards. "
                f"Point\nECHO_MEMORY_DATABASE_URL at a scratch database, or pass --force if "
                f"this is one.",
                file=sys.stderr,
            )
            return 1

        embedder = LocalEmbedder()
        embedder.embed("warm")  # the first call loads the model; keep it out of the rate

        try:
            result = external.run(
                conn, args.dataset, args.path, embedder,
                limit=args.limit, per_type=args.per_type, top_k=args.top_k,
                results_path=args.results,
                progress=lambda line: print(line, file=sys.stderr, flush=True),
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

        if args.json:
            Path(args.json).write_text(json.dumps(external.report(result), indent=2) + "\n")
            print(f"wrote {args.json}", file=sys.stderr)
        print(external.render(result), end="")
        return 0 if result.rows else 1

    if args.command == "benchmark":
        from echo_memory.ingestion.embeddings import LocalEmbedder

        conn = connect(config.database_url)
        if args.rounds < 1:
            print("error: --rounds must be at least 1", file=sys.stderr)
            return 1
        from echo_memory.cli.benchmark import DEFAULT_SEED_FACTS

        seed = DEFAULT_SEED_FACTS if args.seed_facts is None else args.seed_facts
        print(render_benchmark(
            run_benchmark(conn, args.group, LocalEmbedder(), args.rounds, seed_facts=seed)),
              end="")
        return 0

    if args.command == "bootstrap":
        conn = connect(config.database_url)
        sources = tuple(args.only) if args.only else bootstrap_mod.SOURCES
        if args.dry_run:
            found = bootstrap_mod.discover(sources=sources)
            print(f"Would queue {len(found)} document(s):")
            for item in found:
                print(f"  [{item['project']}] ({item['source']}) {item['path']}")
            return 0
        result = bootstrap_mod.run(conn, sources=sources, force=args.force)
        print(bootstrap_mod.render(result), end="")
        return 0

    if args.command == "health":
        conn = connect(config.database_url)
        report = health.collect(conn, config)
        if args.json:
            print(json.dumps({**report, "score": health.score(report)}, indent=2))
        else:
            print(health.render(report), end="")
        return 0

    if args.command == "skill":
        if args.package is not None:
            written = package_skill(args.package)
            print(f"Wrote {written}")
            print("Claude Desktop: Settings > Capabilities > Skills > Upload skill.")
            return 0
        print(render_skill(args.client), end="")
        return 0

    if args.command == "adopt":
        results = (
            adopt.apply(config, force=args.force) if args.apply else adopt.plan(config)
        )
        print(
            adopt.render(results, applied=args.apply, manual=adopt.manual_steps(config)),
            end="",
        )
        return 0

    if args.command == "init-db":
        try:
            if args.check:
                initdb.current(config.database_url)
            else:
                initdb.upgrade(config.database_url)
                print("Schema is at head. Echo Memory is ready to use.")
        except Exception as e:
            hint = initdb.explain(e)
            if hint is None:
                raise
            print(f"error: {hint}", file=sys.stderr)
            return 1
        return 0

    if args.command == "install":
        if getattr(args, "user_global", False):
            for line in install.install_global(Path.home()):
                print(line)
            print(
                "\nThe skill now applies in every project. MCP registration is still "
                "per-client:\n  claude mcp add --scope user echo-memory -- echo-memory serve"
            )
            return 0
        targets = (
            ("claude", "cursor", "codex") if args.targets == "all" else (args.targets,)
        )
        root = args.root.resolve()
        if not root.is_dir():
            print(f"error: not a directory: {root}", file=sys.stderr)
            return 1
        project = args.project or detect_project(str(root), env={})
        try:
            done = install(root, config, targets, project=project)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(render_install(root, project, targets, done), end="")
        if not args.no_bootstrap:
            conn = connect(config.database_url)
            result = bootstrap_mod.run(conn)
            if not result["skipped"]:
                print()
                print(bootstrap_mod.render(result), end="")
        return 0

    if args.command in _PROJECT_COMMANDS:
        return _PROJECT_COMMANDS[args.command](args, config, connect(config.database_url))

    group_id = config.group_id(args.scope)

    if args.command == "graph":
        if args.html:
            # Renders the dashboard, which supersedes the old single-scope
            # snapshot: every scope, faceted by project, with an inspector that
            # answers what a fact says, who wrote it, when and why. Kept as an
            # alias so a command shipped last week still works.
            conn = connect(config.database_url)
            data = fetch_dashboard(conn, config)
            args.html.write_text(render_dashboard(data))
            n_facts = sum(len(sc["facts"]) for sc in data["scopes"].values())
            print(
                f"Wrote {args.html} ({n_facts} facts across "
                f"{len(data['projects'])} projects)."
            )
            print(
                "Note: --html now renders the full dashboard, so it covers every scope "
                "rather than just --scope, and includes superseded facts as history. "
                "`echo-memory dashboard` is the command for this going forward."
            )
            return 0
        if args.watch:
            try:
                while True:
                    graph = fetch_graph(connect(config.database_url), group_id)
                    os.system("clear")
                    print(render_graph(args.scope, group_id, graph))
                    print(f"(refreshing every {args.interval}s, ctrl-C to stop)")
                    time.sleep(args.interval)
            except KeyboardInterrupt:
                return 0
        graph = fetch_graph(connect(config.database_url), group_id)
        print(render_graph(args.scope, group_id, graph))
        return 0

    conn = connect(config.database_url)

    if args.command == "why":
        result = get_fact_history(conn, group_id, args.fact_id)
        print(render_history(args.fact_id, result["entries"]))
        return 0

    if args.command == "export":
        result = export_group(conn, group_id, args.out)
        print(
            f"Exported {result['n_nodes']} nodes, {result['n_facts']} facts "
            f"to {result['out_dir']}"
        )
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
