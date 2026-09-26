"""python -m echo_memory.server: wires write_episode, query_memory,
record_recall_save, get_audit_log, pending_documents and mark_ingested into one
MCP server (see the design doc's MCP tool contract).
Runs over stdio by default (mcp.server.mcpserver's MCPServer.run default),
not a network listener at all, let alone one bound beyond localhost; see
the design doc's Constraints ("v1 is single-user, local-only")."""

import atexit
import inspect
import json
import threading
import time

import psycopg
from mcp.server.mcpserver import MCPServer

from echo_memory import __version__
from echo_memory.audit.get_audit_log import get_audit_log as _get_audit_log
from echo_memory.infra.config import Config, ConfigError, load_config
from echo_memory.infra.db import GRAPH_NAME as GRAPH
from echo_memory.infra.logging import configure_logging, get_logger
from echo_memory.infra.pool import make_pool
from echo_memory.infra.project import UNKNOWN as UNKNOWN_AGENT
from echo_memory.ingestion import bootstrap as bootstrap_mod
from echo_memory.ingestion import capture
from echo_memory.ingestion.embeddings import Embedder, LocalEmbedder
from echo_memory.ingestion.write_episode import write_episode as _write_episode
from echo_memory.retrieval.causality import trace_cause as _trace_cause
from echo_memory.retrieval.query_memory import query_memory as _query_memory
from echo_memory.trial import observations as _observations
from echo_memory.trial import reads as _reads

server = MCPServer(
    name="echo-memory",
    # MCPServer defaults this to "", so every client that shows which server
    # it connected to displayed a blank - and the first question asked about a
    # memory bug is which version wrote the fact. Same value write_episode
    # stamps on every audit entry, so the handshake and the data agree.
    version=__version__,
    instructions=(
        "Persistent memory across sessions and tools, backed by your own local "
        "database. Use it proactively, without being asked - don't wait for a "
        "natural stopping point. Call write_episode IN THE SAME TURN whenever "
        "any of these happen: the user states a decision (\"we're using X\", "
        "\"X only deploys from branch Y\"), corrects something you said or did "
        "(\"actually, X not Y\"), states a preference, or says anything like "
        "\"remember this\"/\"for future reference\"/\"don't do that again\". If "
        "you notice one of these mid-task, call write_episode right then, not "
        "batched up at the end. Skip only genuinely throwaway exchanges (typo "
        "fixes, one-off questions with no lasting relevance) - the cost of a "
        "missed memory is higher than the cost of one extra call. Call "
        "query_memory at the start of a session, and any other time recalling "
        "prior context would save the user from re-explaining something they "
        "likely already told a different tool or a past session - check here "
        "before asking them to repeat themselves. When something recalled here "
        "spares the user re-explaining - most of all when a different tool or "
        "an earlier session wrote it - call record_recall_save in the same "
        "turn, passing the fact_id from the query_memory result that actually "
        "helped. Nothing else records that: reads and writes are logged "
        "automatically, this one only happens if you make it happen. Do not "
        "call it speculatively, or for a fact you wrote yourself - an inflated "
        "count is worse than an empty one. Call pending_documents at "
        "the start of a session too: it lists memory files this project has "
        "written that the graph has not heard about yet. Read each one, "
        "write_episode what it states, then mark_ingested to close it - "
        "without that last step the file stays queued and the next session is "
        "asked to do the same work again."
    ),
)


class ServerState:
    """Not module-level globals directly: keeps startup() testable without
    mutating process-wide state that other tests might also touch."""

    config: Config
    warming = None
    pool: object
    embedder: Embedder


_state = ServerState()


