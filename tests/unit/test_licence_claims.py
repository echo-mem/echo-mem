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


#: Files that describe the licence to a reader deciding whether they may use
#: this. NOTICE is deliberately absent: it records the BSL window on purpose.
SELLS_THE_LICENCE = ("README.md", "docs/COMPLIANCE.md")

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
    for name in SELLS_THE_LICENCE:
        text = flat(ROOT / name).lower()
        for phrase in IMPOSSIBLE_UNDER_APACHE:
            assert phrase not in text, (
                f"{name} still says {phrase!r}, which Apache 2.0 does not "
                f"impose. A reader deciding whether they may use this would "
                f"be told, wrongly, that they may not."
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
