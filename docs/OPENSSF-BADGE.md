# OpenSSF Best Practices badge, passing level

The answers to all 67 passing level criteria, with the evidence for each, so
the self assessment at <https://www.bestpractices.dev> is filled in from a
reviewed document rather than from memory at the keyboard.

**It is not claimed yet.** This file is the preparation, and
[COMPLIANCE.md](COMPLIANCE.md) says the badge is obtainable and unclaimed
rather than claiming it. Two criteria, `know_secure_design` and
`know_common_errors`, are assertions about the maintainer and nobody else can
make them; both are marked CONFIRM THIS YOURSELF below.

Criteria text is quoted from `criteria/criteria.yml` and
`config/locales/en.yml` in `coreinfrastructure/best-practices-badge`, read on
2026-10-27.

## Where it stands

| | MUST | SHOULD | SUGGESTED | total |
|---|---|---|---|---|
| Met | 39 | 9 | 10 | 58 |
| N/A | 4 | 1 | 1 | 6 |
| Unmet | 0 | 0 | 3 | 3 |
| **total** | 43 | 10 | 14 | **67** |

**No MUST is unmet.** Every MUST is either met or marked N/A where the
criteria file permits N/A for that specific criterion, which was checked
rather than assumed. The badge requires every MUST and SHOULD to be met,
unmet-with-justification or N/A; a SUGGESTED criterion may be answered Unmet
and still award the badge.

The three Unmet answers are all SUGGESTED and all genuine:

- **`test_most`** The project does not measure coverage at all: there is no pytest-cov, no coverage gate in CI, and therefore no number. 970 tests against a real database is not the same claim as branch coverage, and asserting "most" without measuring it would be exactly the unearned claim this project avoids elsewhere. SUGGESTED, so it does not block the passing badge.
- **`warnings_strict`** ruff runs with close to its default rule set and a 100 character line length; there is no type checker, so no mypy or pyright strict mode. Honest answer is not maximally strict. SUGGESTED.
- **`dynamic_analysis`** No fuzzer, no sanitizer and no dynamic analysis tool is applied before a release. For a pure Python project reading from PostgreSQL the usual candidates fit awkwardly, but nothing is being run and the answer is no. SUGGESTED.

Each is a real gap, answered Unmet rather than argued into a Met, which is
the whole reason this file exists.

## Basics

### Basic project website content

**`description_good`** `MUST` — **Met**

> The project website MUST succinctly describe what the software does (what problem does it solve?).

README's first paragraph states the problem: storing a memory invokes no model on the server. The repository description says it in one line, and https://echo-mem.com leads with it.

**`interact`** `MUST` — **Met**

> The project website MUST provide information on how to: obtain, provide feedback (as bug reports or enhancements), and contribute to the software.

README covers obtaining it (`pip install echo-mem`, the MCP registry, the hosted plan), CONTRIBUTING.md covers contributing, and Issues and Discussions are both enabled.

**`contribution`** `MUST` (URL required) — **Met**

> The information on how to contribute MUST explain the contribution process (e.g., are pull requests used?)

CONTRIBUTING.md states the process explicitly: branch, test, open a PR, merge, never commit to main. https://github.com/echo-mem/echo-mem/blob/main/CONTRIBUTING.md

**`contribution_requirements`** `SHOULD` (URL required) — **Met**

> The information on how to contribute SHOULD include the requirements for acceptable contributions (e.g., a reference to any required coding standard).

CONTRIBUTING.md requires a test for every new codepath, `ruff check`, and a DCO sign-off (`git commit -s`), the last enforced by .github/workflows/dco.yml on every commit in a branch.

### FLOSS license

**`floss_license`** `MUST` — **Met**

> The software produced by the project MUST be released as FLOSS.

Apache License 2.0. Relicensed from BSL 1.1 on 2026-10-26 covering the whole work, versions 0.5.0 to 0.5.5 included.

**`floss_license_osi`** `SUGGESTED` — **Met**

> It is SUGGESTED that any required license(s) for the software produced by the project be approved by the Open Source Initiative (OSI).

Apache-2.0 is OSI approved. GitHub detects it as `apache-2.0`, which requires the canonical 202 line text byte for byte.

**`license_location`** `MUST` (URL required) — **Met**

> The project MUST post the license(s) of its results in a standard location in their source repository.

LICENSE at the repository root, plus NOTICE naming the copyright holder. `license = "Apache-2.0"` in pyproject.toml, pinned by tests/unit/test_licence_claims.py so the metadata cannot drift from the file.