def startup(config: Config | None = None, embedder: Embedder | None = None) -> None:
    # Structured logs to stderr. stdout is the MCP protocol channel.
    configure_logging()
    _state.config = config or load_config()
    _state.pool = make_pool(_state.config.database_url)
    _state.embedder = embedder or LocalEmbedder()

    # In the background, so the client's handshake is not held for seven
    # seconds, and so the load happens while the user is still typing rather
    # than inside their first question.
    #
    # Claude Desktop's server log on 2026-09-12 shows what it costs otherwise:
    # query_memory at 6099ms and 5994ms against other calls at 5ms. The model
    # is lazy on purpose - a CLI that prints a queue must not download one -
    # but a long-lived server knows it will need it and has an idle moment at
    # startup to pay for it.
    warm = getattr(_state.embedder, "warm", None)
    _state.warming = None
    if warm is not None:
        # Kept on the state rather than fired and forgotten: a caller that
        # needs the load finished - a test, or a one-shot script - can join it,
        # and a daemon thread that logs after its process has moved on is how
        # a background task turns into confusing output somewhere else.
        _state.warming = threading.Thread(target=_warm, args=(warm,), daemon=True)
        _state.warming.start()
        # Wait for it at exit, briefly, rather than letting the interpreter
        # tear down underneath it.
        #
        # A daemon thread is killed abruptly at shutdown. This one is inside
        # torch when that happens, and killing a thread mid C++ produces
        # "terminate called without an active exception" and SIGABRT - exit
        # code 134, after every test has already passed. CI saw exactly that:
        # 771 passed, 5 skipped, then aborted. It is timing dependent, so it
        # is rare on a machine with the model cached and common on one
        # downloading it, which is the wrong way round for a release gate.
        #
        # Bounded, because the point of loading in the background is that
        # nothing waits for it. A load still running after this long is one
        # the process was never going to benefit from anyway.
        atexit.register(_finish_warming)


WARM_SHUTDOWN_GRACE_SECONDS = 10.0


def _finish_warming() -> None:
    warming = getattr(_state, "warming", None)
    if warming is not None and warming.is_alive():
        warming.join(timeout=WARM_SHUTDOWN_GRACE_SECONDS)


def _warm(warm) -> None:
    """Never raises. A model that fails to preload will fail again on the first
    real call, where the caller can be told about it; killing a background
    thread with a traceback on stderr would only corrupt an MCP server's log."""
    try:
        started = time.perf_counter()
        warm()
        _logger.info(
            "embedder_warm",
            extra={"duration_ms": (time.perf_counter() - started) * 1000},
        )
    except Exception:  # noqa: BLE001 - see docstring
        # Logging can itself fail here, and does: a thread still running while
        # the interpreter shuts down finds the log stream closed and atexit
        # refusing new registrations, so reporting the first failure raises a
        # second one out of a thread with nowhere to put it. The report is
        # best effort; the swallow is the contract in the docstring.
        try:
            _logger.warning("embedder_warm_failed", exc_info=True)
        except Exception:  # noqa: BLE001, S110 - nowhere left to report to
            pass


def _tool(fn):
    """Register a tool with its docstring dedented.

    The SDK publishes `fn.__doc__` verbatim, so every line of a tool
    description arrives at the client carrying the four spaces that put the
    docstring inside a function. Claude Code truncates a description at 2048
    characters (see tests/unit/test_tool_descriptions.py), and on
    write_episode that indentation was 140 of them - 7% of the budget spent
    on whitespace no reader wanted. cleandoc strips it and nothing else.
    """
    return server.tool(description=inspect.cleandoc(fn.__doc__ or ""))(fn)


