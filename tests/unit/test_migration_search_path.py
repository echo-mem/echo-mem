"""A migration that names an ag_catalog type must put ag_catalog on the path.

This exists because of a failure that no amount of running the suite could
find. `graphid` and `agtype` live in `ag_catalog`, and Alembic's connection
does not put that schema on the search_path, so a migration naming either one
has to `SET` it. 0030 and 0033 did not, and both passed anyway: every test and
every CI run migrates from base, so an earlier migration's `SET` - 0026's for
0030, 0032's for 0033 - was still in force in the same session by the time
they ran.

A real deployment does not migrate from base. It resumes from wherever it is,
in a new session, and then the predecessor never executes. A store sitting at
0027, 0028 or 0029 died at 0030 with `type "graphid" does not exist`, and one
at 0032 died at 0033 the same way. The live store was at 0028, so this was the
upgrade path of the only deployment that mattered.

Checking the text rather than running the migrations is deliberate. Proving it
by execution means a separate database per start revision, 38 of them, to
assert a property that is local to each file; and the property worth holding is
the stronger one - that every migration is self-sufficient - not that the
particular revisions in use today happen to work.
"""

from __future__ import annotations

import pathlib
import re
from importlib import resources

#: Types that only resolve when ag_catalog is on the search_path. Matched
#: without a preceding dot, so an already-qualified `ag_catalog.agtype` does
#: not count - that form needs no setting.
AG_CATALOG_TYPE = re.compile(r"(?<![\w.])(graphid|agtype)(?![\w.])")

SETS_SEARCH_PATH = re.compile(r"SET\s+search_path\s*=\s*ag_catalog")

#: `migrations/` and `migrations/versions/` carry no `__init__.py` - Alembic
#: loads them by path - so neither has a usable `__file__`. The resources API
#: finds them whether this runs from the working tree or an installed wheel.
VERSIONS = pathlib.Path(str(resources.files("echo_memory") / "migrations" / "versions"))


def _upgrade_code(source: str) -> str:
    """The body of `upgrade()` with comments stripped.

    Comments are removed because several migrations discuss `agtype` in prose,
    and a docstring mentioning a type is not a statement using one.
    """
    match = re.search(r"\ndef upgrade\(\).*?(?=\ndef downgrade\(\)|\Z)", source, re.DOTALL)
    body = match.group(0) if match else ""
    return "\n".join(line.split("#")[0] for line in body.splitlines())


def test_every_migration_naming_an_ag_catalog_type_sets_the_search_path():
    offenders = []
    checked = 0
    for path in sorted(VERSIONS.glob("0*.py")):
        code = _upgrade_code(path.read_text())
        used = AG_CATALOG_TYPE.search(code)
        if not used:
            continue
        checked += 1
        sets = SETS_SEARCH_PATH.search(code)
        if not sets or sets.start() > used.start():
            offenders.append(
                f"{path.name}: names {used.group(1)!r} at offset {used.start()} "
                + ("without setting search_path at all" if not sets
                   else f"but sets search_path later, at {sets.start()}")
            )

    # Without this the test passes when the regex stops matching anything,
    # which is the way a guard like this dies quietly.
    assert checked >= 7, f"only {checked} migrations matched; the check has gone blind"
    assert not offenders, "migrations that cannot be applied from a resumed session:\n" + "\n".join(offenders)
