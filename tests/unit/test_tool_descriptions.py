"""Tool descriptions have to survive the client that reads them.

Claude Code truncates an MCP tool description at 2048 characters and logs it
as a debug line nobody reads. On 2026-09-03 that log revealed write_episode's
description was 2252 characters, so every session saw it cut mid-word:

    ...entities=[{"name": "Postgres", "type": "tool"}, {"name": "Decisi

The whole `facts=[...]` argument - source, target, relation_type, fact,
confidence - was never shown. The one worked example for the tool stopped
before demonstrating half the call, in the tool whose adoption is the entire
problem this project exists to solve.

Length is therefore a correctness property here, not style. These tests read
the descriptions the server actually publishes, so they measure what a client
receives rather than what the source looks like."""


import pytest

from echo_memory import server

# Claude Code's limit. Other clients differ; this is the tightest one in use,
# so it is the one worth holding to.
MAX_DESCRIPTION = 2048

# Enough headroom that an ordinary edit cannot silently cross the line - the
# failure is invisible at runtime, so the margin is the early warning.
HEADROOM = 100


def _tools():
    return {t.name: (t.description or "") for t in server.server._tool_manager.list_tools()}


def test_every_tool_description_fits_the_client_limit():
    too_long = {n: len(d) for n, d in _tools().items() if len(d) > MAX_DESCRIPTION}
    assert not too_long, f"truncated by Claude Code at {MAX_DESCRIPTION}: {too_long}"


def test_descriptions_keep_headroom_below_the_limit():
    """Not the same test. Crossing the limit is silent - a debug log line and
    a cut string - so the margin has to fail before a real edit does."""
    tight = {
        n: len(d) for n, d in _tools().items() if len(d) > MAX_DESCRIPTION - HEADROOM
    }
    assert not tight, f"within {HEADROOM} chars of the {MAX_DESCRIPTION} limit: {tight}"


@pytest.mark.parametrize("tool", ["write_episode"])
def test_the_worked_example_survives_truncation(tool):
    """The example is the part a model copies. It has to be inside the window
    AND syntactically whole - a cut example is worse than none, because it
    reads as authoritative."""
    description = _tools()[tool]
    visible = description[:MAX_DESCRIPTION]
    example = visible[visible.index("write_episode(") :]
    assert example.count("(") == example.count(")"), "example call is unbalanced"
    assert example.count("[") == example.count("]"), "example arrays are unbalanced"
    assert example.count("{") == example.count("}"), "example objects are unbalanced"


def test_the_example_shows_every_required_argument():
    """The 2026-09-03 truncation cut `facts` entirely, so the only example
    demonstrated entities and nothing else."""
    visible = _tools()["write_episode"][:MAX_DESCRIPTION]
    for argument in ("scope=", "session_id=", "entities=", "facts="):
        assert argument in visible, f"{argument} missing from the visible example"
    for key in ("source", "target", "relation_type", "fact", "confidence"):
        assert f'"{key}"' in visible, f"fact key {key!r} never shown"


def test_the_confidence_enum_is_stated_in_full():
    """The one field the server rejects outright. All three values have to be
    inside the window or a model guesses, and a guess is a failed write."""
    visible = _tools()["write_episode"][:MAX_DESCRIPTION]
    for value in ("extracted", "inferred", "ambiguous"):
        assert value in visible


def test_descriptions_are_published_without_source_indentation():
    """The SDK publishes `fn.__doc__` verbatim, so a docstring nested inside a
    function arrives with four spaces on every line. On write_episode that was
    140 characters - 7% of the budget above - spent on whitespace, and it is
    also just worse to read. `_tool` dedents; this is what stops the next tool
    from being registered with the raw decorator again."""
    indented = {
        name: description
        for name, description in _tools().items()
        if any(line.startswith(" ") for line in description.splitlines()[:2])
    }
    assert not indented, f"published with source indentation: {sorted(indented)}"