@_tool
def write_episode(
    scope: str,
    session_id: str,
    entities: list[dict],
    facts: list[dict],
    entity_resolutions: dict | None = None,
    assume_new: bool = False,
) -> dict:
    """Record something worth remembering later: a decision, a correction, a
    stated preference, or context that would otherwise be re-explained to
    another tool or a later session. Call it the moment you notice one, not
    batched and not at the end - a missed memory costs more than an extra
    call. You extract the entities and facts; this server never infers.

    entities:
      name  non-empty, unique within this call
      type  any short string ("tool", "person", "decision") - not an enum

    facts:
      source/target   must each exactly match a name in entities
      relation_type   any short string ("uses", "decided") - not an enum
      fact            the sentence to remember, plain text
      confidence      exactly one of "extracted" (stated), "inferred"
                      (deduced), "ambiguous" (uncertain). Anything else,
                      or a number, is rejected.
      causal_hint     optional, only when the session said so: caused_by,
                      led_to, enabled_by, blocked_by, contradicts. What
                      trace_cause walks. Omit when merely associative.

    entity_resolutions (optional): only after a call returned
    ambiguous_entities, to say which candidate a mention meant:
    {"mention": {"resolved_to": "<node_id>" | "new"}}. Omit otherwise.

    assume_new (optional): True when you know every entity here is new - a
    symbol just read, a title just coined. It answers "new" up front, so the
    call cannot come back asking. Explicit resolutions win.

    related_entities in the reply: names this scope already uses for what
    you just wrote. Reuse them rather than coin a near-synonym. Advisory;
    no reply needed.

    Example:
    write_episode(scope="solo", session_id="s1",
      entities=[{"name": "Postgres", "type": "tool"},
                {"name": "Decision", "type": "decision"}],
      facts=[{"source": "Decision", "target": "Postgres",
              "relation_type": "uses", "confidence": "extracted",
              "fact": "decided to use Postgres for storage"}])
    """
    try:
        group_id = _state.config.group_id(scope)
    except ConfigError as e:
        return {"error": str(e)}
    try:
        with _state.pool.connection() as conn:
            return _write_episode(
                conn, group_id, session_id, entities, facts, entity_resolutions, _state.embedder,
                project=_state.config.project, agent_id=_state.config.agent_id,
                assume_new=assume_new,
            )
    except psycopg.OperationalError as e:
        return _operational_error(e)



@_tool
def trace_cause(
    scope: str,
    subject: str,
    direction: str = "both",
    max_hops: int = 3,
) -> dict:
    """Why something happened, not what resembles it. Follows the causal links
    recorded on facts about `subject` and returns them as chains, ordered from
    the subject outwards.

    Use this when the question is "why", "what did this break", "what was this
    a consequence of", or "what happens if we undo it". query_memory ranks
    facts by similarity and returns a flat list, which cannot answer those -
    the chain is the answer.

    direction: "upstream" for what led here, "downstream" for what followed,
    "both" for both. max_hops: 1 to 6, default 3.

    causes/effects are chains, each a list of facts. contradictions are facts
    explicitly recorded as contradicting one of the matched entities.

    Only links a caller recorded are followed. Nothing here infers a cause, so
    an empty answer means nobody asserted one - not that none exists. Pass
    causal_hint on a fact in write_episode to make it traceable."""
    try:
        group_id = _state.config.group_id(scope)
    except ConfigError as e:
        return {"error": str(e)}
    try:
        with _state.pool.connection() as conn:
            return _trace_cause(
                conn, group_id, subject, _state.embedder,
                direction=direction, max_hops=max_hops,
            )
    except psycopg.OperationalError as e:
        return _operational_error(e)


