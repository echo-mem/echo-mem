"""The seven tools, their names and their parameters, pinned.

A customer's client is configured against this surface and not against this
repository. dugout has two memories on unchanged keys and calls `write_episode`
and `query_memory` with no parameter this file does not list; a renamed
parameter or a seventh-becomes-eighth tool is a break they would discover at
runtime, in a session, with no error that names the cause.

The intelligence platform added `across`, `trust`, `events`, `intelligence` and
`exchange` to the engine without touching `server.py`, which is the property
worth asserting rather than assuming. The list below was taken from v0.5.7 -
the release a customer is actually running - and from this branch, and the two
were identical. That is the baseline this file holds.

Why a literal list rather than a derivation: a test that builds its expectation
from the same `list_tools()` call it checks compares the source with itself and
passes through any rename. The duplication is the mechanism.
"""

from __future__ import annotations

import re
from pathlib import Path

from echo_memory import server

README = Path(__file__).resolve().parents[2] / "README.md"

#: Tool name -> (every parameter in order, the required subset in order).
#:
#: Order matters for the parameter list because the MCP schema preserves it and
#: a client that positions arguments reads it. It does not matter to a
#: well-behaved client, which names them; it matters to the ones that do not.
SURFACE = {
    "write_episode": (
        ["scope", "session_id", "entities", "facts", "entity_resolutions", "assume_new"],
        ["scope", "session_id", "entities", "facts"],
    ),
    "query_memory": (
        ["scope", "query", "top_k", "digest", "as_of", "about"],
        ["scope"],
    ),
    "trace_cause": (
        ["scope", "subject", "direction", "max_hops"],
        ["scope", "subject"],
    ),
    "record_recall_save": (
        ["scope", "fact_id", "note"],
        ["scope", "fact_id", "note"],
    ),
    "get_audit_log": (["scope", "since"], ["scope"]),
    "pending_documents": (["project"], []),
    "mark_ingested": (["paths", "session_id"], ["paths"]),
}


def _tools():
    return {t.name: (t.parameters or {}) for t in server.server._tool_manager.list_tools()}


def test_the_engine_registers_exactly_these_seven_tools():
    assert sorted(_tools()) == sorted(SURFACE)


def test_the_readme_tool_count_matches_the_registry():
    """The README tells a customer the tool surface did not move. A sentence
    saying so is sales copy until something checks it - this project has
    already shipped a README claiming thirty commands against a parser with
    35, and licence copy that was eight days stale. Same mechanism as
    test_readme_claims.py: the prose states the number in digits, and the
    registry is the truth.
    """
    text = " ".join(README.read_text(encoding="utf-8").split())
    match = re.search(r"(\d+)\s+MCP tools", text)
    assert match, "the README no longer states an MCP tool count in digits"
    assert int(match.group(1)) == len(_tools())


def test_no_tool_gained_or_lost_a_parameter():
    """Separate from the count, because the failure is different. A new tool is
    additive and a client ignores it; a new REQUIRED parameter on an existing
    tool refuses every call that worked yesterday."""
    actual = {
        name: (
            list(schema.get("properties") or {}),
            list(schema.get("required") or []),
        )
        for name, schema in _tools().items()
    }
    assert actual == {k: (list(v[0]), list(v[1])) for k, v in SURFACE.items()}


def test_adding_a_module_did_not_pull_a_new_import_into_the_server():
    """`server.py` is what a customer's client talks to, and none of the six
    new capabilities belongs on it. Asserted against the module's own imports
    rather than against behaviour: a lazy import inside a tool would not show
    up in a round trip test until the flag that reaches it is on."""
    import ast
    from pathlib import Path

    source = Path(server.__file__).read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)

    # The capabilities that must stay off the hosted tool surface. `trust` and
    # `intelligence` reach a read through query_memory's two lazy imports,
    # behind flags that default off - which is query_memory's business and not
    # this module's.
    forbidden = {
        "echo_memory.retrieval.across",
        "echo_memory.retrieval.context",
        "echo_memory.retrieval.scratchpad",
        "echo_memory.trust",
        "echo_memory.events",
        "echo_memory.intelligence",
        "echo_memory.exchange",
    }
    reached = {m for m in imported for f in forbidden if m == f or m.startswith(f + ".")}
    assert not reached, f"server.py imports a capability it does not expose: {reached}"
