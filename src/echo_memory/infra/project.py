"""Which project a fact was written from.

Resolved server-side from the process's working directory, never passed in by
the calling agent. Same reasoning as group_id (see the design doc's
Configuration section): a value an agent types is a value an agent types
inconsistently, and "eigen" / "Eigon" / "eigen-backend" across three calls
would make the project dimension worthless for exactly the grouping it exists
to provide.

This works because each session gets its own MCP server process whose cwd is
the project directory - verified against four live processes, three in
work/dugout and one in work/echo-mem. So no per-project registration, env var
or config file is needed for the common case.

ECHO_MEMORY_PROJECT overrides, for callers where cwd means nothing: the direct
Python client embedded in a long-running service (see docs/INTEGRATIONS.md), a
container whose cwd is /app, or a chatbot serving many tenants from one
process."""

import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path

UNKNOWN = "unknown"

# Keep it filesystem- and URL-safe, and stable enough to group on: a project
# name ends up in a group-by, a filter chip and a CLI argument.
_SAFE = re.compile(r"[^a-zA-Z0-9._-]+")


def normalize(name: str) -> str:
    cleaned = _SAFE.sub("-", name.strip()).strip("-.")
    return cleaned[:64] or UNKNOWN


def _git_toplevel(cwd: str) -> str | None:
    """The repo root, so a session started in a subdirectory still reports the
    project rather than the subdirectory it happened to open in."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd, capture_output=True, text=True, timeout=2, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    top = result.stdout.strip()
    return top or None


def detect_project(cwd: str | None = None, env: Mapping[str, str] | None = None) -> str:
    """The repo root's name, else the directory's name, else 'unknown'.

    The git remote is deliberately not consulted: it needs no network here but
    it can disagree with what the operator calls the project (a fork, a rename,
    a monorepo pushed under another org), and the local directory name is what
    they'll type into a filter."""
    env = env if env is not None else os.environ
    override = env.get("ECHO_MEMORY_PROJECT")
    if override:
        return normalize(override)

    try:
        cwd = cwd or os.getcwd()
    except OSError:
        return UNKNOWN

    top = _git_toplevel(cwd)
    name = Path(top or cwd).name
    # Path("/").name is "", and a repo checked out at a filesystem root is not
    # worth guessing about.
    return normalize(name) if name else UNKNOWN


def encoded_segments(name: str) -> list[str]:
    """Split a real directory name the way Claude Code's encoding would.

    The encoding collapses both path separators and dots into dashes, so
    `work/ayushbasral.com` and `work/ayushbasral/com` and `work/ayushbasral-com`
    all encode identically. Splitting a real name on the same characters is how
    a candidate directory is matched back against encoded parts."""
    return [segment for segment in re.split(r"[-.]", name) if segment]


def _walk(base: Path, parts: list[str], budget: list[int]) -> Path | None:
    """Descend by matching encoded parts against directories that actually
    exist, longest match first.

    Reading the filesystem beats guessing where the separators were. An earlier
    version rejoined parts with dashes and tested each split, which could not
    recover a name containing a dot: `ayushbasral.com` encodes to
    `...-ayushbasral-com`, no dash-joined candidate exists on disk, and the
    project silently became "com".

    Longest match first keeps a hyphenated or dotted directory intact when one
    exists; backtracking still finds the nested reading when it does not.
    budget bounds the search, since the input is an untrusted directory name."""
    if not parts:
        return base
    if budget[0] <= 0:
        return None
    try:
        children = [child for child in base.iterdir() if child.is_dir()]
    except OSError:
        return None

    matches = []
    for child in children:
        segments = encoded_segments(child.name)
        if segments and parts[: len(segments)] == segments:
            matches.append((len(segments), child))

    for length, child in sorted(matches, key=lambda pair: -pair[0]):
        budget[0] -= 1
        found = _walk(child, parts[length:], budget)
        if found is not None:
            return found
    return None


def resolve_claude_project_dir(encoded: str) -> Path | None:
    """The real directory behind Claude Code's encoded project-directory name,
    or None if it no longer exists.

    The encoding replaces every path separator with '-', which is ambiguous the
    moment a directory name contains one of its own: .../work/yallahaji-Backend
    and .../work/yallahaji/Backend encode identically. Splitting on dashes
    can't recover either reliably, so resolve against the filesystem instead.
    Parent directories can contain dashes too, so this has to search, not just
    rejoin the tail."""
    parts = [p for p in encoded.lstrip("-").split("-") if p]
    if not parts:
        return None
    return _walk(Path("/"), parts, [4096])


def decode_claude_project_dir(encoded: str) -> str:
    """The project name behind an encoded directory. Falls back to the last
    segment when the directory no longer exists, which is what a deleted
    project leaves behind."""
    resolved = resolve_claude_project_dir(encoded)
    if resolved is not None:
        return normalize(resolved.name)
    parts = [p for p in encoded.lstrip("-").split("-") if p]
    return normalize(parts[-1]) if parts else UNKNOWN