@_tool
def query_memory(
    scope: str, query: str | None = None, top_k: int = 10,
    digest: bool = False, as_of: int | None = None,
) -> dict:
    """Recall prior facts relevant to query, from this agent's own memory
    (scope="solo") or the pool shared across this user's agents
    (scope="shared"). Call this at session start, and any other time
    recalling prior context would save the user from re-explaining
    something - check here before asking them to repeat themselves or
    guessing at context you don't have.

    digest=True ignores query and returns the most recently written active
    facts instead, as an opt-in "catch me up" convenience; call it
    explicitly at session start if you want one, it's never automatic.

    as_of (optional): unix seconds. Answers with what this scope believed at
    that moment rather than now, including facts later superseded. Nothing is
    ever rewritten here, so the history is real: use it for "what did we think
    was true when this was decided".

    Each fact carries rank (1 first), score and matched.

    If you must drop facts to fit a budget, drop by rank. It is the fused
    result of every channel, which is this server's whole opinion; score is
    one input to it, and re-sorting by a single input throws the rest away.

    score is cosine similarity to your query, present on every fact and
    comparable across queries. It is diagnostic, not a correctness test: a
    fact found by exact keyword match can be the right answer at a low
    score. matched names the channels that found it, as information - full
    text search is not the weaker one, it has the better recall here.

    A pending_ingest field appears when memory files have been written that
    the graph hasn't heard about yet. Read each listed file and call
    write_episode with the entities and facts it states, then mark it done
    with `echo-memory pending --done <path>`. The queue exists because
    extraction needs a model and this server never calls one."""
    try:
        group_id = _state.config.group_id(scope)
    except ConfigError as e:
        return {"error": str(e)}
    try:
        with _state.pool.connection() as conn:
            result = _query_memory(
                conn, group_id, query, top_k, _state.embedder,
                digest=digest, as_of=as_of,
            )
        # The other read surface. Counted the same way so the ratio in
        # `echo-memory health` covers both, not just the hook.
        if "error" not in result:
            _reads.record(
                conn, group_id, _reads.QUERY,
                n_facts=len(result.get("facts") or []),
                injected_chars=sum(len(f.get("fact") or "") for f in result.get("facts") or []),
                # All three were already columns and none were being written on
                # this path, so every query_memory read landed unattributed
                # while hook reads carried project and session. That made the
                # main read surface the one nobody could account for.
                project=_state.config.project,
                agent_id=_state.config.agent_id,
                # Which facts, not just how many. A count cannot answer
                # "was this one delivered", which is the question
                # record_recall_save has to ask of its own evidence.
                fact_ids=[f.get("fact_id") for f in result.get("facts") or []],
            )
            _bootstrap_once(conn)
            queued = capture.pending(conn)
            if queued:
                result["pending_ingest"] = {
                    "count": len(queued),
                    "files": [{"path": q["path"], "project": q["project"]} for q in queued[:10]],
                    "instruction": (
                        "These memory files changed on disk. That is not the same as "
                        "their content being absent from the graph - a past session may "
                        "have written the facts and never marked the file done. Call "
                        "query_memory on each file's subject FIRST; if what it states is "
                        "already recorded, mark it done rather than writing it twice. "
                        "Otherwise call write_episode with what it states. Either way, "
                        "finish with `echo-memory pending --done <path>` for each."
                    ),
                }
            return result
    except psycopg.OperationalError as e:
        return _operational_error(e)


_logger = get_logger("server")


def _operational_error(e: psycopg.OperationalError) -> dict:
    """Turn a database outage into the typed {"error"} shape every tool
    already uses for ConfigError.

    Deliberately narrow. psycopg.OperationalError covers what is genuinely
    operational - connection lost, server down, and PoolTimeout, which is a
    subclass - while ProgrammingError and IntegrityError are NOT subclasses and
    still propagate. That split matters: swallowing a bad query or a violated
    constraint into a polite message would hide a real bug behind an outage
    story, which is the over-catching this exists to avoid.

    The agent gets something it can act on ("the database is unreachable, tell
    the user") instead of a stack trace it can only relay.

    The recovery command is named because the raw psycopg text is not
    actionable on its own. On 2026-09-09 a client reported "couldn't get a
    connection after 5.00 sec" to its user, who then had to work out that the
    Docker VM was down; the message describes a symptom and stops. Naming the
    command costs one line and turns a report into a fix. It is also the one
    moment an agent has the user's attention on the subject, so it is the wrong
    place to be terse."""
    _logger.warning("database_unavailable", extra={"error_type": type(e).__name__})
    return {"error": (
        f"memory database unavailable: {e}. Nothing is lost - the store is on disk "
        "and unreachable, not empty. Tell the user to start it with "
        "`docker compose up -d db` from the echo-mem checkout (and `colima start` "
        "first if Docker itself is not running), then retry."
    )}


