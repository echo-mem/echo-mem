"""The README enumerates the commands that reach a model; the source is the truth.

The central claim is not "no model anywhere", it is that storing a memory never
depends on one. The README states that carefully, and to state it carefully it
NAMES the commands that do reach a provider. An enumeration drifts the moment
one ships: the paragraph named only `infer-causal-hints` while `distil` had
been calling a provider since it shipped, so somebody checking the claim found
a missing member and had every reason to stop trusting the rest of the page
(issue #146).

So this pins the enumeration to the call sites rather than to a reviewer
noticing. Add a third command that calls a model and this test fails until the
README says so, which is the only way the paragraph stays true by construction.

It deliberately checks two separate things, because the claim needs both:

  - how many places call the model at all, and
  - that the module holding the outbound request is still the only one,

since a new provider call that bypassed `cli/llm.py` entirely would leave the
first check passing while making the claim false.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "echo_memory"
README = ROOT / "README.md"

# The module that owns the one outbound request, excluded from the call site
# search because it is where `complete` is defined.
LLM_MODULE = SRC / "cli" / "llm.py"

# The commands the README has to name, and the module whose call site each one
# is reached through. Written out rather than derived: the point is to compare
# the prose against the source, and deriving both from the source would compare
# the source with itself.
MODEL_CALLERS = {
    "infer-causal-hints": SRC / "cli" / "infer_causal.py",
    "distil": SRC / "distil" / "candidates.py",
}


def _python_files() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _calls_complete(path: Path) -> bool:
    """Does this module call a function named `complete`?

    Matches both `complete(...)` and `llm.complete(...)`, so it does not depend
    on how the caller chose to import it.
    """
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "complete":
            return True
        if isinstance(func, ast.Attribute) and func.attr == "complete":
            return True
    return False


def test_the_model_call_sites_are_the_ones_the_readme_names():
    found = {p for p in _python_files() if p != LLM_MODULE and _calls_complete(p)}
    expected = set(MODEL_CALLERS.values())
    new = sorted(str(p.relative_to(ROOT)) for p in found - expected)
    gone = sorted(str(p.relative_to(ROOT)) for p in expected - found)
    assert not new, (
        f"{new} calls a model and the README's 'No inference on the write path' "
        "paragraph does not name it. Name the command there, and add it here."
    )
    assert not gone, (
        f"{gone} no longer calls a model; the README still names its command "
        "as one that does."
    )


def test_the_readme_names_every_command_that_reaches_a_provider():
    paragraph = README.read_text().split("**No inference on the write path.**")
    assert len(paragraph) == 2, "the write path paragraph is gone or duplicated"
    # Up to the next bolded lead-in, so a later mention elsewhere on the page
    # cannot satisfy this.
    claim = paragraph[1].split("**")[0]
    for command in MODEL_CALLERS:
        assert f"`{command}`" in claim, (
            f"{command!r} reaches a model provider and the write path paragraph "
            "does not name it"
        )


# Modules that MAKE a request. Deliberately not the `http` package as a whole:
# `http.server` is an inbound listener, and the dashboard serves itself on
# 127.0.0.1 through it, which is the opposite of what this test is about. Nor
# `urllib` as a whole, since `urllib.parse` is string handling and quickstart
# builds a connection string with it.
OUTBOUND = ("http.client", "requests", "httpx", "urllib.request")


def _imports_outbound(path: Path) -> bool:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        for name in names:
            if any(name == m or name.startswith(m + ".") for m in OUTBOUND):
                return True
    return False


def test_only_one_module_makes_an_outbound_request():
    """A provider call that skipped cli/llm.py would make the claim false."""
    importers = {p for p in _python_files() if _imports_outbound(p)}
    assert importers == {LLM_MODULE}, (
        f"expected cli/llm.py to hold the only outbound request, found "
        f"{sorted(str(p.relative_to(ROOT)) for p in importers)}"
    )