### Documentation

**`documentation_basics`** `MUST` — **Met**

> The project MUST provide basic documentation for the software produced by the project.

README.md plus docs/: DEVELOPMENT.md, WRITE-COST.md, BENCHMARKS.md, COMPLIANCE.md, ERASURE.md, INTEGRATIONS.md, PUBLISHING.md.

**`documentation_interface`** `MUST` — **Met**

> The project MUST provide reference documentation that describes the external interface (both input and output) of the software produced by the project.

The MCP tool contract is specified in docs/DEVELOPMENT.md; the 35 CLI commands are listed in README.md, and tests/unit/test_readme_claims.py pins that count to the argument parser so the two cannot disagree.

### Other

**`sites_https`** `MUST` — **Met**

> The project sites (website, repository, and download URLs) MUST support HTTPS using TLS.

All three verified HTTP 200 over TLS: https://echo-mem.com, https://github.com/echo-mem/echo-mem, https://pypi.org/project/echo-mem/.

**`discussion`** `MUST` — **Met**

> The project MUST have one or more mechanisms for discussion (including proposed changes and issues) that are searchable, allow messages and topics to be addressed by URL, enable new people to participate in some of the discussions, and do not require client-side installation of proprietary software.

GitHub Issues and GitHub Discussions are both enabled on a public repository.

**`english`** `SHOULD` — **Met**

> The project SHOULD provide documentation in English and be able to accept bug reports and comments about code in English.

All documentation, issues and commit messages are in English.

**`maintained`** `MUST` — **Met**

> The project MUST be maintained.

Actively maintained. Note the distinction from OpenSSF Scorecard's Maintained check, which scores 0 because it requires 90 days of history and the repository is younger than that. This criterion asks whether the project is maintained, not how old it is.

## Change Control

### Public version-controlled source repository

**`repo_public`** `MUST` — **Met**

> The project MUST have a version-controlled source repository that is publicly readable and has a URL.

https://github.com/echo-mem/echo-mem, public and readable without an account.

**`repo_track`** `MUST` — **Met**

> The project's source repository MUST track what changes were made, who made the changes, and when the changes were made.

git, so every change carries author, timestamp and parent.

**`repo_interim`** `MUST` — **Met**

> To enable collaborative review, the project's source repository MUST include interim versions for review between releases; it MUST NOT include only final releases.

Every change lands through a branch and a pull request. main is protected: linear history, no force pushes, a required review and a required `lint-and-test` status check.

**`repo_distributed`** `SUGGESTED` — **Met**

> It is SUGGESTED that common distributed version control software be used (e.g., git) for the project's source repository.

git.

### Unique version numbering

**`version_unique`** `MUST` — **Met**

> The project results MUST have a unique version identifier for each release intended to be used by users.

Each release has a unique version in pyproject.toml, and release.yml refuses a tag that disagrees with it.

**`version_semver`** `SUGGESTED` — **Met**

> It is SUGGESTED that the Semantic Versioning (SemVer) or Calendar Versioning (CalVer) version numbering format be used for releases. It is SUGGESTED that those who use CalVer include a micro level value.

Semantic Versioning: 0.3.0, 0.4.1, 0.5.0 through 0.5.6.

**`version_tags`** `SUGGESTED` — **Met**

> It is SUGGESTED that projects identify each release within their version control system. For example, it is SUGGESTED that those using git identify each release using git tags.

A v-prefixed annotated tag per release, and the tag is what triggers publishing at all.

### Release notes

**`release_notes`** `MUST` (URL required) — **Met**

> The project MUST provide, in each release, release notes that are a human-readable summary of major changes in that release to help users determine if they should upgrade and what the upgrade impact will be. The release notes MUST NOT be the raw output of a version control log (e.g., the "git log" command results are not release notes). Projects whose results are not intended for reuse in multiple locations (such as the software for a single website or service) AND employ continuous delivery MAY select "N/A".

CHANGELOG.md per version, and release.yml creates a GitHub Release carrying the distributions and a link to the changelog. https://github.com/echo-mem/echo-mem/blob/main/CHANGELOG.md

**`release_notes_vulns`** `MUST` — **N/A**

> The release notes MUST identify every publicly known run-time vulnerability fixed in this release that already had a CVE assignment or similar when the release was created. This criterion may be marked as not applicable (N/A) if users typically cannot practically update the software themselves (e.g., as is often true for kernel updates). This criterion applies only to the project results, not to its dependencies. If there are no release notes or there have been no publicly known vulnerabilities, choose N/A.