def _bootstrap_once(conn) -> None:
    """First initialisation sweeps the machine for work that already exists.

    A fresh store is empty, but the machine it runs on usually isn't: months of
    decisions already sit in per-project memory files, gstack learnings and
    CLAUDE.md files. Waiting for new sessions to slowly refill the graph throws
    all of that away and makes the user re-explain what they already wrote down.

    Runs at most once (guarded by bootstrap_state), and never fails a query:
    recall is the caller's actual request, and a discovery problem has no
    business breaking it."""
    try:
        if bootstrap_mod.has_run(conn):
            return
        result = bootstrap_mod.run(conn)
        _logger.info(
            "bootstrap_discovered",
            extra={"found": result["found"], "queued": result["queued"]},
        )
    except Exception as e:  # noqa: BLE001 - see docstring: never fail a query
        _logger.warning("bootstrap_failed", extra={"error": str(e)})


# Returned by `_author_of` for a fact that is really there but carries no
# agent_id at all. Distinct from None, which means no such fact in this scope.
# Collapsing the two sent an agent chasing a fact_id that was never the problem;
# see `_author_of`.
UNATTRIBUTED = object()


def _author_of(conn, group_id: str, fact_id: str) -> str | object | None:
    """The `agent_id` on a fact edge.

    Three outcomes, and they are not interchangeable:
      - the agent id, as a string
      - UNATTRIBUTED, when the edge exists but has no agent_id property at all
      - None, when no such fact is in this scope

    The middle case used to return None as well, so `record_recall_save`
    answered a real fact with "no fact <id> in this scope - pass the fact_id
    from a query_memory result, not a remembered one". That advice is not just
    wrong, it is unfollowable: the id DID come from query_memory, so the agent
    re-queries, gets the same id back, and tries again. Nothing it can do
    resolves the error, and the loop looks exactly like the one the Stop gate
    caused on 2026-09-02.

    Facts reach that state through a real path. A Claude Code MCP server is a
    long-lived stdio process that imports this package once, at spawn, and an
    editable install does not change that: a server started before `agent_id`
    shipped on 2026-08-23 kept writing facts without it. One such process
    (pid 53784, started 2026-08-22 17:47) was still writing on 2026-09-03 -
    30 facts in this store have no agent_id because of it. Apache AGE drops a
    property whose value is null on CREATE, so the key is absent rather than
    null, which is why `WHERE e.agent_id = 'unknown'` in migration 0007 never
    matched them. Migration 0011 backfills them; `_create_edge` now refuses to
    write another.

    Scoped by group_id on purpose: a fact_id from another tenant must not be
    citable as evidence here, and an unscoped lookup would let one."""
    try:
        edge_id = int(fact_id)
    except (TypeError, ValueError):
        return None
    # Parameters, not interpolation. `query_memory._any_term_tsquery` already
    # refuses to paste caller text into a query and cites the design doc's
    # security review for it; group_id embeds user_id and agent_id, both of
    # which `adopt` is about to write into machine-global config files.
    row = conn.execute(
        f"""SELECT * FROM cypher('{GRAPH}', $$
            MATCH ()-[e:FACT]->()
            WHERE id(e) = $edge_id AND e.group_id = $gid
            RETURN e.agent_id
        $$, %s) AS (agent_id agtype)""",
        (json.dumps({"edge_id": edge_id, "gid": group_id}),),
    ).fetchone()
    if row is None:
        return None
    # A row came back, so the edge exists and is in this scope. A null here is
    # a fact without provenance, which is a different problem from a fact that
    # isn't there, and gets a different answer.
    if row[0] is None:
        return UNATTRIBUTED
    return str(row[0]).strip('"')


