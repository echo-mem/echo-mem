"""echo-memory export --out <path>: markdown export of a scope's memory
(see the CEO plan's scope decision #5). One file per node with its
resolved aliases and active outgoing edges, plus a top-level index."""

import json
import re
from pathlib import Path

from echo_memory.infra.db import GRAPH_NAME as GRAPH
from echo_memory.infra.db import agtype_str as _agtype_str


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "node"


def _fetch_nodes(conn, group_id: str) -> list[dict]:
    rows = conn.execute(
        f"""SELECT * FROM cypher('{GRAPH}', $$
            MATCH (n:Node {{group_id: $gid}})
            RETURN id(n), n.name, n.type, n.aliases
        $$, %s) AS (id agtype, name agtype, type agtype, aliases agtype)""",
        (json.dumps({"gid": group_id}),),
    ).fetchall()
    return [
        {
            "id": str(node_id),
            "name": _agtype_str(name),
            "type": _agtype_str(type_),
            "aliases": json.loads(str(aliases)) if aliases is not None else [],
        }
        for node_id, name, type_, aliases in rows
    ]


def _fetch_active_edges(conn, group_id: str) -> list[dict]:
    rows = conn.execute(
        f"""SELECT * FROM cypher('{GRAPH}', $$
            MATCH (a)-[e:FACT {{group_id: $gid}}]->(b)
            WHERE e.t_invalid IS NULL
            RETURN id(a), e.relation_type, e.fact, e.confidence
        $$, %s) AS (source_id agtype, relation_type agtype, fact agtype,
                     confidence agtype)""",
        (json.dumps({"gid": group_id}),),
    ).fetchall()
    return [
        {
            "source_id": str(source_id),
            "relation_type": _agtype_str(relation_type),
            "fact": _agtype_str(fact),
            "confidence": _agtype_str(confidence),
        }
        for source_id, relation_type, fact, confidence in rows
    ]


def _render_node_markdown(node: dict, edges: list[dict]) -> str:
    lines = [f"# {node['name']}", "", f"**Type:** {node['type']}"]
    if node["aliases"]:
        lines += ["", "**Aliases:** " + ", ".join(node["aliases"])]
    lines += ["", "## Facts", ""]
    if not edges:
        lines.append("_(no active facts)_")
    else:
        lines += [f"- {e['relation_type']}: {e['fact']} ({e['confidence']})" for e in edges]
    return "\n".join(lines) + "\n"


def _render_index_markdown(group_id: str, nodes: list[dict], filenames: dict[str, str]) -> str:
    lines = [f"# Echo Memory export: {group_id}", "", "## Nodes", ""]
    for node in sorted(nodes, key=lambda n: n["name"].lower()):
        lines.append(f"- [{node['name']}]({filenames[node['id']]})")
    return "\n".join(lines) + "\n"


def _assign_filenames(nodes: list[dict]) -> dict[str, str]:
    """Slugified filenames, disambiguated on collision (two nodes named the
    same thing is rare but not impossible: distinct nodes, same surface
    text, e.g. before entity resolution catches it)."""
    filenames = {}
    seen: dict[str, int] = {}
    for node in nodes:
        slug = _slugify(node["name"])
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        filenames[node["id"]] = f"{slug}.md" if count == 0 else f"{slug}-{count}.md"
    return filenames


def export_group(conn, group_id: str, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    nodes = _fetch_nodes(conn, group_id)
    edges = _fetch_active_edges(conn, group_id)

    edges_by_source: dict[str, list[dict]] = {}
    for e in edges:
        edges_by_source.setdefault(e["source_id"], []).append(e)

    filenames = _assign_filenames(nodes)
    for node in nodes:
        content = _render_node_markdown(node, edges_by_source.get(node["id"], []))
        (out_dir / filenames[node["id"]]).write_text(content)

    (out_dir / "index.md").write_text(_render_index_markdown(group_id, nodes, filenames))

    return {"n_nodes": len(nodes), "n_facts": len(edges), "out_dir": str(out_dir)}
