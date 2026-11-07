"""The README states how many CLI commands exist; the parser is the truth.

A count written in prose drifts the moment a command ships - the README said
"thirty CLI commands" while the parser registered 35, and nothing noticed.
This pins the figure to the parser so the next command has to update the
README, in the spirit of test_licence_claims.py, which pins the licence
claims to the files that carry them.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from echo_memory.cli.main import build_parser

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"


def registered_commands() -> set[str]:
    """The top level command names the CLI actually accepts."""
    parser = build_parser()
    subparsers = [
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    ]
    assert subparsers, "the CLI has no subparsers, so it has no commands"
    return set(subparsers[0].choices)


def readme_command_count() -> int:
    """The command count the README claims, in digits.

    The README has to state the count as a number for this to be checkable;
    if it goes back to spelling it out, the search fails and the test says
    so rather than quietly passing.
    """
    text = " ".join(README.read_text().split())
    match = re.search(r"(\d+)\s+CLI commands", text)
    assert match, "the README no longer states a CLI command count in digits"
    return int(match.group(1))


def test_the_readme_command_count_matches_the_parser():
    commands = registered_commands()
    assert readme_command_count() == len(commands), (
        f"the README claims {readme_command_count()} CLI commands, "
        f"the parser registers {len(commands)}"
    )

#: Commands a person never types, so naming them in a usage block would be
#: misleading rather than complete. Each is invoked by a Claude Code hook with
#: arguments the hook constructs, and `install-hooks` is what registers them.
#:
#: An allowlist rather than a filter on some property of the command, because
#: "is this for a human" is a judgement and a judgement belongs somewhere a
#: reviewer can see it change. Adding a name here is a decision; adding one by
#: accident is not possible.
MACHINE_FACING = {
    "notice",        # PostToolUse: a memory file changed
    "notice-edit",   # PostToolUse: a session edited a file, counts only
    "session-brief", # SessionStart: what memory knows about this project
    "stop-check",    # Stop: what this session still owes the graph
}


def readme_commands() -> set[str]:
    """Every command the README names, anywhere, not only in a usage block."""
    return set(re.findall(r"echo-memory ([a-z][a-z-]+)", README.read_text()))


def test_every_command_a_person_types_is_in_the_readme():
    """The gap this is written for. On 2026-11-07 the README named 17 of 38
    commands. `forget` was among the missing, which is the one a compliance
    review looks for, and `retain` and `verify` had just been added by the
    same hand that was counting them.

    The count has been guarded since the day the roadmap table was written and
    stayed correct throughout, because it reads the number rather than the
    list. A count and a list are different claims, and only one of them tells
    a reader what this thing can do.
    """
    missing = registered_commands() - readme_commands() - MACHINE_FACING

    assert not missing, (
        f"the README names no command called {sorted(missing)}, and they are "
        f"not in MACHINE_FACING. Either document them beside the others, or "
        f"add them to that set with the hook that calls them."
    )


def test_the_allowlist_does_not_outlive_its_commands():
    """A name left in MACHINE_FACING after the command is gone silently
    excuses a future command that happens to take the same name."""
    stale = MACHINE_FACING - registered_commands()

    assert not stale, f"MACHINE_FACING names commands that no longer exist: {sorted(stale)}"


def test_the_readme_names_no_command_that_does_not_exist():
    """The other direction, which is worse: a reader runs it and it fails.
    Nothing has caught this because nothing has looked."""
    invented = readme_commands() - registered_commands()

    assert not invented, (
        f"the README tells a reader to run {sorted(invented)}, which the CLI "
        f"does not accept"
    )