@_tool
def record_recall_save(
    scope: str,
    fact_id: str,
    note: str,
) -> dict:
    """Record that a fact you recalled from memory saved the user from
    re-explaining something to you.

    Call this IN THE SAME TURN, the moment it happens. The trigger is
    concrete: you called query_memory (or read a memory-derived fact), it
    answered something the user would otherwise have had to tell you again,
    and the fact was originally written by a DIFFERENT tool or a past session.

    That is why this takes only a `fact_id`. Pass the one carried by the
    query_memory result that helped; the server reads that edge's own agent_id
    for the writer, and uses its own configured agent id for the reader. You
    assert neither. Both used to be caller-supplied, and each in turn let the
    model being graded type its own evidence.

    The server also checks its read log for the fact you cite, and refuses an
    id no read in this scope has ever returned. Pass the id you were given,
    not one you remember.

    If the fact's author and you are the same tool, the save is recorded but
    does not count - recalling your own note from ten minutes ago is not the
    thing being measured.

    note should be one sentence naming what it saved re-explaining, written so
    it still makes sense read cold in six months. Recording the identical note
    twice is a no-op, so a retry after an error is safe.

    Do NOT call this speculatively, for a fact you wrote this session, or
    because a recall was merely interesting. It is evidence for a gate that
    decides real build work; an inflated count is worse than an empty one."""
    try:
        group_id = _state.config.group_id(scope)
    except ConfigError as e:
        return {"error": str(e)}

    # Not a parameter: an agent asserting which tool it is, to a criterion that
    # measures whether two tools are involved, is the same fault the written_by
    # fix closed on 2026-08-29.
    recalled_by = _state.config.agent_id
    try:
        with _state.pool.connection() as conn:
            written_by = _author_of(conn, group_id, fact_id)
            if written_by is None:
                return {"error": (
                    f"no fact {fact_id} in this scope - pass the fact_id from a "
                    "query_memory result, not a remembered one"
                )}
            if written_by is UNATTRIBUTED:
                return {"error": (
                    f"fact {fact_id} carries no agent_id, so it cannot evidence a "
                    "cross-tool save. This is not something you can fix by "
                    "re-querying - the fact_id is correct. Run "
                    "`echo-memory init-db` to backfill it, then cite a "
                    "different fact for this save."
                )}
            if written_by == UNKNOWN_AGENT:
                return {"error": (
                    f"fact {fact_id} predates agent attribution (agent_id is "
                    f"'{UNKNOWN_AGENT}'), so it cannot evidence a cross-tool save. "
                    "Nothing recovers this - the session that knew is gone. Cite "
                    "a different fact."
                )}
            # Was this fact ever actually given to anybody?
            #
            # Both tool identities are derived rather than asserted, but until
            # now nothing checked the caller had RECEIVED the fact it cites -
            # the id was looked up directly, so an agent could name a fact it
            # never queried for. A reviewer of the paper written from this
            # work made the point, and it was the strongest objection of the
            # seven.
            #
            # Refused only when NO read in this scope ever returned it. A
            # weaker grade of evidence is recorded rather than rejected,
            # because most of this store's reads predate the column that would
            # corroborate them and a check that refuses every honest save
            # would destroy the criterion it is meant to strengthen.
            delivery = _reads.delivered(conn, group_id, fact_id, recalled_by)
            if delivery is None and _reads.has_delivery_log(conn, group_id):
                return {"error": (
                    f"no read in this scope ever returned fact {fact_id}, so it "
                    "cannot evidence a recall. Cite the fact_id from a "
                    "query_memory result you actually received in this session."
                )}
            try:
                recorded = _observations.record(
                    conn, group_id, _observations.RECALL_SAVE, note,
                    written_by=written_by, recalled_by=recalled_by,
                    delivery=delivery,
                )
            except _observations.TrialError as e:
                return {"error": str(e)}
            counts = _observations.counts(conn, [group_id])
    except psycopg.OperationalError as e:
        return _operational_error(e)

    cross_tool = written_by != recalled_by
    _logger.info(
        "recall_save_recorded",
        extra={
            "observation_id": recorded["id"], "newly_recorded": recorded["created"],
            "written_by": written_by, "recalled_by": recalled_by,
            "cross_tool": cross_tool, "group_id": group_id,
            "delivery": delivery,
        },
    )
    result = {
        "recorded": True,
        "observation_id": recorded["id"],
        "already_recorded": not recorded["created"],
        # Always returned, both branches. The caller supplies neither of these
        # now, so the only way it can see what the server concluded about
        # authorship - and check the save it just logged says what it meant -
        # is for the response to state it.
        "written_by": written_by,
        "recalled_by": recalled_by,
        "counts_toward_gate": cross_tool,
        # How strongly the read log corroborates the claim: "agent" if a read
        # by this tool returned the fact, "group" if some read in the scope
        # did, null if the log cannot say. The benefit is still the agent's
        # judgement; this is about whether the recall happened at all.
        "delivery": delivery,
        "cross_tool_saves": counts["cross_tool_saves"],
        "required": _observations.REQUIRED_SAVES,
    }
    if not cross_tool:
        result["note"] = (
            f"Recorded, but {written_by} both wrote and recalled this, so it does not count "
            "toward the trial's cross-tool bar."
        )
    return result


