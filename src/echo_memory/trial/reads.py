"""How often memory was read, and what it cost to inject.

Best-effort throughout. This is instrumentation on the hot path - the
UserPromptSubmit hook runs in a fresh process on every prompt - and a
measurement that can fail a prompt is worse than no measurement."""

from __future__ import annotations

from echo_memory.infra.logging import get_logger

UNATTRIBUTED = "unattributed"

_logger = get_logger("read_event")

HOOK = "hook"
QUERY = "query_memory"
#: The other two read surfaces. They were not recorded anywhere, on either the
#: self hosted or the hosted path, so "which facts has this agent been given"
#: could be answered for query_memory and not for the two tools that also hand
#: facts to an agent. A delivery record with a hole in it is not one.
TRACE = "trace_cause"
AUDIT = "get_audit_log"

#: Every kind this table carries. Named so a reader of the data knows the set
#: without grepping, and so a new surface has somewhere obvious to register.
KINDS = (HOOK, QUERY, TRACE, AUDIT)

# The ratio characters-to-tokens for English prose. Rough on purpose: the point
# is an order of magnitude a reader can act on, not a billing figure, and the
# real number depends on a tokenizer this server deliberately does not carry.
CHARS_PER_TOKEN = 4


def record(
    conn,
    group_id: str,
    kind: str,
    n_facts: int,
    injected_chars: int,
    project: str | None = None,
    session_id: str | None = None,
    agent_id: str | None = None,
    fact_ids: list[str] | None = None,
) -> None:
    """Never raises. A prompt must reach the agent whether or not this works.

    project and session_id are optional because this runs on the prompt path
    against whatever CLI version is installed; an unattributed read is worth
    more than an exception between the user and the agent."""
    try:
        conn.execute(
            """INSERT INTO public.read_event
                   (group_id, kind, n_facts, injected_chars, project, session_id,
                    agent_id, returned_fact_ids)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (group_id, kind, n_facts, injected_chars, project, session_id, agent_id,
             [str(f) for f in fact_ids] if fact_ids else None),
        )
    except Exception as e:  # noqa: BLE001 - see docstring: never blocks a prompt
        _logger.warning("read_event_not_recorded", extra={"error": str(e)})


def unreturned(conn, group_ids: list[str], days: int = 30) -> dict:
    """Active facts that retrieval has not handed to anyone in `days`.

    The question underneath every "forgetting layer" in the agent-memory
    literature is which memories are worth keeping, and it is usually answered
    by policy - a decay curve, a TTL, an eviction rule - chosen before anyone
    has counted. This counts first.

    A fact nothing has returned is not automatically dead. It may be the one
    that matters next week, retrieval may simply be failing to reach it, or it
    may be about a project nobody has touched. So this reports a number and
    proposes nothing: no pruning, no scoring, no decay. It exists so that a
    decision about forgetting can be made against evidence, and so that the
    share of the store that is unreachable in practice stops being invisible.

    Needs `returned_fact_ids`, added by migration 0021, so the window can only
    see reads recorded since. A shorter history reads as a larger unreturned
    share, which is a floor on the estimate rather than a measurement of decay.
    """
    import json as _json

    from echo_memory.infra.db import GRAPH_NAME as GRAPH

    active = {
        str(edge_id)
        for group_id in group_ids
        for (edge_id,) in conn.execute(
            f"""SELECT * FROM cypher('{GRAPH}', $$
                MATCH ()-[e:FACT]->()
                WHERE e.group_id = $g AND e.t_invalid IS NULL
                RETURN id(e) $$, %s) AS (i agtype)""",
            (_json.dumps({"g": group_id}),),
        ).fetchall()
    }
    if not active:
        return {"active": 0, "returned": 0, "unreturned": 0, "days": days}

    seen = {
        row[0]
        for row in conn.execute(
            """SELECT DISTINCT unnest(returned_fact_ids) FROM public.read_event
               WHERE group_id = ANY(%s) AND returned_fact_ids IS NOT NULL
                 AND at > now() - make_interval(days => %s)""",
            (group_ids, days),
        ).fetchall()
    }
    returned = len(active & seen)
    return {
        "active": len(active),
        "returned": returned,
        "unreturned": len(active) - returned,
        "days": days,
    }


def summary(conn, group_ids: list[str], days: int = 7) -> dict:
    """Reads and their cost over a window, next to the saves they produced.

    Saves come from trial_observation rather than being inferred: a read that
    helped is a judgement the agent makes, and this only counts how many times
    it made one against how many chances it had."""
    row = conn.execute(
        """SELECT count(*), coalesce(sum(injected_chars), 0),
                  count(*) FILTER (WHERE n_facts > 0)
           FROM public.read_event
           WHERE group_id = ANY(%s) AND at > now() - make_interval(days => %s)""",
        (group_ids, days),
    ).fetchone()
    reads, chars, with_facts = (row or (0, 0, 0))
    saves = conn.execute(
        """SELECT count(*) FROM public.trial_observation
           WHERE group_id = ANY(%s) AND kind = 'recall_save'
             AND timestamp > now() - make_interval(days => %s)""",
        (group_ids, days),
    ).fetchone()
    # Who did the reading. The whole claim is cross-tool recall, and one tool
    # reading a hundred times looks identical to three tools reading thirty
    # each until this is broken out. Rows written before migration 0012 have no
    # agent_id and are counted under "unattributed" rather than assigned to a
    # likely-looking tool.
    by_agent = {
        (agent or UNATTRIBUTED): count
        for agent, count in conn.execute(
            """SELECT agent_id, count(*) FROM public.read_event
               WHERE group_id = ANY(%s) AND at > now() - make_interval(days => %s)
               GROUP BY agent_id ORDER BY count(*) DESC""",
            (group_ids, days),
        ).fetchall()
    }

    return {
        "days": days,
        "reads": reads or 0,
        "reads_with_facts": with_facts or 0,
        "injected_chars": chars or 0,
        "injected_tokens": (chars or 0) // CHARS_PER_TOKEN,
        "saves": (saves or [0])[0],
        "by_agent": by_agent,
    }


def by_project(conn, group_ids: list[str], days: int = 7) -> list[dict]:
    """Reads per project, so the cost of recall can be read against the repo
    that paid it. This is the question read_event could not answer on the day
    it shipped: every row carried the same group_id, so 12 reads across four
    repos and 12 reads in one were indistinguishable."""
    rows = conn.execute(
        """SELECT coalesce(project, 'unattributed'), count(*),
                  count(*) FILTER (WHERE n_facts > 0),
                  coalesce(sum(injected_chars), 0),
                  count(DISTINCT session_id)
           FROM public.read_event
           WHERE group_id = ANY(%s) AND at > now() - make_interval(days => %s)
           GROUP BY 1 ORDER BY 2 DESC""",
        (group_ids, days),
    ).fetchall()
    return [
        {
            "project": r[0],
            "reads": r[1],
            "reads_with_facts": r[2],
            "injected_chars": r[3],
            "injected_tokens": r[3] // CHARS_PER_TOKEN,
            "sessions": r[4],
        }
        for r in rows
    ]


# How strongly a claimed recall is corroborated by the read log.
DELIVERED_TO_AGENT = "agent"
DELIVERED_TO_GROUP = "group"


def delivered(conn, group_id: str, fact_id: str, agent_id: str | None) -> str | None:
    """Whether a read in this scope ever returned this fact, and to whom.

    Three answers, and the middle one is the point:

      "agent"  a read BY THIS TOOL returned it. The claim is corroborated.
      "group"  some read in this scope returned it, by a tool the log did not
               record. Weaker, and the honest answer while most reads are
               unattributed.
      None     no read ever returned it. The caller is citing a fact it was
               never given, which is the case worth refusing.

    Best-effort like everything else here: a store mid-migration answers None
    for every fact, so the caller treats an unavailable log as no evidence
    rather than as proof of abuse. See record_recall_save.
    """
    try:
        rows = conn.execute(
            """SELECT coalesce(agent_id, '') FROM public.read_event
               WHERE group_id = %s AND returned_fact_ids @> ARRAY[%s]::text[]""",
            (group_id, str(fact_id)),
        ).fetchall()
    except Exception as e:  # noqa: BLE001
        _logger.warning("delivery_lookup_failed", extra={"error": str(e)})
        return None
    if not rows:
        return None
    if agent_id and any(r[0] == agent_id for r in rows):
        return DELIVERED_TO_AGENT
    return DELIVERED_TO_GROUP


def has_delivery_log(conn, group_id: str) -> bool:
    """Whether this scope has ever recorded which facts a read returned.

    The guard on refusing. Before migration 0021 no read stored its fact ids,
    so `delivered` answers None for every fact in a store that has not read
    anything since - and refusing on that would reject every honest save on
    every existing install the moment it upgraded. A check that fires hardest
    on the people who have been using the thing longest is not a check.

    Once one read has been logged with its ids, absence of evidence for a
    particular fact starts to mean something.
    """
    try:
        row = conn.execute(
            """SELECT 1 FROM public.read_event
               WHERE group_id = %s AND returned_fact_ids IS NOT NULL LIMIT 1""",
            (group_id,),
        ).fetchone()
    except Exception as e:  # noqa: BLE001
        _logger.warning("delivery_log_check_failed", extra={"error": str(e)})
        return False
    return row is not None
