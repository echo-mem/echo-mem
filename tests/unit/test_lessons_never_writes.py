"""The lesson layer derives at read time and stores nothing, asserted against
the source text rather than against behaviour.

Reading the file is a strange-looking test and it is the right one here. A
behavioural test would have to catch the write it is trying to forbid, which
means knowing in advance what shape the regression takes - and the plausible
regressions do not look like a write. They look like a cache: derive the
lessons once a night and store them as facts so the read path is cheap. That
change passes every test about what `candidates` returns, because it returns
the same rows.

What it would cost is the three properties the module's docstring sets out. A
stored conclusion outlives its evidence, so superseding a supporting outcome
leaves the lesson asserting a pattern the data no longer shows. A stored
conclusion is retrieved, ranked and quoted exactly like an observation, with
nothing marking it as something a machine generalised. And a stored conclusion
becomes an input to the next derivation.

So the test is on the import, where the change would have to show up, and it
fails in the pull request that introduces it rather than in the store a year
later.
"""

from __future__ import annotations

from pathlib import Path

from echo_memory.intelligence import lessons

SOURCE = Path(lessons.__file__)


def test_the_lesson_module_does_not_reference_the_write_path():
    text = SOURCE.read_text()
    assert "write_episode" not in text, (
        f"{SOURCE.name} mentions the write path. A lesson is derived at read "
        f"time and never stored; see the module docstring for what storing one "
        f"costs. If a cache is genuinely needed it does not belong in the graph."
    )


def test_the_lesson_module_writes_no_sql_that_mutates():
    """Not just the write path - any mutation. Going around the write path with
    raw SQL would be worse than using it, because it would skip dedup,
    supersession, embedding, attestation and the audit entry as well."""
    text = SOURCE.read_text().upper()
    for statement in ("INSERT INTO", "UPDATE ", "DELETE FROM", "CREATE ("):
        assert statement not in text, (
            f"{SOURCE.name} contains {statement!r}. This module reads."
        )


def test_the_module_is_importable_without_the_ingestion_stack():
    """The separation has to hold for the program, not only for one file.

    An import of this module that dragged the write path in would make the
    text check above true and the property false: every reader of lessons
    would have the ingestion stack loaded, and the next person to need
    something from it would reach for what was already there.
    """
    import subprocess
    import sys

    probe = (
        "import echo_memory.intelligence.lessons, sys; "
        "print('echo_memory.ingestion.write_episode' in sys.modules)"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "False", (
        "importing lessons pulled in the write path, probably through "
        "echo_memory/intelligence/__init__.py. See the note in that file."
    )
