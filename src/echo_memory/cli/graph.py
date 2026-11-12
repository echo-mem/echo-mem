"""echo-memory graph: a terminal view of a scope's memory graph (nodes and
active facts), optionally live-refreshing. Not part of the CEO plan's
accepted scope items; an observability aid for watching real usage land
after the invocation-mechanism trial began. Read-only, same connection and
scope resolution as the rest of the CLI."""

import json
from datetime import UTC, datetime

from echo_memory.infra.db import GRAPH_NAME as GRAPH
from echo_memory.infra.db import agtype_str as _agtype_str


def _fetch_nodes(conn, group_id: str) -> list[dict]:
    rows = conn.execute(
        f"""SELECT * FROM cypher('{GRAPH}', $$
            MATCH (n:Node {{group_id: $gid}})
            RETURN id(n), n.name, n.type
        $$, %s) AS (id agtype, name agtype, type agtype)""",
        (json.dumps({"gid": group_id}),),
    ).fetchall()
    return [
        {"id": str(node_id), "name": _agtype_str(name), "type": _agtype_str(type_)}
        for node_id, name, type_ in rows
    ]


def _fetch_active_facts(conn, group_id: str) -> list[dict]:
    rows = conn.execute(
        f"""SELECT * FROM cypher('{GRAPH}', $$
            MATCH (a)-[e:FACT {{group_id: $gid}}]->(b)
            WHERE e.t_invalid IS NULL
            RETURN id(a), a.name, id(b), b.name, e.relation_type, e.fact,
                   e.confidence, e.t_valid
            ORDER BY e.t_valid DESC
        $$, %s) AS (source_id agtype, source_name agtype, target_id agtype,
                     target_name agtype, relation_type agtype, fact agtype,
                     confidence agtype, t_valid agtype)""",
        (json.dumps({"gid": group_id}),),
    ).fetchall()
    return [
        {
            "source_id": str(source_id),
            "source_name": _agtype_str(source_name),
            "target_id": str(target_id),
            "target_name": _agtype_str(target_name),
            "relation_type": _agtype_str(relation_type),
            "fact": _agtype_str(fact),
            "confidence": _agtype_str(confidence),
            "t_valid": int(str(t_valid)),
        }
        for source_id, source_name, target_id, target_name, relation_type, fact,
        confidence, t_valid in rows
    ]


def fetch_graph(conn, group_id: str) -> dict:
    return {
        "nodes": _fetch_nodes(conn, group_id),
        "facts": _fetch_active_facts(conn, group_id),
    }


def render_graph(scope: str, group_id: str, graph: dict) -> str:
    nodes, facts = graph["nodes"], graph["facts"]
    lines = [f"Echo Memory - {scope} ({group_id})", f"{len(nodes)} nodes, {len(facts)} active facts", ""]

    if not facts and not nodes:
        lines.append("(no memories recorded yet)")
        return "\n".join(lines) + "\n"

    connected_ids = {f["source_id"] for f in facts} | {f["target_id"] for f in facts}

    for f in facts:
        ts = datetime.fromtimestamp(f["t_valid"], tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
        lines.append(f"{f['source_name']} --[{f['relation_type']}]--> {f['target_name']}")
        lines.append(f'    "{f["fact"]}"  ({f["confidence"]}, {ts})')

    orphans = [n for n in nodes if n["id"] not in connected_ids]
    if orphans:
        lines += ["", "Nodes with no active facts:"]
        lines += [f"  - {n['name']} ({n['type']})" for n in sorted(orphans, key=lambda n: n["name"].lower())]

    return "\n".join(lines) + "\n"
