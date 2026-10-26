# Compliance

What a buyer's security and legal review asks for, answered with things that can
be checked rather than claimed. Every number here is reproducible by the reader,
and the command that reproduces it is given.

## The licence of Echo Memory itself

Apache License 2.0, SPDX `Apache-2.0`. Copyright Ateleir Tech, named in
`NOTICE`.

**This is source available, not open source.** BSL is neither OSI approved nor
FSF free, and saying otherwise to pass a checklist would be the kind of claim
this document exists to avoid.

Production use is free for an organisation with **fewer than 50 employees AND
under US$5,000,000 annual revenue**, counting parents, subsidiaries and
affiliates. Development, testing, evaluation, research and teaching are free for
everybody at any size. Above either threshold, production use needs a commercial
licence, and so does offering Echo Memory to third parties as a hosted service:
hello@echo-mem.com.

Each published version converts to Apache 2.0 four years after it ships.
Version 0.4.1 and everything before it are Apache 2.0 permanently, because
relicensing cannot reach backwards.

## What ships, and under which licence

```bash
.venv/bin/python scripts/licence-inventory.py
```

It walks the runtime dependency closure from `pyproject.toml` rather than
listing what happens to be installed, because a developer environment carries
test and lint tooling that never reaches a customer.

As of 2026-10-08, across the 61 distributions that ship:

| | |
|---|---|
| Strong copyleft (GPL, AGPL, SSPL) | **none** |
| Weak or file level | `psycopg`, `psycopg-pool` (LGPL-3.0-only), `certifi`, `tqdm` (MPL-2.0) |
| Everything else | MIT, BSD, Apache 2.0 and equivalents |

On the four: LGPL and MPL are not the thing a proprietary product has to fear.
`psycopg` is imported, not modified and not statically linked, which is the use
LGPL is written to permit. MPL-2.0 is file level: it reaches modified MPL files,
and none are modified here.

One trap worth naming, because the inventory script hit it. `scipy` publishes
47,596 characters of licence TEXT in its `License` metadata field, including
bundled third party notices, one of which reads `GPL-3.0-or-later WITH
GCC-exception-3.1`. A naive scan reports scipy as strong copyleft. Its own
licence is BSD-3-Clause, per its classifier. The script prefers identifiers over
prose for exactly this reason.

## Supply chain and build integrity

Releases are built and published by GitHub Actions through **PyPI Trusted
Publishing**, which issues a **Sigstore** attestation binding each artifact to
the workflow that produced it. No API token exists to leak, and nothing is
signed by hand.

Verify any release without trusting us:

```
https://pypi.org/integrity/echo-mem/0.5.3/echo_mem-0.5.3-py3-none-any.whl/provenance
```

The publisher recorded there is `echo-mem/echo-mem`, workflow `release.yml`.
Every release from 0.4.1 onward carries one.

This satisfies **SLSA Build L2**: a hosted build service, signed provenance, and
an identity tied to the workflow rather than to a person. L3 additionally wants
stronger isolation between build steps and is not claimed.

Each release carries two attestations, which are different things and both real:

- `*.publish.attestation`, PEP 740 files written by the PyPI upload itself
- `multiple.intoto.jsonl`, a Sigstore bundle carrying an in-toto statement over
  every distribution in the release, signed with the workflow's identity and
  logged to Rekor

The second exists because the first is not what a scanner looks for. OpenSSF
Scorecard's Signed-Releases check reads release assets for `.asc`, `.sig`,
`.minisig` or `.intoto.jsonl`, so it scored this project 0 while every release
was in fact signed. The answer was to publish a second genuine artifact in the
format the convention names, rather than to rename the first one into a format
it is not: a verifier reaching for standard tooling has to find what the
filename promises.

The bundle is copied into `dist/` **after** the PyPI upload and before
`gh release create`. `gh-action-pypi-publish` sends everything in its packages
directory through twine, which refuses a file whose extension is not a
distribution format, so staging it earlier fails the upload. Only the GitHub
Release needs the file, and that step is last.

Every GitHub Action used is pinned to a **commit SHA**, not a tag, so an action
owner cannot repoint a version under us. Dependabot keeps those pins current.

## Static analysis and testing

CodeQL runs on every push to main, every pull request, and weekly, with the
`security-extended` query suite. `ruff` also runs in CI, but it is a linter and
is not counted as security analysis.

The test suite is 890 tests against a real PostgreSQL with Apache AGE and
pgvector. There are no mocked databases: tenant isolation is a property of what
the database returns, and a fake that returns whatever the test set up would
assert nothing.

## OpenSSF Scorecard

```bash
scorecard --repo=github.com/echo-mem/echo-mem
```

Run it yourself. We do not publish a cached number, because a score that is
quoted rather than recomputed is a score that was true once.

Three checks score zero and no amount of work changes them soon, so they are
stated here rather than explained away when asked:

- **Maintained** requires 90 days of history. The repository is younger than that.
- **Contributors** wants contributors from two or more organisations. There is
  one maintainer.
- **Code-Review** counts approved changesets. Branch protection requires an
  approving review and there is one maintainer, so changes are merged by the
  author with admin rights. The compensating controls are the required status
  checks, linear history, no force pushes, and a test suite that must pass.

## OpenSSF Best Practices Badge

**Obtainable as of 2026-10-26, and not yet claimed.** The badge's first
criterion, `floss_license`, is a MUST: the software has to be released under an
OSI approved or FSF free licence. That blocked it outright while the project was
BSL 1.1, and the relicence to Apache 2.0 removes the block.

It is not claimed yet because the badge is self assessed against 67 criteria at
the passing level, and asserting it before working through them would be the
kind of unearned claim this document exists to avoid.

They have now been worked through, one at a time, in
[OPENSSF-BADGE.md](OPENSSF-BADGE.md): the answer and the evidence for all 67.
**No MUST is unmet.** 43 are MUST, of which 39 are met and 4 are N/A where the
criteria file permits N/A for that specific criterion, which was checked rather
than assumed.

Three are answered Unmet, all of them SUGGESTED, which do not block the badge:

| | why |
|---|---|
| `test_most` | coverage is not measured at all, so there is no number to claim |
| `warnings_strict` | ruff near its defaults, and no type checker |
| `dynamic_analysis` | no fuzzer and no sanitizer runs before a release |

Each is a real gap, answered Unmet rather than argued into a Met. Two criteria,
`know_secure_design` and `know_common_errors`, are assertions about the
maintainer and are left for the maintainer to make.

Until it is claimed, the stronger evidence is Scorecard plus the Sigstore
attestation on each release, because both are computed by somebody else rather
than declared here.

## Vulnerability reporting

See [SECURITY.md](../SECURITY.md). Do not open a public issue.