@_tool
def get_audit_log(scope: str, since: str | None = None) -> dict:
    """Human-readable audit trail: what was written, invalidated, superseded,
    or resolved, and why. since is an ISO8601 timestamp; entries at or after
    it, chronologically ordered."""
    try:
        group_id = _state.config.group_id(scope)
    except ConfigError as e:
        return {"error": str(e)}
    try:
        with _state.pool.connection() as conn:
            return _get_audit_log(conn, group_id, since)
    except psycopg.OperationalError as e:
        return _operational_error(e)


@_tool
def pending_documents(project: str | None = None) -> dict:
    """Memory files this project has written that are not in the graph yet.

    A hook notices a file the moment it changes; turning it into entities and
    facts needs a model, so it waits here for one. Read each path, call
    write_episode with what it states, then mark_ingested to close it.

    Claude Code is told about this queue at session start by a hook. Nothing
    tells any other tool, so this is how they find out at all.
    """
    try:
        with _state.pool.connection() as conn:
            queued = capture.pending(conn, project or _state.config.project)
    except psycopg.OperationalError as e:
        return _operational_error(e)
    return {
        "project": project or _state.config.project,
        "n": len(queued),
        "documents": [
            {"path": q["path"], "project": q["project"], "source": q["source"]}
            for q in queued
        ],
    }


@_tool
def mark_ingested(paths: list[str], session_id: str | None = None) -> dict:
    """Close pending documents once their content is in the graph.

    Pass the same session_id you pass write_episode. Closing a document is one
    of the two valid answers to a capture prompt - the other is writing the
    facts - so without it the gate that asked cannot tell a session that
    complied from one that ignored it.

    The other half of pending_documents, and it exists because the queue could
    not be closed by the tool that had just drained it. On 2026-09-12 Codex
    read a pending note, wrote its facts correctly, and reported: "Its pending
    marker couldn't be cleared because the local echo-memory command wasn't
    available." It was right - marking done was CLI-only, so a tool that could
    do the hard half could not do the trivial one, and the file stayed queued
    for a tool that happened to have a shell.

    Paths that are not in the queue are reported rather than silently ignored,
    because a mistyped path that returns success leaves a document queued
    forever while the caller believes it is done.
    """
    if not paths:
        return {"marked": 0, "not_queued": []}
    try:
        with _state.pool.connection() as conn:
            queued = {q["path"] for q in capture.pending(conn, None)}
            known = [p for p in paths if p in queued]
            marked = capture.mark_ingested(conn, known, session_id)
    except psycopg.OperationalError as e:
        return _operational_error(e)
    return {
        "marked": marked,
        "not_queued": [p for p in paths if p not in queued],
    }


if __name__ == "__main__":
    startup()
    server.run()
