"""Reading what the harness already wrote down.

The capture problem, restated. Writes have always depended on an agent
choosing to call `write_episode`, and the measurements say it does not: eight
merged PRs produced zero calls, Claude Desktop logged seven reads and zero
writes, and the Stop gate converts about one firing in seven. The recorded
conclusion was that capture has to be structural rather than instructional,
and that conclusion has been true and unimplemented for a while.

Beacon (Asymptote Labs, MIT, 2.3k stars) is the thing that made this urgent.
It captures at the HARNESS level - OTLP, hooks, polling, IDE and browser
extensions across 40-odd runtimes - so nothing depends on an agent deciding
anything. That is the right shape, and this module takes the half of it that
costs nothing: **the transcript is already on disk.** Claude Code writes every
session to ~/.claude/projects/<slug>/<session-id>.jsonl without being asked.
There is no capture to build, only reading to do.

Where this deliberately differs from Beacon: their artifact is a Skill, prose
an agent retrieves. Ours is a fact on a bi-temporal graph, which supersedes,
resolves entities, and can be asked what was true in March. A skills directory
has no answer to a skill that became wrong; `t_invalid` is that answer.

HUMAN TURNS ONLY, which is the whole reason this is cheap. One real session
measured on 2026-10-21: 69,881 lines, 170MB, of which 649 were a person
typing and 7,923 were tool results. Feeding the transcript to a model would be
absurd; feeding 649 short messages is not. It is also where the signal is. The
first human turn sampled from that file reads "from now on do not push
anything directly on main, create a separate branch" - which is, word for
word, a memory the graph already holds because a person later wrote it into a
file by hand.

Nothing here calls a model. This module finds sessions and extracts turns, and
is tested without a provider; `candidates.py` does the part that needs one.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

# Claude Code's transcript root. One directory per project, named by the
# working directory with every separator replaced by a dash.
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"

# Below this, a message is an acknowledgement rather than an instruction:
# "yes", "do it", "go on". They carry no fact and cost tokens to ask about.
MIN_TURN_CHARS = 25

# Above this, a turn is almost always pasted material - a log, a stack trace, a
# file - rather than a person stating something. The statement, if there is
# one, is usually in a much shorter turn nearby. Truncated rather than dropped,
# because the opening line of a paste is often the instruction that frames it.
MAX_TURN_CHARS = 2000


@dataclass(frozen=True)
class Session:
    """One harness session, identified the way the harness identifies it."""

    session_id: str
    project: str
    path: Path
    harness: str = "claude-code"

    @property
    def size_bytes(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0


def project_slug(cwd: str | Path) -> str:
    """The directory name Claude Code derives from a working directory.

    `/Users/a/work/echo-mem` becomes `-Users-a-work-echo-mem`. Derived rather
    than guessed at, and kept in one place: a second copy of this derivation is
    exactly the mistake that made the console read whole accounts as empty.
    """
    return str(Path(cwd).resolve()).replace(os.sep, "-")


def sessions_for(cwd: str | Path, root: Path | None = None) -> list[Session]:
    """Every transcript this harness has written for this project, newest first.

    Returns empty rather than raising when the directory does not exist. A
    machine with no Claude Code history is not an error, it is a machine that
    has not used Claude Code.
    """
    root = root or CLAUDE_PROJECTS
    slug = project_slug(cwd)
    directory = root / slug
    if not directory.is_dir():
        return []

    project = Path(cwd).resolve().name
    found = [
        Session(session_id=p.stem, project=project, path=p)
        for p in directory.glob("*.jsonl")
        if p.is_file()
    ]
    return sorted(found, key=lambda s: s.path.stat().st_mtime, reverse=True)


def _text_of(record: dict) -> str | None:
    """The human's words in one transcript record, or None if there are none.

    A `user` record is not the same thing as a person speaking. Most of them
    are tool results being fed back to the model, which outnumbered real turns
    twelve to one in the session this was built against. They are identified
    structurally, by carrying a tool_result block, rather than by guessing from
    their text.
    """
    if record.get("type") != "user":
        return None

    message = record.get("message") or {}
    content = message.get("content")

    if isinstance(content, str):
        return content.strip() or None

    if not isinstance(content, list):
        return None

    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            # Machine output being returned to the model. Never a statement.
            return None
        if block.get("type") == "text":
            parts.append(block.get("text") or "")

    text = " ".join(p for p in parts if p).strip()
    return text or None


def _is_noise(text: str) -> bool:
    """Turns that cannot carry a fact.

    Slash commands are addressed to the harness rather than to anybody, and
    the system's own injected blocks are not the person speaking even though
    they arrive in a user record.
    """
    if text.startswith("/"):
        return True
    if text.startswith("<") and text.endswith(">"):
        return True
    return "<system-reminder>" in text and len(text) < 400


def human_turns(path: Path, limit: int | None = None) -> list[str]:
    """What the person actually typed, in order.

    Streams the file a line at a time. The transcript measured here was 170MB,
    and reading it whole to keep 0.9% of it would be the kind of avoidable
    memory spike that makes a CLI unusable on the machine it is meant to help.

    A malformed line is skipped rather than fatal. These files are appended to
    by a running process, so the last line can legitimately be half-written
    while the command reads it.
    """
    turns: list[str] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except (ValueError, TypeError):
                    continue
                text = _text_of(record)
                if text is None or len(text) < MIN_TURN_CHARS or _is_noise(text):
                    continue
                turns.append(text[:MAX_TURN_CHARS])
                if limit is not None and len(turns) >= limit:
                    break
    except OSError:
        return []
    return turns
