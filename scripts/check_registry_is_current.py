"""Fail when the MCP registry is serving an older release than PyPI.

The registry is how somebody looking for an MCP memory server finds this one,
which makes it the highest intent audience the project has. On 2026-10-26 it
was serving **0.5.3** while PyPI was on **0.5.5**, so everyone arriving that
way was pointed at a release that predated entity scoped reads.

Nothing had gone wrong. `server.json` already said 0.5.5 and the manifest was
correct; the publish is a manual step in `docs/PUBLISHING.md` and it simply
had not been run. A step that is remembered or not remembered is a step that
will eventually not be remembered, and there was nothing anywhere to say so.

This is the smallest thing that turns silence into noise. It does not publish
and it cannot: the namespace `io.github.ayushcodes10/*` is tied to a personal
GitHub account, so an Actions token cannot claim it. What it can do is refuse
to stay quiet.

Three versions, and all three have to agree:

    server.json   what this repository intends to publish
    PyPI          what somebody gets from `pip install echo-mem`
    registry      what an MCP client is told to install

A mismatch between the first two means a release was tagged without updating
the manifest. A mismatch between the last two means the manifest was updated
and never published. They are different mistakes and the output says which.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = "https://registry.modelcontextprotocol.io/v0/servers?search=echo-mem"
PYPI = "https://pypi.org/pypi/echo-mem/json"
SERVER_NAME = "io.github.ayushcodes10/echo-mem"


def _get(url: str, what: str) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.loads(r.read().decode())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        # Loud, and distinct from "they disagree". A network failure that
        # reads as a version mismatch sends somebody to publish a release
        # that is already published.
        raise SystemExit(f"could not reach {what}: {e}") from e


def registry_latest() -> str | None:
    """The version the registry marks isLatest, or None if nothing is listed."""
    for entry in _get(REGISTRY, "the MCP registry").get("servers", []):
        server = entry.get("server", entry)
        if server.get("name") != SERVER_NAME:
            continue
        meta = entry.get("_meta", {}).get(
            "io.modelcontextprotocol.registry/official", {}
        )
        if meta.get("isLatest"):
            return server.get("version")
    return None


def main() -> int:
    intended = json.loads((ROOT / "server.json").read_text())["version"]
    published = _get(PYPI, "PyPI")["info"]["version"]
    listed = registry_latest()

    print(f"server.json {intended} | PyPI {published} | registry {listed}")

    problems = []
    if intended != published:
        problems.append(
            f"server.json says {intended} and PyPI has {published}. A release "
            f"was published without updating the manifest, or the manifest was "
            f"bumped ahead of a release."
        )
    if listed is None:
        problems.append(
            f"the registry lists no version of {SERVER_NAME} at all."
        )
    elif listed != published:
        problems.append(
            f"the registry serves {listed} and PyPI has {published}. The "
            f"manifest was never published. Run `mcp-publisher login github` "
            f"then `mcp-publisher publish` from the repository root. See "
            f"docs/PUBLISHING.md."
        )

    if problems:
        print(file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    print("the registry, PyPI and server.json agree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
