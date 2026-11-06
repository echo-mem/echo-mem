"""docs/COMPLIANCE.md describes what is encrypted; the code is the truth.

That page is read by somebody deciding whether to trust this with their data,
and it previously said nothing at all about encryption while making detailed
claims about licensing that had been false for eight days (#188). A page that
answers a security question has to be pinned to the thing it describes, or it
becomes the most confidently wrong file in the repository.

What can be pinned here is the half that lives in code: how credentials are
stored. The infrastructure half - the KMS key on the volume, SSE on the
bucket, the IAM policy, TLS 1.3 at the edge - cannot be asserted from a unit
test and is instead recorded in that page WITH the command that reproduces
it, so a reader checks rather than believes.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPLIANCE = ROOT / "docs" / "COMPLIANCE.md"

#: The cloud, which is where credentials are stored. A relative path because
#: the two repositories sit side by side in development and the cloud is not
#: importable from here.
CLOUD = ROOT.parent / "echo-mem-cloud" / "src" / "echo_cloud"


def flat(text: str) -> str:
    return " ".join(text.split())


def test_the_page_answers_the_encryption_question_at_all():
    """It did not. Zero mentions of encryption, at rest, in transit or TLS, in
    the document whose first line says it answers what a buyer's security and
    legal review asks for."""
    text = flat(COMPLIANCE.read_text()).lower()

    for phrase in ("in transit", "at rest", "tls 1.3"):
        assert phrase in text, f"COMPLIANCE.md does not mention {phrase!r}"


def test_the_page_says_the_database_hop_is_not_encrypted():
    """The one claim a reviewer is most likely to assume the other way, and the
    one most tempting to leave out. If this stops being true because the
    database moves or TLS is switched on, this test should fail and the page
    should be rewritten rather than quietly left understating the service."""
    text = flat(COMPLIANCE.read_text())

    assert "application to database hop is NOT encrypted" in text


def test_the_page_says_facts_are_plaintext_rows():
    """"Encrypted at rest" is commonly read as field level encryption. Here it
    means an encrypted volume and an encrypted backup, and the difference is
    exactly what a reviewer needs."""
    text = flat(COMPLIANCE.read_text())

    assert "Customer facts are stored as plaintext rows" in text
    assert "no field level or column level encryption" in text.lower()


def test_the_named_gaps_are_still_named():
    """Two gaps are listed: no HSTS, and no bucket policy denying non-TLS. If
    either is fixed, the fix includes deleting its paragraph - which this test
    forces somebody to notice rather than leaving a page claiming a weakness
    that is gone."""
    text = flat(COMPLIANCE.read_text())

    assert "No HSTS" in text
    assert "No bucket policy denying non-TLS requests" in text


# --- the half that is code -----------------------------------------------


def _cloud_source(name: str) -> str:
    path = CLOUD / name
    if not path.exists():  # pragma: no cover - a standalone engine checkout
        import pytest

        pytest.skip("echo-mem-cloud is not checked out beside this repository")
    return path.read_text()


def test_passwords_are_argon2_not_encrypted_and_not_plain_sha():
    """The page claims Argon2. A password put through bare SHA-256 would make
    that claim false in the direction that matters, and the two are one import
    apart."""
    text = _cloud_source("accounts.py")

    assert "from argon2 import PasswordHasher" in text
    # No bare digest of a password anywhere in the account module.
    assert not re.search(r"sha256\(\s*password", text), (
        "a password is being hashed with bare SHA-256"
    )


def test_an_api_key_is_stored_as_a_digest_and_32_bytes_of_csprng():
    """Both halves of the page's claim: the stored form is a digest, and the
    input is random enough that an unsalted digest is the right choice."""
    text = _cloud_source("apikeys.py")

    assert "hashlib.sha256" in text
    assert "secrets.token_urlsafe(KEY_BYTES)" in text
    assert re.search(r"^KEY_BYTES\s*=\s*32$", text, re.MULTILINE), (
        "the page says 32 bytes of CSPRNG output; this no longer is"
    )


def test_the_page_quotes_the_key_length_the_code_uses():
    """The number in the prose and the number in the code, compared. This is
    the shape that goes stale silently: the README carried $99 for weeks after
    the price moved to $149."""
    code = _cloud_source("apikeys.py")
    match = re.search(r"^KEY_BYTES\s*=\s*(\d+)$", code, re.MULTILINE)
    assert match

    assert f"token_urlsafe({match.group(1)})" in flat(COMPLIANCE.read_text())
