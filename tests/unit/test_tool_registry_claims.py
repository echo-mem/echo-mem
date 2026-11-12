"""Every count and every list of MCP tools in prose, against the live registry.

The registry rather than the source, deliberately, and for the same reason
tests/integration/test_erasure.py reads it for the forbidden-name guard: what
a client is offered is what `server.tool()` actually registered, not what a
`def` in server.py suggests it registered. A tool can be defined and never
decorated, decorated behind a condition, registered under a different name, or
removed from the module while a doc still describes it. Only one of those is
visible by reading the file, and none of them is visible by counting `def`s.

What this is written for. On 2026-11-11 the website's public/llms.txt said:

    ## MCP tools

    Four, over stdio:

against a registry returning seven, and listed four. trace_cause,
pending_documents and mark_ingested had never been added to it. The website's
own CI did check a tool count - it compared public/docs/integration.md with
app/docs/page.tsx, found both saying five, and passed. Both were right. Both
described the HOSTED endpoint, which does register five. llms.txt described
the stdio server and was not one of the two files compared, so the claim that
was wrong was the one nothing looked at.

Hence the second test below, which is the part that matters more than any
individual count: a count in prose that this file has no entry for is a
FAILURE. Adding a sentence that states a number and forgetting to guard it is
exactly how the last one survived three releases, and "we did not think to
look there" is not something a passing test should be able to mean.
"""

from __future__ import annotations

import re
from pathlib import Path

from echo_memory import server
from echo_memory.client import EchoMemory

ROOT = Path(__file__).resolve().parents[2]

WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven",
         "eight", "nine", "ten", "eleven", "twelve"]


def registered_tools() -> set[str]:
    """The tool names the MCP server actually publishes to a client."""
    published = {t.name for t in server.server._tool_manager.list_tools()}
    assert published, "the server published no tools, so this proves nothing"
    return published


def client_methods() -> set[str]:
    """The in-process Python API's public methods.

    A different set from the MCP tools on purpose: `EchoMemory` is four
    methods and the server is seven. docs/INTEGRATIONS.md names the four in a
    parenthetical introduced by "the same engine the MCP server runs", which
    is close enough to a count of MCP tools that it is worth pinning to the
    thing it actually describes.
    """
    return {n for n in dir(EchoMemory) if not n.startswith("_")}


def word(n: int) -> str:
    return WORDS[n] if n < len(WORDS) else str(n)


#: Every place this project's rendered text states how many MCP tools exist,
#: with the pattern that extracts the figure.
#:
#: A missing pattern is a failure rather than a skip. If somebody rewords one
#: of these sentences, the count stops being checked, and a guard that quietly
#: stops checking is worse than no guard: it reports green.
COUNT_CLAIMS = (
    ("README.md", re.compile(r"^### The (\w+) MCP tools$", re.MULTILINE)),
    ("README.md", re.compile(r"\| \*\*v1a, built\*\*.*?(\w+) MCP tools", re.DOTALL)),
    ("docs/INTELLIGENCE.md", re.compile(r"The (\w+) MCP tools are unchanged")),
    # Added when the capability table landed on a separate branch from this
    # guard and the two met at the merge. That is the guard working: the claim
    # arrived unchecked and the suite said so before anybody published it.
    ("README.md", re.compile(r"registers\s+exactly (\w+) MCP tools")),
)

#: Files whose prose may state a tool count, which is every page this project
#: publishes about itself. Kept in step with
#: tests/unit/test_licence_claims.py's sells_the_licence() by hand, because the
#: exclusions there are argued one by one and copying the list would hide them.
SCANNED = (
    "README.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "src/echo_memory/skill/SKILL.md",
    *sorted(str(p.relative_to(ROOT)) for p in (ROOT / "docs").glob("*.md")),
)


#: Phrases that match the pattern and are not counts of MCP tools.
#:
#: Matched on the LINE's text rather than its number, so the exemption
#: survives the file being edited above it and dies if the sentence itself
#: changes - which is the right way round. A reworded sentence should be looked
#: at again rather than inheriting a decision made about different words.
#:
#: Each entry carries why. This list staying short is the point: it is the
#: record of every place somebody decided a number did not need checking, and
#: the cloud guard pins the length of its one exemption for the same reason.
NOT_A_TOOL_COUNT = (
    (
        "README.md",
        "two tools claiming the same id",
        (
            "'tools' here means MCP clients - Cursor, Claude Desktop - sharing "
            "an ECHO_MEMORY_AGENT_ID. src/echo_memory/cli/connect.py uses the "
            "word the same way."
        ),
    ),
)


