"""What ships with echo-mem, and under which licence.

Walks the RUNTIME dependency closure from pyproject rather than listing whatever
happens to be installed: a developer environment carries pytest, ruff and their
trees, none of which reach a customer, and an inventory that counts them answers
a question nobody asked while hiding the one they did.

Run it rather than trust the committed copy:

    .venv/bin/python scripts/licence-inventory.py

A licence inventory is a claim about the product, and a claim that is written
once and never recomputed is how this repository ended up telling customers it
was Apache 2.0 a fortnight after it stopped being.
"""

from __future__ import annotations

import pathlib
import re
import sys
import tomllib
from importlib.metadata import distributions

# Reciprocal families an enterprise buyer asks about by name. Split, because
# "copyleft" covering both AGPL and MPL in one word is what makes the answer
# sound worse than it is.
STRONG = re.compile(r"\b(AGPL|GPL-[23]|GPLv[23]|GNU General Public|SSPL)", re.IGNORECASE)
WEAK = re.compile(r"\b(LGPL|MPL|EPL|CDDL|MOZILLA PUBLIC)", re.IGNORECASE)


def runtime_roots() -> list[str]:
    data = tomllib.loads(pathlib.Path("pyproject.toml").read_text())
    names = []
    for spec in data["project"]["dependencies"]:
        names.append(re.split(r"[\[><=!~;\s]", spec, 1)[0])
    return names


# A License field longer than this is the full licence TEXT rather than an
# identifier, and scanning prose for "GPL" is guaranteed to misfire: scipy ships
# 47,596 characters of it including bundled third party notices, one of which is
# "GPL-3.0-or-later WITH GCC-exception-3.1" for a runtime component. Matching
# that reported scipy as strong copyleft when its own licence is BSD-3-Clause.
# A compliance check that cries wolf gets switched off, so it reads the
# classifier instead whenever the field is prose.
MAX_IDENTIFIER_LEN = 200


def licence_of(dist) -> str:
    meta = dist.metadata
    expression = meta.get("License-Expression")
    if expression:
        return expression
    classifiers = "; ".join(
        c.split("::")[-1].strip()
        for c in meta.get_all("Classifier", [])
        if c.startswith("License ::")
    )
    declared = meta.get("License") or ""
    if declared and len(declared) <= MAX_IDENTIFIER_LEN:
        return declared
    return classifiers or (declared[:MAX_IDENTIFIER_LEN] if declared else "UNKNOWN")


def closure(roots: list[str]) -> dict:
    from packaging.requirements import InvalidRequirement, Requirement

    installed = {
        d.metadata["Name"].lower().replace("_", "-"): d
        for d in distributions()
        if d.metadata.get("Name")
    }
    seen: set[str] = set()
    queue = list(roots)
    while queue:
        name = queue.pop().lower().replace("_", "-")
        if name in seen:
            continue
        seen.add(name)
        dist = installed.get(name)
        if dist is None:
            continue
        for raw in dist.requires or []:
            try:
                req = Requirement(raw)
            except InvalidRequirement:
                # A requirement string this version of packaging cannot parse.
                # Skipped rather than fatal: one unparseable line in one
                # dependency must not stop the whole inventory, and the result
                # is visible as a package missing from a list somebody reads.
                continue
            # Skip extras: they are not installed unless asked for, so counting
            # them would inventory software the customer does not receive.
            if req.marker and not req.marker.evaluate({"extra": ""}):
                continue
            queue.append(req.name)
    return {n: installed[n] for n in sorted(seen) if n in installed}


def main() -> int:
    found = closure(runtime_roots())
    strong, weak = [], []
    rows = []
    for name, dist in found.items():
        lic = licence_of(dist)
        rows.append((name, dist.version, lic))
        if STRONG.search(lic):
            strong.append(name)
        elif WEAK.search(lic):
            weak.append(name)

    print(f"{len(rows)} distributions ship with echo-mem\n")
    for name, version, lic in rows:
        flag = ""
        if name in strong:
            flag = "  <-- STRONG copyleft"
        elif name in weak:
            flag = "  <-- file level / weak"
        print(f"  {name:30s} {version:12s} {lic[:46]}{flag}")

    print(f"\nstrong copyleft (GPL, AGPL, SSPL): {strong or 'none'}")
    print(f"weak or file level (LGPL, MPL): {weak or 'none'}")

    # Non-zero only on the family that actually changes what a buyer may do with
    # a proprietary product. Weak copyleft is reported and not failed, because
    # importing an LGPL library is not the thing LGPL restricts.
    if strong:
        print("\nA strong copyleft dependency reaches customers. This needs a "
              "decision before the next release, not after.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
