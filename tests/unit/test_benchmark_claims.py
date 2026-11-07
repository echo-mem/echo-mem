"""The README's benchmark figures have to be the ones that were measured.

The README is the page somebody reads before deciding whether to try this, and
every number on it is a claim about a run that happened. Those claims have gone
stale twice. The hosted price sat at $99 for weeks after it moved to $149,
which is now guarded; and the LoCoMo row sat at the 2026-09-26 figures for
eight weeks after a better run on 2026-10-27, so the front page **understated**
this project's own retrieval by four points of recall.

Understating is the more interesting direction, because nobody reports it. A
number that flatters gets caught by whoever it embarrasses; a number that
undersells is simply believed.

So this asserts **agreement** rather than presence, per the lesson in
test_licence_claims.py: a guard that checks the right figure appears somewhere
cannot see the stale one sixty lines away. There is exactly one source for
these numbers, the anchored table in docs/BENCHMARKS.md, and both the README
and this test read it.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"
BENCHMARKS = ROOT / "docs/BENCHMARKS.md"

#: Marks which recorded run the README quotes. In the document rather than in a
#: fixture beside it, because a fixture is a second copy of the numbers and the
#: whole failure being guarded here is two copies disagreeing. Updating the
#: README's figures means moving this comment to a newer run's table.
ANCHOR = "<!-- locomo-baseline"

#: Column name to its position in the BENCHMARKS table, after the leading
#: category and n. Named rather than indexed blindly: a column inserted into
#: that table would otherwise shift every number silently.
COLUMNS = ("recall@1", "recall@10", "recall@30", "hit@10", "session@10", "MRR")


def measured() -> dict[str, float]:
    """The overall row of the anchored LoCoMo run, as numbers."""
    text = BENCHMARKS.read_text()
    assert ANCHOR in text, (
        f"{ANCHOR} is gone from docs/BENCHMARKS.md, so nothing says which run "
        f"the README quotes. Put it back above the table the README's figures "
        f"come from."
    )
    after = text.split(ANCHOR, 1)[1]
    row = re.search(r"^\|\s*\*\*overall\*\*\s*\|(.+)$", after, re.MULTILINE)
    assert row, "no **overall** row follows the anchor"

    cells = [c.strip().strip("*") for c in row.group(1).split("|") if c.strip()]
    # The first cell is n, formatted with a thousands separator.
    values = cells[1:]
    assert len(values) == len(COLUMNS), (
        f"the anchored table has {len(values)} measure columns, this test "
        f"expects {len(COLUMNS)}: {COLUMNS}. A column was added or removed, "
        f"which moves every number."
    )
    return dict(zip(COLUMNS, (float(v) for v in values), strict=True))


def claimed() -> dict[str, float]:
    """The four figures the README publishes for LoCoMo."""
    row = re.search(r"^\|\s*LoCoMo retrieval[^|]*\|(.+?)\|", README.read_text(),
                    re.MULTILINE)
    assert row, "the README no longer has a LoCoMo retrieval row"

    out = {}
    for name in ("recall@10", "hit@10", "MRR", "session@10"):
        found = re.search(rf"{re.escape(name)}\s+\*{{0,2}}([0-9.]+)", row.group(1))
        assert found, f"the README's LoCoMo row does not state {name}"
        out[name] = float(found.group(1))
    return out


def test_the_readme_quotes_the_run_it_points_at():
    """The bug this file exists for. On 2026-11-07 the README said recall@10
    0.601, hit@10 0.658, MRR 0.460, session@10 0.850 - the 2026-09-26 run -
    while BENCHMARKS.md recorded 0.643, 0.700, 0.485, 0.907 from 2026-10-27
    over the identical corpus and the identical 1,982 questions.

    Both the slide deck and the project page carried the older set, so the
    stale number had already propagated into work being shown to other people
    before anybody noticed.
    """
    run, readme = measured(), claimed()

    for name, value in readme.items():
        assert value == run[name], (
            f"the README says {name} {value} and the anchored run measured "
            f"{run[name]}. One of them is wrong, and editing the README "
            f"without moving the anchor is not how this is fixed."
        )


def test_the_readme_names_the_question_and_turn_counts_of_that_run():
    """The denominators, which are what make the figures mean anything. 1,982
    questions over 5,882 turns is the whole of LoCoMo; a subset would score
    differently and read identically."""
    row = re.search(r"^\|\s*LoCoMo retrieval([^|]*)\|", README.read_text(), re.MULTILINE)
    assert row, "the README no longer has a LoCoMo retrieval row"

    assert "1,982 questions" in row.group(1)
    assert "5,882 turns" in row.group(1)

    after = BENCHMARKS.read_text().split(ANCHOR, 1)[1]
    overall = re.search(r"^\|\s*\*\*overall\*\*\s*\|\s*\*\*([\d,]+)\*\*", after, re.MULTILINE)
    assert overall and overall.group(1) == "1,982", (
        "the anchored run does not score 1,982 questions, so the README's "
        "denominator belongs to a different run than its numbers"
    )


def test_the_longmemeval_row_agrees_with_its_own_table():
    """The other external row. Not anchored, because LongMemEval has had one
    run: this asserts the README's three figures appear in the overall row of
    that run's table, which is the same agreement check with a cheaper way of
    finding the table."""
    readme = re.search(r"^\|\s*LongMemEval retrieval([^|]*)\|(.+?)\|",
                       README.read_text(), re.MULTILINE)
    assert readme, "the README no longer has a LongMemEval retrieval row"

    stated = {
        name: float(re.search(rf"{re.escape(name)}\s+\*{{0,2}}([0-9.]+)",
                              readme.group(2)).group(1))
        for name in ("session@10", "recall@10", "MRR")
    }

    rows = re.findall(r"^\|\s*\*\*overall\*\*\s*\|\s*\*\*90\*\*\s*\|(.+)$",
                      BENCHMARKS.read_text(), re.MULTILINE)
    assert rows, "no 90 question overall row in BENCHMARKS.md"
    cells = [float(c.strip().strip("*")) for c in rows[-1].split("|") if c.strip()]
    run = dict(zip(COLUMNS, cells, strict=True))

    for name, value in stated.items():
        assert value == run[name], (
            f"the README says LongMemEval {name} {value}, the table says {run[name]}"
        )