def test_every_count_claim_matches_the_live_registry():
    n = len(registered_tools())

    for name, pattern in COUNT_CLAIMS:
        text = (ROOT / name).read_text()
        m = pattern.search(text)
        assert m, (
            f"{name} no longer states a tool count matching {pattern.pattern!r}. "
            f"If the sentence was reworded, update the pattern in COUNT_CLAIMS "
            f"so the count stays checked. A guard that silently stops looking "
            f"is the failure mode this file was written for."
        )
        claimed = m.group(1).lower()
        assert claimed in (word(n), str(n)), (
            f"{name} says {claimed!r} MCP tools; the server registers {n} "
            f"({word(n)}): {', '.join(sorted(registered_tools()))}. Read from "
            f"server.server._tool_manager.list_tools(), not from counting "
            f"decorators."
        )


def test_no_tool_count_in_prose_is_left_unguarded():
    """The completeness half, and the reason this file is not just the test
    above.

    Every count the test above checks is a count somebody thought to list.
    public/llms.txt said four for the whole life of trace_cause because it was
    not on anybody's list, and the website CI's tool-count check compared two
    files that both happened to be right.

    So: find every count of tools in every page, and fail on one no entry
    covers. The fix for a failure here is to add the entry, which takes a line
    and makes the claim permanent.
    """
    n = len(registered_tools())
    guarded = set()
    for name, pattern in COUNT_CLAIMS:
        text = (ROOT / name).read_text()
        m = pattern.search(text)
        if m:
            # The line the figure sits on, so an entry and a find agree.
            guarded.add((name, text[: m.start(1)].count("\n") + 1))

    # "31 tool calls" is a count of invocations, not of tools, and that usage
    # will recur. Excluded in the pattern rather than by exemption, because
    # the distinction is structural and not a judgement about one sentence.
    any_count = re.compile(
        r"\b(" + "|".join(WORDS[2:]) + r"|\d+)\s+(?:MCP\s+)?tools?\b"
        r"(?!\s+calls?\b)", re.IGNORECASE)

    unguarded = []
    for name in SCANNED:
        path = ROOT / name
        if not path.exists():
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            for m in any_count.finditer(line):
                if (name, lineno) in guarded:
                    continue
                if any(f == name and s in line for f, s, _ in NOT_A_TOOL_COUNT):
                    continue
                unguarded.append((name, lineno, m.group(0).strip()))

    assert not unguarded, (
        "these state a number of tools and nothing checks it:\n"
        + "\n".join(f"  {f}:{n}  {t!r}" for f, n, t in unguarded)
        + f"\n\nThe server registers {n}. Add each to COUNT_CLAIMS in "
        f"{Path(__file__).name} with a pattern that extracts the figure, or "
        f"reword the sentence so it does not state one. Note that in "
        f"src/echo_memory/cli/ the phrase \"4 tools\" means four MCP CLIENTS, "
        f"not four MCP tools; no page in SCANNED uses it that way, and one "
        f"that starts to needs a deliberate exemption here rather than a "
        f"silent pass."
    )


def test_the_readme_table_lists_exactly_the_registered_tools():
    """A count can be right while the list is wrong.

    The website had both failures at once in one file: "Four, over stdio"
    above a list of four, from a registry of seven. Fixing only the number
    would have left three tools undocumented and the page still passing a
    count check.
    """
    text = (ROOT / "README.md").read_text()
    section = text[text.index("### The seven MCP tools") :]
    section = section[: section.index("\n## ")]

    listed = set(re.findall(r"^\| `(\w+)` \|", section, re.MULTILINE))
    published = registered_tools()

    assert listed == published, (
        "README.md's MCP tool table does not match the registry.\n"
        f"  registered, never documented: {sorted(published - listed) or 'none'}\n"
        f"  documented, not registered:   {sorted(listed - published) or 'none'}\n"
        "A tool in the second list is worse than one in the first: a reader "
        "writes a call against it and gets a protocol error."
    )


def test_the_integrations_page_lists_exactly_the_client_methods():
    """docs/INTEGRATIONS.md introduces the in-process client as "the same
    engine the MCP server runs" and then names four calls. Four is right for
    `EchoMemory` and wrong for the server, so the sentence is only correct as
    long as those two sets stay different in exactly the way they are.

    Pinned to the client, which is what the parenthetical actually enumerates.
    A method added to `EchoMemory` fails here rather than leaving a reader to
    discover it by reading the source.
    """
    text = (ROOT / "docs/INTEGRATIONS.md").read_text()
    start = text.index("same engine the MCP server runs")
    listed = set(re.findall(r"`(\w+)`", text[start : start + 400]))
    methods = client_methods()

    assert listed >= methods, (
        "docs/INTEGRATIONS.md introduces echo_memory.client.EchoMemory and "
        f"names {sorted(listed)}, but the class exposes {sorted(methods)}. "
        f"Never named: {sorted(methods - listed)}. Somebody told to use this "
        "class instead of MCP cannot find a method the page does not mention."
    )
