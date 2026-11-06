# Compliance

What a buyer's security and legal review asks for, answered with things that can
be checked rather than claimed. Every number here is reproducible by the reader,
and the command that reproduces it is given.

## The licence of Echo Memory itself

Apache License 2.0, SPDX `Apache-2.0`. Copyright Ateleir Tech, named in
`NOTICE`.

**This is open source.** Apache 2.0 is OSI approved and FSF free. An earlier
version of this page said the opposite, which was true while the project was
BSL 1.1 and false from the moment it was not; a checklist answered from it in
the meantime was answered wrongly, in the direction of understating the rights
on offer.

**There is no employee count, no revenue threshold and no commercial licence.**
Production use is free for any organisation at any size, including offering
Echo Memory to third parties as a hosted service. The restriction this page
used to describe - fewer than 50 employees and under US$5,000,000 of revenue -
was withdrawn on 2026-10-26 and does not apply to any version.

Nor is there a conversion date to wait for. BSL converts each version to
Apache 2.0 four years after it ships, and that mechanic is gone with the
licence: **every version is Apache 2.0 now**, including 0.5.0 to 0.5.5 which
shipped under BSL. Ateleir Tech holds the copyright in the whole work and
relicensed all of it, so the grant only widened and nobody lost a right they
already had.

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

## Encryption, and the two places there is none

Measured against the running service on 2026-11-06, not described from the
deployment plan. Every claim below has the command that checks it.

### In transit

TLS 1.3 on all three hostnames, certificate issued and renewed by Caddy:

```bash
echo | openssl s_client -connect app.echo-mem.com:443 \
  -servername app.echo-mem.com -brief
# Protocol version: TLSv1.3   Ciphersuite: TLS_AES_128_GCM_SHA256
```

`api.echo-mem.com`, `app.echo-mem.com` and `admin.echo-mem.com` all answer
TLS 1.3 with AES-128-GCM. Plain HTTP is answered with a 308 to HTTPS. The
session cookie is `httponly`, `secure` and `samesite=lax`, so it is never sent
over plain HTTP in production and is not readable by page scripts.

Only ports 80 and 443 are open to the internet, and **there is no SSH**:
operator access is AWS Systems Manager, so there is no key to lose and no
listening sshd.

```bash
aws ec2 describe-security-groups --group-ids sg-07b90334765275cec \
  --query 'SecurityGroups[0].IpPermissions[].FromPort'
# 80, 443
```

**The application to database hop is NOT encrypted.** Postgres has `ssl = off`
and live connections report `ssl = f`:

```bash
docker compose exec -T db psql -U postgres -d echo_cloud \
  -c "SELECT ssl, client_addr FROM pg_stat_ssl JOIN pg_stat_activity USING (pid)"
# f | 172.18.0.3
```

That is stated rather than glossed because it is the question a reviewer
should ask. The connection never leaves the machine: the database is a
container on the same host, reached over a Docker bridge address, and its port
is not reachable from outside the instance — only 80 and 443 are open, and a
direct connection to 5433 from the internet is refused. So the traffic is not
exposed to a network an attacker can be on, and TLS between two processes on
one host would protect against an attacker who already has that host.

It is still a real difference from a managed database, and the honest version
is: this deployment's isolation is the instance boundary, not transport
encryption. A deployment that moves the database off the host **must** set
`sslmode=verify-full`; `ECHO_CLOUD_DATABASE_URL` accepts it and
[DEPLOY.md](../DEPLOY.md) says so.

### At rest

The volume holding every customer's memory is encrypted with a KMS customer
managed key:

```bash
aws ec2 describe-volumes --filters \
  Name=attachment.instance-id,Values=i-03f980b2c6b27c24e \
  --query 'Volumes[].{encrypted:Encrypted,kms:KmsKeyId}'
# Encrypted: true
# arn:aws:kms:ap-south-1:190460274227:key/bc0ff9de-...
```

Daily EBS snapshots inherit that encryption, and every snapshot in the account
reports `Encrypted: true`.

Nightly logical backups go to S3 with SSE-S3 (AES256) and bucket keys, the
bucket blocks all four forms of public access, and versioning is on:

```bash
aws s3api head-object --bucket echomem-cloud-backups-190460274227 \
  --key postgres/2026/11/echo_cloud-20261106T021846Z.dump
# ServerSideEncryption: AES256
```

The instance role's S3 permissions are `PutObject` and
`AbortMultipartUpload`, scoped to the `postgres/*` prefix, plus `ListBucket`.
**No `GetObject` and no `DeleteObject`**, verified from the role rather than
from the script's comment:

```bash
aws iam get-role-policy --role-name echomem-cloud-ec2 \
  --policy-name echomem-backup-write
```

So a compromised host can write a new backup and list what exists. It cannot
read an old one or destroy one. Versioning covers overwrite of the same key.

**Customer facts are stored as plaintext rows.** There is no field level or
column level encryption. What protects them at rest is the encrypted volume
and the encrypted backup, which means they are protected against a lost disk
or a leaked snapshot and **not** against anybody who can reach the running
database. Said plainly because "encrypted at rest" is often read as the
second thing and here it is only the first.

Credentials are not stored in a recoverable form at all, which is a stronger
property than encryption:

| | how it is stored |
|---|---|
| Account passwords | Argon2, via `argon2.PasswordHasher` |
| API keys | SHA-256 of the key; only the last four digits are kept in clear |
| Erasure subjects | HMAC-SHA256 of the normalised name |

So a database dump does not yield a password or a usable API key. The key
hash is unsalted SHA-256, which is correct here and would not be for a
password: the input is `secrets.token_urlsafe(32)`, 32 bytes from a CSPRNG,
rather than something a person chose - so there is no dictionary to run
against it and nothing a salt would defend.

Deployment secrets live in `/opt/echo-mem-cloud/.env` on the instance,
readable by root, which is ordinary for Docker Compose and is the thing to
replace first if this moves to a platform with a secret manager.

### Two gaps, named rather than left to be found

**No HSTS.** Plain HTTP redirects to HTTPS, but without
`Strict-Transport-Security` a first visit over HTTP can have that redirect
stripped by an active network attacker. It is a one line Caddy change and the
only reason to be careful is that browsers cache it, so a short `max-age`
comes first.

**No bucket policy denying non-TLS requests.** Uploads use HTTPS because the
AWS SDK defaults to it, not because the bucket refuses anything else. A
`Deny` on `aws:SecureTransport: false` would make it a property of the bucket
rather than of the client.

Neither is exploitable from where the service stands today. Both are listed
because a page that only lists what is right is not evidence of anything.

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
