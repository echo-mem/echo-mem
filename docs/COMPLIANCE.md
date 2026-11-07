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

### Two gaps that were named here, now closed

Both were listed on this page as gaps before they were fixed, which is the
order that makes a page like this worth reading.

**HSTS**, added 2026-11-07. Plain HTTP already redirected to HTTPS, but a
redirect is something an active network attacker can strip on a first visit,
and that first visit is the one carrying a session cookie on the way back.

```bash
curl -sS -o /dev/null -D - https://app.echo-mem.com/healthz | grep -i strict
# strict-transport-security: max-age=86400
```

`max-age` is deliberately one day rather than a year. Browsers **cache** this,
so a wrong value cannot be withdrawn by editing the server: every visitor
keeps enforcing it until their own copy expires. No `includeSubDomains` and no
`preload`, for the same reason - both are far harder to take back than to add.
It is raised once it has run unremarkably.

**A bucket policy denying non-TLS requests**, added 2026-11-07. Uploads used
HTTPS because the AWS SDK defaults to it, not because the bucket refused
anything else. Now it is a property of the bucket:

```bash
aws s3api get-bucket-policy --bucket echomem-cloud-backups-190460274227
# Deny s3:* when "aws:SecureTransport": "false"

aws s3api list-objects-v2 --bucket echomem-cloud-backups-190460274227 \
  --endpoint-url http://s3.ap-south-1.amazonaws.com
# AccessDenied ... with an explicit deny in a resource-based policy
```

Checked in both directions rather than only the one that passes: the same
call over HTTPS still lists the backups.

### The gap that remains

**The application still reaches Postgres over a plaintext connection**
(`ssl = off`), on the Docker bridge, with 5433 filtered at the security group.
Nothing leaves the host, so the exposure is an attacker who already has code
execution on the instance - who at that point can read the data directory
anyway. It is listed because a page that only lists what is right is not
evidence of anything, and because "encrypted in transit" without this
sentence would be a claim that is true of the edge and not of every hop.

## Retention and deletion

Two separate obligations, answered by two commands.

**On request.** GDPR Article 17 and the DPDP Act require actual deletion of a
subject's personal data when they ask. `echo-memory forget` does it: a fact
and every version of its triple, or an entity with its aliases and every fact
touching it, or a whole scope. Supersession is not deletion - a superseded
fact keeps its text and gains a `t_invalid` - so this is a separate path that
also removes embeddings, redacts audit text, strips fact ids out of read and
eval records, drops the orphaned entities and vacuums the pages. Every run
writes an `erasure_event` row holding counts, a scope, a verb, who asked and a
ticket reference, and deliberately no fact text and no subject name. See
[ERASURE.md](ERASURE.md).

**As a standing rule.** Article 5(1)(e) and the DPDP Act also require that
data is not kept longer than its purpose needs, which no deletion-on-request
path answers. `echo-memory retain` records how long a scope keeps a fact,
measured from the instant the fact became true, along with the purpose that
duration serves and the person who decided it. Both are required columns: "we
delete after 90 days" is not a compliance answer, and the reason is the part
nobody can reconstruct later. Replacing a policy retires the old one rather
than overwriting it, so "why did we stop deleting after 90 days" stays
answerable.

`retain --enforce` deletes what the policy no longer permits keeping, and it
does so by handing those facts to the same erasure path above - so a retention
deletion and a subject request leave one kind of record rather than two.
Neither is reachable over MCP, and there is a test that reads the published
tool list so adding either later fails loudly: irreversible deletion one
prompt injection away from any agent holding a key is not a trade worth
making.

### What this does not do

**There is no default policy.** A scope with no policy keeps its facts, and
`retain` reports that as "nobody has decided" rather than as a retention
period, because those are different answers and only one of them is a
finding. Inventing a default would be this software deciding a compliance
question on an operator's behalf, in the direction of deleting their data.

**Nothing enforces on a schedule.** `retain --enforce` is an operator action,
and without `--confirm` it only reports what would go. When enforcement runs
is an operational choice, so it belongs in whatever invokes it rather than in
a column here.

## Whether provenance can be trusted

Every fact records its author, its session, the version of the package that
wrote it and when it became true. Until 2026-11-06 all of that was believed
because the database said so: anyone who could write SQL could change a
fact's text, repoint it at a different entity, or rewrite its `agent_id`, and
nothing afterwards could tell that it had happened. For AI governance that is
the gap that matters, because the value of "which agent asserted this" is
precisely that it cannot be edited later by whoever finds the answer
inconvenient.

Each fact now carries a digest over its own claims - the endpoint names, the
relation, the text, the confidence, the causal type, `t_valid`, `agent_id`,
the project and the session - chained to the previous attestation in the same
scope. `echo-memory verify` recomputes the chain and answers three separate
questions: has any fact changed since it was written, is the chain
continuous, and does every fact have a link at all.

```bash
echo-memory verify           # recompute the chain for this scope
echo-memory verify --head    # the one string to record elsewhere
echo-memory verify --backfill  # attest facts written before this shipped
```

### What it is worth, which depends on one of two things

A digest stored in the same database as the data it attests is worth nothing
against somebody who can recompute it. That needs one of:

- **A key.** `ECHO_MEMORY_ATTESTATION_KEY` makes each digest an HMAC-SHA256,
  so SQL access without the key cannot forge one. The key does not belong in
  the database; the same secret store the erasure key uses is the right place.
- **An external witness.** Unkeyed the chain is still a chain: any change
  anywhere alters every digest after it, including the head. Recording the
  head outside this database - a ticket, a commit, a log the database cannot
  reach - makes a silent rewrite impossible with no key at all.

With neither, this detects corruption and careless edits and nothing more.
`verify` prints which of the two is in force on every run rather than
reporting a clean chain as though the result were unconditional, and the
`keyed` column is on every row so a scope attested unkeyed cannot later be
described as keyed.

### Three things it does not prove

**Not the author.** `agent_id` is still a string the writer supplies. The
attestation proves the recorded author has not changed since the write, not
that it was true at the write. Proving that would need a key per agent, and
an agent here is a model running in somebody else's process rather than
something that can hold one.

**Not the interval before the attestation.** A fact attested by `--backfill`
has a baseline from the moment it ran and says nothing about what happened
before. Each row's `attested_at` is what keeps that visible, which is why
backfilling is an operator command rather than something `init-db` does
quietly.

**Not the absence of deletion, on its own.** A missing link is visible, but
whether it was erasure doing its job or somebody removing evidence is
answered by reconciling the gaps against `erasure_event`, which `verify`
does. A legitimate rewrite - `merge` repointing an edge, a causal pass
retyping a fact - is reported and labelled with the audit entry that accounts
for it rather than hidden, so a reader still sees that the fact is not what
was attested.

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

Two are answered Unmet, both SUGGESTED, which do not block the badge:

| | why |
|---|---|
| `warnings_strict` | ruff near its defaults, and no type checker |
| `dynamic_analysis` | no fuzzer and no sanitizer runs before a release |

`test_most` was the third until 2026-11-07. Branch coverage is now measured
at 76.48% and gated in CI, so it is Met with a number rather than argued.

Each is a real gap, answered Unmet rather than argued into a Met. Two criteria,
`know_secure_design` and `know_common_errors`, are assertions about the
maintainer and are left for the maintainer to make.

Until it is claimed, the stronger evidence is Scorecard plus the Sigstore
attestation on each release, because both are computed by somebody else rather
than declared here.

## Vulnerability reporting

See [SECURITY.md](../SECURITY.md). Do not open a public issue.