No publicly known run-time vulnerability has been fixed in any release, so there has been none to identify. Zero published security advisories, zero open or dismissed vulnerability reports.

## Reporting

### Bug-reporting process

**`report_process`** `MUST` (URL required) — **Met**

> The project MUST provide a process for users to submit bug reports (e.g., using an issue tracker or a mailing list).

GitHub Issues, linked from the README and CONTRIBUTING.md. https://github.com/echo-mem/echo-mem/issues

**`report_tracker`** `SHOULD` — **Met**

> The project SHOULD use an issue tracker for tracking individual issues.

GitHub Issues, with labels used for triage (bug, documentation, enhancement, good first issue, help wanted).

**`report_responses`** `MUST` — **Met**

> The project MUST acknowledge a majority of bug reports submitted in the last 2-12 months (inclusive); the response need not include a fix.

Every report has been responded to. There have been no externally filed bug reports; the three external pull requests received (#150, #151, #156) were each responded to on the day they were opened, and two were merged within a day.

**`enhancement_responses`** `SHOULD` — **Met**

> The project SHOULD respond to a majority (>50%) of enhancement requests in the last 2-12 months (inclusive).

All enhancement requests are tracked as labelled issues and triaged; none is unanswered.

**`report_archive`** `MUST` (URL required) — **Met**

> The project MUST have a publicly available archive for reports and responses for later searching.

GitHub Issues is publicly readable and searchable with no account. https://github.com/echo-mem/echo-mem/issues?q=is%3Aissue

### Vulnerability report process

**`vulnerability_report_process`** `MUST` (URL required) — **Met**

> The project MUST publish the process for reporting vulnerabilities on the project site.

SECURITY.md states the route and a response time, and says explicitly not to open a public issue. https://github.com/echo-mem/echo-mem/blob/main/SECURITY.md

**`vulnerability_report_private`** `MUST` (URL required) — **Met**

> If private vulnerability reports are supported, the project MUST include how to send the information in a way that is kept private.

GitHub private vulnerability reporting is enabled, verified through the API. It had previously been documented in SECURITY.md while switched off, which is why it is now checked rather than assumed.

**`vulnerability_report_response`** `MUST` — **N/A**

> The project's initial response time for any vulnerability report received in the last 6 months MUST be less than or equal to 14 days.

No vulnerability report has been received in the last 6 months, so there is no response time to report. Zero advisories.

## Quality

### Working build system

**`build`** `MUST` — **Met**

> If the software produced by the project requires building for use, the project MUST provide a working build system that can automatically rebuild the software from source code.

`python -m build` from pyproject.toml, with setuptools as the backend. release.yml additionally asserts the built wheel carries every migration script, counted from the source tree rather than hardcoded, because a wheel missing them installs cleanly and then cannot create its own schema.

**`build_common_tools`** `SUGGESTED` — **Met**

> It is SUGGESTED that common tools be used for building the software.

setuptools and PyPA `build`.

**`build_floss_tools`** `SHOULD` — **Met**

> The project SHOULD be buildable using only FLOSS tools.

setuptools, build, pytest and ruff are all FLOSS.

### Automated test suite

**`test`** `MUST` — **Met**

> The project MUST use at least one automated test suite that is publicly released as FLOSS (this test suite may be maintained as a separate FLOSS project). The project MUST clearly show or document how to run the test suite(s) (e.g., via a continuous integration (CI) script or via documentation in files such as BUILD.md, README.md, or CONTRIBUTING.md).

970 tests under pytest, in the repository and released under the same Apache 2.0 licence. The integration tests run against a real PostgreSQL with Apache AGE and pgvector; there are no mocked databases, because tenant isolation is a property of what the database returns.

**`test_invocation`** `SHOULD` — **Met**

> A test suite SHOULD be invocable in a standard way for that language.

`pytest`, the standard invocation for Python.

**`test_most`** `SUGGESTED` — **Unmet**

> It is SUGGESTED that the test suite cover most (or ideally all) the code branches, input fields, and functionality.

The project does not measure coverage at all: there is no pytest-cov, no coverage gate in CI, and therefore no number. 970 tests against a real database is not the same claim as branch coverage, and asserting "most" without measuring it would be exactly the unearned claim this project avoids elsewhere. SUGGESTED, so it does not block the passing badge.

**`test_continuous_integration`** `SUGGESTED` — **Met**

> It is SUGGESTED that the project implement continuous integration (where new or changed code is frequently integrated into a central code repository and automated tests are run on the result).

ci.yml runs lint and the full suite on every push to main and every pull request, building the Postgres image with AGE and pgvector itself.

### New functionality testing

**`test_policy`** `MUST` — **Met**

> The project MUST have a general policy (formal or not) that as major new functionality is added to the software produced by the project, tests of that functionality should be added to an automated test suite.

CONTRIBUTING.md: "Every new codepath needs a test." It also distinguishes what gets an integration test from what gets a unit test.

**`tests_are_added`** `MUST` — **Met**

> The project MUST have evidence that the test_policy for adding tests has been adhered to in the most recent major changes to the software produced by the project.

Held in the most recent changes: #150 added tests/unit/test_readme_claims.py, #164 added tests/unit/test_no_model_on_the_write_path.py with three guards, and the graph-hop annotation fix added three tests to tests/integration/test_relevance_fields.py. Each was verified to fail before the change.

**`tests_documented_added`** `SUGGESTED` — **Met**

> It is SUGGESTED that this policy on adding tests (see test_policy) be documented in the instructions for change proposals.

The policy is in CONTRIBUTING.md, which is the contribution instructions.

### Warning flags

**`warnings`** `MUST` — **Met**

> The project MUST enable one or more compiler warning flags, a "safe" language mode, or use a separate "linter" tool to look for code quality errors or common simple mistakes, if there is at least one FLOSS tool that can implement this criterion in the selected language.

`ruff check .` runs in CI on every push and pull request and fails the build.

**`warnings_fixed`** `MUST` — **Met**

> The project MUST address warnings.

CI is a required status check on main, so a ruff finding blocks the merge rather than being recorded and ignored.

**`warnings_strict`** `SUGGESTED` — **Unmet**

> It is SUGGESTED that projects be maximally strict with warnings in the software produced by the project, where practical.

ruff runs with close to its default rule set and a 100 character line length; there is no type checker, so no mypy or pyright strict mode. Honest answer is not maximally strict. SUGGESTED.

## Security

### Secure development knowledge

**`know_secure_design`** `MUST` — **Met**

> The project MUST have at least one primary developer who knows how to design secure software. (See ‘details’ for the exact requirements.)

CONFIRM THIS YOURSELF. Suggested wording: the sole maintainer applies least privilege (every GitHub Actions workflow declares read-only `permissions` at the top level and a job widens it only for itself), minimises attack surface (the provider key is read from the environment inside the one module that calls out and is deliberately NOT wired into infra/config.Config, so the server cannot reach it), and does not rely on secrecy of design. Tenant isolation is tested as a property of what the database returns rather than asserted, and docs/ERASURE.md and docs/COMPLIANCE.md record the reasoning behind the security relevant choices.

**`know_common_errors`** `MUST` — **Met**

> At least one of the project's primary developers MUST know of common kinds of errors that lead to vulnerabilities in this kind of software, as well as at least one method to counter or mitigate each of them.

CONFIRM THIS YOURSELF. Suggested wording: yes, and the repository is the evidence rather than a claim. SQL is parameterised throughout; the graph traversal was rewritten off an unbounded Cypher pattern after it was measured at 58 seconds, which is the denial of service shape; tenant scoping is enforced in the query and tested by asserting another scope's content does not leak rather than by asserting a filter string; and a null in a relevance field that led a model to invent an answer is documented in tests/integration/test_relevance_fields.py as a failure class, not a bug that was quietly fixed.

### Use basic good cryptographic practices

**`crypto_published`** `MUST` — **Met**

> The software produced by the project MUST use, by default, only cryptographic protocols and algorithms that are publicly published and reviewed by experts (if cryptographic protocols and algorithms are used).

The engine's whole cryptographic surface is four lines, all standard library on OpenSSL: HMAC-SHA256 for the erasure subject digest (erasure.py:101), SHA-256 for a capture content hash (ingestion/capture.py:25) and an eval digest (eval/external.py:541), and `secrets.token_urlsafe` for the generated Postgres password (cli/quickstart.py:52). All published and reviewed. Of those only the HMAC and the token are security mechanisms; the two SHA-256 uses are deduplication and identity keys.

**`crypto_call`** `SHOULD` — **Met**

> If the software produced by the project is an application or library, and its primary purpose is not to implement cryptography, then it SHOULD only call on software specifically designed to implement cryptographic functions; it SHOULD NOT re-implement its own.

Nothing is implemented from primitives. `hmac`, `hashlib` and `secrets` from the Python standard library are the only things used.

**`crypto_floss`** `MUST` — **Met**

> All functionality in the software produced by the project that depends on cryptography MUST be implementable using FLOSS.

Python's standard library, on OpenSSL. All FLOSS.

**`crypto_keylength`** `MUST` — **Met**

> The security mechanisms within the software produced by the project MUST use default keylengths that at least meet the NIST minimum requirements through the year 2030 (as stated in 2012). It MUST be possible to configure the software so that smaller keylengths are completely disabled.

HMAC-SHA256, a 256 bit construction. The one generated secret, the Postgres password in cli/quickstart.py, is `secrets.token_urlsafe(24)`, 192 bits of entropy.

**`crypto_working`** `MUST` — **Met**

> The default security mechanisms within the software produced by the project MUST NOT depend on broken cryptographic algorithms (e.g., MD4, MD5, single DES, RC4, Dual_EC_DRBG), or use cipher modes that are inappropriate to the context, unless they are necessary to implement an interoperable protocol (where the protocol implemented is the most recent version of that standard broadly supported by the network ecosystem, that ecosystem requires the use of such an algorithm or mode, and that ecosystem does not offer any more secure alternative). The documentation MUST describe any relevant security risks and any known mitigations if these broken algorithms or modes are necessary for an interoperable protocol.

No security mechanism depends on a broken algorithm. One use of MD5 exists and is worth naming rather than letting a reviewer find it: `ORDER BY md5(edge_id::text)` in retrieval/query_memory.py, which draws a stable arbitrary sample for the adaptive floor. It replaced `ORDER BY random()`, which redrew on every call and made identical queries return different answers; MD5 is doing the job of a deterministic shuffle, not of a hash anyone trusts. No SHA-1, DES, RC4 or Dual_EC_DRBG anywhere.

**`crypto_weaknesses`** `SHOULD` — **Met**

> The default security mechanisms within the software produced by the project SHOULD NOT depend on cryptographic algorithms or modes with known serious weaknesses (e.g., the SHA-1 cryptographic hash algorithm or the CBC mode in SSH).

SHA-256 and HMAC-SHA256 have no known practical weakness.

**`crypto_pfs`** `SHOULD` — **N/A**

> The security mechanisms within the software produced by the project SHOULD implement perfect forward secrecy for key agreement protocols so a session key derived from a set of long-term keys cannot be compromised if one of the long-term keys is compromised in the future.

The engine implements no network security mechanism of its own. It connects to PostgreSQL, where TLS and its cipher suites are the operator's server configuration, not something this software selects.

**`crypto_password_storage`** `MUST` — **N/A**

> If the software produced by the project causes the storing of passwords for authentication of external users, the passwords MUST be stored as iterated hashes with a per-user salt by using a key stretching (iterated) algorithm (e.g., Argon2id, Bcrypt, Scrypt, or PBKDF2). See also OWASP Password Storage Cheat Sheet.

The engine stores no passwords for authenticating external users. It has no accounts and no authentication surface. The password in cli/quickstart.py is a PostgreSQL credential it generates for the operator's own local database and does not store a hash of. The hosted service, which does authenticate users, is a separate codebase and stores Argon2 hashes.

**`crypto_random`** `MUST` — **Met**

> The security mechanisms within the software produced by the project MUST generate all cryptographic keys and nonces using a cryptographically secure random number generator, and MUST NOT do so using generators that are cryptographically insecure.

The one security relevant value, the generated database password, uses `secrets.token_urlsafe`, which is a CSPRNG. `random.Random(seed)` appears in graph/communities.py, cli/calibrate.py and eval/independent.py, and none is a security mechanism: each is a deliberately SEEDED sample so a benchmark scores the same thing twice, which is the opposite requirement.

### Secured delivery against man-in-the-middle (MITM) attacks

**`delivery_mitm`** `MUST` — **Met**

> The project MUST use a delivery mechanism that counters MITM attacks. Using https or ssh+scp is acceptable.

pip installs from PyPI over HTTPS, and git over HTTPS or SSH. Beyond transport, every release is built by GitHub Actions through PyPI Trusted Publishing, which issues a Sigstore attestation binding each artifact to the workflow that produced it. No API token exists to leak and nothing is signed by hand.

**`delivery_unsigned`** `MUST` — **Met**

> A cryptographic hash (e.g., a sha1sum) MUST NOT be retrieved over http and used without checking for a cryptographic signature.

No hash is fetched over http anywhere. Every GitHub Action is pinned to a commit SHA rather than a tag, so an action owner cannot repoint a version underneath the build, and Dependabot keeps those pins current.

### Publicly known vulnerabilities fixed

**`vulnerabilities_fixed_60_days`** `MUST` — **Met**

> There MUST be no unpatched vulnerabilities of medium or higher severity that have been publicly known for more than 60 days.

No vulnerability of any severity is known. Zero published advisories, and zero open code scanning alerts.

**`vulnerabilities_critical_fixed`** `SHOULD` — **Met**

> Projects SHOULD fix all critical vulnerabilities rapidly after they are reported.

None has been reported. The route and the stated response time are in SECURITY.md.

### Other security issues

**`no_leaked_credentials`** `MUST` — **Met**

> The public repositories MUST NOT leak a valid private credential (e.g., a working password or private key) that is intended to limit public access.

GitHub secret scanning and push protection are both enabled, verified through the API. The one secret the build needs, the MCP registry DNS signing key, is a repository secret; publishing uses OIDC rather than a stored token, so there is no PyPI credential in the repository to leak.

## Analysis

### Static code analysis

**`static_analysis`** `MUST` (justification required) — **Met**

> At least one static code analysis tool (beyond compiler warnings and "safe" language modes) MUST be applied to any proposed major production release of the software before its release, if there is at least one FLOSS tool that implements this criterion in the selected language.

CodeQL with the `security-extended` query suite, on every push to main, every pull request, and weekly. ruff also runs in CI but is a linter and is not counted here as security analysis.

**`static_analysis_common_vulnerabilities`** `SUGGESTED` — **Met**

> It is SUGGESTED that at least one of the static analysis tools used for the static_analysis criterion include rules or approaches to look for common vulnerabilities in the analyzed language or environment.

`security-extended` is CodeQL's broader security suite and covers the common CWE classes rather than the default subset.

**`static_analysis_fixed`** `MUST` — **Met**

> All medium and higher severity exploitable vulnerabilities discovered with static code analysis MUST be fixed in a timely way after they are confirmed.

Zero open alerts. Two were open and both were `py/incomplete-url-substring-sanitization` at high severity, in tests/unit/test_connect.py and tests/unit/test_quickstart.py. Both are assertions that a help or error message mentions api.echo-mem.com: nothing is parsed, trusted or used to authorise anything, and neither is exploitable. They are dismissed individually as false positives with that reason recorded, rather than excluded with a paths-ignore, so test code stays in scope for real findings.

**`static_analysis_often`** `SUGGESTED` — **Met**

> It is SUGGESTED that static source code analysis occur on every commit or at least daily.

Every push, every pull request, and weekly on a schedule.

### Dynamic code analysis

**`dynamic_analysis`** `SUGGESTED` — **Unmet**

> It is SUGGESTED that at least one dynamic analysis tool be applied to any proposed major production release of the software before its release.

No fuzzer, no sanitizer and no dynamic analysis tool is applied before a release. For a pure Python project reading from PostgreSQL the usual candidates fit awkwardly, but nothing is being run and the answer is no. SUGGESTED.

**`dynamic_analysis_unsafe`** `SUGGESTED` — **N/A**

> It is SUGGESTED that if the software produced by the project includes software written using a memory-unsafe language (e.g., C or C++), then at least one dynamic tool (e.g., a fuzzer or web application scanner) be routinely used in combination with a mechanism to detect memory safety problems such as buffer overwrites. If the project does not produce software written in a memory-unsafe language, choose "not applicable" (N/A).

The engine is pure Python and produces no memory-unsafe code. Its C extensions arrive through psycopg[binary], which is a dependency rather than software produced by this project.

**`dynamic_analysis_enable_assertions`** `SUGGESTED` — **Met**

> It is SUGGESTED that the project use a configuration for at least some dynamic analysis (such as testing or fuzzing) which enables many assertions. In many cases these assertions should not be enabled in production builds.

The test suite runs under pytest with assertions enabled, which is how every one of the 970 tests checks anything. The integration suite additionally runs against a real PostgreSQL with Apache AGE rather than a stub, so database level constraints are genuinely exercised.

**`dynamic_analysis_fixed`** `MUST` — **N/A**

> All medium and higher severity exploitable vulnerabilities discovered with dynamic code analysis MUST be fixed in a timely way after they are confirmed.

No dynamic analysis tool is applied, so none has discovered anything. See dynamic_analysis.

