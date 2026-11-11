"""The licence this project claims has to be the one in the file.

A package that says one licence in its metadata while shipping another is
making a false statement to everybody who installs it, and those are exactly
the people who read metadata rather than a LICENSE file.

This project has now changed licence twice: Apache 2.0 to BSL 1.1 on
2026-09-25, and back to Apache 2.0 on 2026-10-26. Both times the thing that
broke first was a claim in a file nobody thought to look at. So these pin
every place the claim appears, including the ones that are easy to forget.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def flat(path: Path) -> str:
    """The file with its line wrapping collapsed.

    A licence is wrapped for a human to read, so asserting on raw text makes
    a test fail when a phrase happens to straddle a newline, which says
    nothing about whether the licence is right. This one did exactly that on
    "fewer than 50 employees".
    """
    return " ".join(path.read_text().split())


def line_of(path: Path, phrase: str) -> int:
    """The first line of `path` containing `phrase`, for the failure message.

    flat() collapses newlines so a wrapped phrase can be matched at all, which
    loses the line number a maintainer actually needs. This finds it back on
    the unflattened text, and falls back to 0 when the phrase only exists
    across a line break.
    """
    low = phrase.lower()
    for n, line in enumerate(path.read_text().splitlines(), start=1):
        if low in line.lower():
            return n
    return 0


def test_the_licence_file_is_the_apache_license():
    text = flat(ROOT / "LICENSE")

    assert "Apache License" in text
    assert "Version 2.0, January 2004" in text
    assert "http://www.apache.org/licenses/" in text


def test_the_licence_text_is_unmodified():
    """Byte for byte, because GitHub detects a licence by matching the text
    and a helpful edit turns Apache-2.0 into "Other" in the sidebar. That is
    not cosmetic: "Other" is what kept this project out of every directory
    that filters on an OSI licence.

    202 lines is the canonical file. The copyright holder is named in NOTICE,
    which is the mechanism Apache provides for it, rather than by filling in
    the appendix and changing the text.
    """
    lines = (ROOT / "LICENSE").read_text().splitlines()

    assert len(lines) == 202, f"LICENSE is {len(lines)} lines, not the canonical 202"
    assert "[yyyy] [name of copyright owner]" in flat(ROOT / "LICENSE"), (
        "the appendix placeholder was filled in, which edits the canonical text"
    )


def test_the_notice_names_the_copyright_holder():
    text = flat(ROOT / "NOTICE")

    assert "Ateleir Tech" in text
    assert "Apache License, Version 2.0" in text


def test_the_package_metadata_says_apache():
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]

    assert meta["license"] == "Apache-2.0"
    assert "LICENSE" in meta["license-files"]
    assert "NOTICE" in meta["license-files"]


def test_nothing_still_claims_the_business_source_license():
    """The failure mode this whole file exists for. Going Apache to BSL left
    the metadata claiming Apache for a while; going back is the same hazard
    pointed the other way, and a stale BSL claim is worse because it tells
    somebody they may not use what they may."""
    for name in ("LICENSE", "NOTICE", "pyproject.toml"):
        text = flat(ROOT / name)
        assert "BUSL-1.1" not in text, name
        if name != "NOTICE":
            # NOTICE records the history on purpose; nothing else should.
            assert "Business Source" not in text, name


#: Every piece of rendered text this project publishes about itself.
#:
#: This used to be ("README.md", "docs/COMPLIANCE.md"), which are the two
#: files that were wrong in November and therefore the two that got a guard.
#: That is the wrong shape for the same reason the guard before it was: it
#: covers the places somebody already found, so the next stale page is
#: wherever nobody looked. echo-mem-cloud proved it - its guard walked
#: src/echo_cloud/**/*.py only, and docs/DATA-PROCESSING.md sold the BSL in
#: three places for sixteen days underneath it.
#:
#: So the list is discovered rather than enumerated, and each exclusion below
#: has to earn itself.
def sells_the_licence() -> list[Path]:
    out = [
        ROOT / name
        for name in (
            "README.md",
            "CONTRIBUTING.md",
            "SECURITY.md",
            "CODE_OF_CONDUCT.md",
            "DCO.md",
            # Shipped inside the wheel and installed into an agent's skill
            # directory, so it is read by more agents than the README is by
            # people.
            "src/echo_memory/skill/SKILL.md",
        )
        if (ROOT / name).exists()
    ]
    # docs/*.md, top level only. Every page a reader reaches from the
    # repository, including the ones that did not exist when this was written.
    out += sorted(p for p in (ROOT / "docs").glob("*.md"))
    return out


#: Not scanned, each for a stated reason rather than because it was awkward.
#:
#:   CHANGELOG.md        records the relicence in both directions on purpose.
#:                       It has to contain "Business Source License" to be a
#:                       changelog at all.
#:   NOTICE              records the BSL window on purpose; that is the
#:                       mechanism Apache provides for it.
#:   TODOS.md            internal working notes, not published prose.
#:   docs/designs/       dated design records written before the thing was
#:                       built. echo-memory-design.md describes three MCP
#:                       tools and a PR5 that would wire them together. That
#:                       is what was planned, which is the document's whole
#:                       point, and rewriting it to match the present would
#:                       destroy the record rather than correct it.
#:   .claude/, .agents/  vendored third party tooling, several hundred files.
#:                       Not this project's text and not ours to edit.
#:   spikes/             throwaway experiments.
NOT_SCANNED = (
    "CHANGELOG.md",
    "NOTICE",
    "TODOS.md",
    "docs/designs",
    ".claude",
    ".agents",
    "spikes",
)

#: Phrases that cannot be true under Apache 2.0, whatever sentence they sit in.
#: Deliberately the OBLIGATION rather than the numbers: "fewer than 50
#: employees" may legitimately appear in a sentence saying that restriction was
#: withdrawn, and banning the figure would ban the correction along with the
#: claim. An obligation has no past tense that reads this way - there is no
#: honest current sentence containing "needs a commercial licence".
IMPOSSIBLE_UNDER_APACHE = (
    "needs a commercial licence",
    "needs an agreement",
    "not open source",
    "converts to apache 2.0 four years",
    "neither osi approved nor",
)


def test_no_file_that_sells_the_licence_still_imposes_a_bsl_obligation():
    """The bug this is written for, which the two tests below missed.

    On 2026-11-03, eight days after the relicence, README's Hosted section
    still read "free in production for organisations under 50 people and under
    $5M revenue ... above either threshold, production use needs an
    agreement", and docs/COMPLIANCE.md - the page a buyer's legal review reads
    - still said "This is source available, not open source" and described the
    same restriction plus a four year conversion date.

    Both existing README tests passed throughout. They assert the CORRECTION is
    present: that "Apache 2.0" appears, that the BSL window is named, that the
    word "wider" is there. All true, and all true sixty lines away from a
    paragraph telling a reader they may not use this. A guard that checks a
    correction exists cannot see the claim it was meant to replace, which is
    the same shape as a guard matching an identifier instead of a behaviour.

    So this asserts the ABSENCE of the obligation, which is the thing that
    would stop somebody using software they are free to use.
    """
    offending = []
    for path in sells_the_licence():
        text = flat(path).lower()
        for phrase in IMPOSSIBLE_UNDER_APACHE:
            if phrase in text:
                offending.append((path.relative_to(ROOT), line_of(path, phrase), phrase))

    assert not offending, "\n".join(
        f"  {f}:{n}  says {p!r}, which Apache 2.0 does not impose. A reader "
        f"deciding whether they may use this would be told, wrongly, that "
        f"they may not."
        for f, n, p in offending
    )


#: Licence names that were true once. Unlike the obligations above these have
#: an honest past tense, so they are allowed where something nearby withdraws
#: them - and nowhere else.
#:
#: Bare figures are deliberately in here rather than in
#: IMPOSSIBLE_UNDER_APACHE. "free only below fifty people and $5M of revenue.
#: That is withdrawn" is the sentence a reader who saw this project in October
#: needs, and a guard that banned the figure would ban the correction with the
#: claim. Requiring the withdrawal instead gets both.
SUPERSEDED = (
    "bsl",
    "busl",
    "business source",
    "source available",
    "50 employees",
    "fifty employees",
    "fifty people",
    "5,000,000",
    "$5m",
    "5m revenue",
    # The price before 2026-10. Not a pricing decision: the README served $99
    # for weeks after the service moved to $149, and this is only the figure
    # that is no longer true.
    "$99",
)

#: What makes a mention a withdrawal rather than a claim.
WITHDRAWS = (
    "withdrawn",
    "relicen",
    "no longer",
    "shipped under",
    "was withdrawn",
    "does not apply",
    "removes the block",
    "for about a month",
    "and is apache",
    # docs/COMPLIANCE.md's opening: "An earlier version of this page said the
    # opposite, which was true while the project was BSL 1.1 and false from
    # the moment it was not." Neither of those can read as a current claim -
    # one dates the text, the other dates the fact - so both count, and
    # neither was in the list until this test flagged that paragraph.
    "an earlier version",
    "was true while",
)


def blocks(path: Path) -> list[tuple[int, list[str]]]:
    """`path` as paragraphs, each with the line number it starts on.

    Paragraphs rather than the whole file, so a reported line number is the
    offending one. Flattening a file whole loses the mapping, and looking the
    phrase back up by its first occurrence reported docs/COMPLIANCE.md:14 -
    the legitimate historical sentence - for a planted claim on line 18. A
    guard that names the wrong line sends somebody to read correct text and
    conclude the test is broken.
    """
    out, start, buf = [], 1, []
    for n, line in enumerate(path.read_text().splitlines(), start=1):
        if line.strip():
            if not buf:
                start = n
            buf.append(line)
        elif buf:
            out.append((start, buf))
            buf = []
    if buf:
        out.append((start, buf))
    return out


def sentences(lines: list[str]) -> list[str]:
    """One paragraph's lines split into sentences, wrapping collapsed first.

    Collapsed because a licence paragraph wraps mid-sentence, so a line-based
    check reads a withdrawal two lines down as absent and fails a page that
    is correct.

    Split on a full stop NOT preceded by a digit. "Versions 0.5.0 to 0.5.5
    shipped under the Business Source License" is one sentence containing
    four full stops, and a naive splitter turns the only honest way to write
    that history into five fragments with the withdrawal in none of them.
    """
    flat_text = " ".join(" ".join(lines).split())
    return [s for s in re.split(r"(?<![0-9])[.!?]+\s+", flat_text) if s]


def line_in(start: int, lines: list[str], phrase: str) -> int:
    """The line inside one paragraph that carries `phrase`."""
    for offset, line in enumerate(lines):
        if phrase in line.lower():
            return start + offset
    return start


def test_no_superseded_licence_name_appears_without_being_withdrawn():
    """The half the obligation list cannot see.

    IMPOSSIBLE_UNDER_APACHE catches a sentence that imposes something. It does
    not catch a sentence that merely NAMES the old licence as the current one,
    which is what public/llms.txt on the website did for sixteen days: a link
    labelled "License (Business Source License 1.1)", seventy-six lines below
    its own line saying Apache-2.0. No obligation in it, and wrong anyway.

    Checked per SENTENCE, not per file and not in a window of characters.

    Per file would be a presence check - "does the word 'withdrawn' appear
    anywhere" - which is what this whole module exists to argue against.

    A 400 character window was the first attempt and was the same bug in
    miniature. Inserting "The engine is source available under BSL 1.1, free
    under 50 employees" into docs/COMPLIANCE.md passed, because the correct
    paragraph around it withdraws the restriction and a window cannot tell
    which claim a withdrawal applies to. A withdrawal three sentences away is
    a withdrawal of a different claim, which is the same shape as a correction
    sixty lines from the thing it was meant to replace.
    """
    offending = []
    for path in sells_the_licence():
        for start, lines in blocks(path):
            for sentence in sentences(lines):
                low = sentence.lower()
                if any(w in low for w in WITHDRAWS):
                    continue
                for phrase in SUPERSEDED:
                    if phrase in low:
                        offending.append(
                            (
                                path.relative_to(ROOT),
                                line_in(start, lines, phrase),
                                phrase,
                            )
                        )

    assert not offending, (
        "these name a superseded licence term with nothing nearby withdrawing "
        "it, so each reads as a current term. The engine is Apache 2.0.\n"
        + "\n".join(f"  {f}:{n}  {p!r}" for f, n, p in offending)
        + "\n\nEither delete it, or put it in the past the way docs/COMPLIANCE.md "
        "does: name what the restriction was, then say it was withdrawn IN THE "
        f"SAME SENTENCE. One of {WITHDRAWS} has to appear in the sentence "
        "carrying the term, not merely somewhere near it."
    )


def test_the_compliance_page_says_it_is_open_source():
    """The inverse claim, stated rather than merely not-denied. COMPLIANCE.md
    exists to answer a procurement checklist, and `floss_license_osi` in
    docs/OPENSSF-BADGE.md is answered Met on the grounds that Apache 2.0 is OSI
    approved. The two pages contradicted each other for eight days."""
    text = flat(ROOT / "docs/COMPLIANCE.md").lower()

    assert "this is open source" in text
    assert "osi approved" in text


def test_the_readme_states_the_licence_and_that_it_widened():
    """Somebody who saw this project in October read that production use
    needed an agreement above fifty people. They have to be able to find out
    that it does not, without reading a git history."""
    text = flat(ROOT / "README.md")

    assert "Apache 2.0" in text
    assert "0.5.0 to 0.5.5" in text, "the BSL window is not named"
    assert "wider" in text, "it does not say nobody lost a right"


def test_the_readme_badge_matches_the_licence():
    assert "license-Apache%202.0-blue" in (ROOT / "README.md").read_text()


def test_no_stale_apache_only_file_is_left_behind():
    """LICENSE-APACHE-2.0 existed to hold the text for versions released
    before the BSL window. LICENSE is that text now, and two copies invite
    them to drift."""
    assert not (ROOT / "LICENSE-APACHE-2.0").exists()
