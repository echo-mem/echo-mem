# Contributing to Echo Memory

Thanks for considering a contribution. This project is early and staged; please read
[`docs/designs/echo-memory-design.md`](docs/designs/echo-memory-design.md) before
proposing anything, so your PR fits the current build phase (v1a → v1b → v1.1, in that
order, each gated on the previous one's exit criteria).

## The contributor agreement

The first pull request you open will get a bot comment asking you to sign the
[Contributor License Agreement](CLA.md). It is a one-line reply in the PR thread, and it
is asked once ever, not once per PR.

Read [`CLA.md`](CLA.md) for what it grants and why. The short version: you keep your
copyright, everything you contribute stays available under the project's licence, and
every version converts to Apache-2.0 four years after publication, and
the agreement adds the right to also distribute your contribution under other terms —
because a paid hosted edition is planned, and some team features may ship
source-available. Asking now is the version of that conversation where nobody is
surprised later.

## Before you start

- **Check the PR plan** in the design doc for the current dependency-ordered sequence.
  Work that jumps ahead of a gate (e.g. v1b work before v1a's exit criteria are met)
  will likely be asked to wait, not because the idea is bad but because the point of the
  staging is to validate each layer before building the next.
- **Open an issue before a large PR.** Small fixes and docs improvements don't need
  this; anything touching schema, retrieval logic, or the MCP contract should get a
  quick discussion first.

## Development setup

See [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md).

## Commit messages

Short, imperative, and specific. `fix entity resolution off-by-one in threshold check`,
not `fix bug`. No AI-tool attribution in commit messages or co-author trailers.

## Pull requests

- Keep PRs scoped to one thing; this project intentionally uses small, independently
  reviewable PRs rather than large landings (see the design doc's PR plan).
- Every new codepath needs a test. The server itself has no LLM-judgment code path to
  eval-test: extraction, entity-resolution confirmation, and `causal_hint` classification
  all happen in the calling agent, not this codebase (see the design doc's MCP tool
  contract). Deterministic server-side logic (resolution threshold branching, RRF fusion,
  audit log transactions) gets a mocked unit test. The `eval` pytest marker is reserved
  for future retrieval-quality evals (e.g. "does `query_memory` rank a labeled query set
  well"), not in use yet; see `docs/DEVELOPMENT.md`'s test-strategy note.
- CI must pass before merge.
- **Never commit to `main`.** Branch, test, open a PR, merge. `scripts/git-hooks/pre-commit`
  refuses commits made on `main`; install it with
  `git config core.hooksPath scripts/git-hooks`. The hook cannot protect a branch it is
  not installed on, so branch protection is the real enforcement and the hook is the
  early warning.

## Code of Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).

## Reporting a bug

Include the version or commit, what you ran, what you expected, and what happened. If it
involves memory content, **redact first** — a memory graph routinely contains hostnames,
account identifiers, and client names, and an issue is public. `echo-memory why <fact-id>`
and `echo-memory status` are usually enough to describe a problem without pasting the
graph itself.

## Reporting security issues

See [`SECURITY.md`](SECURITY.md). Do not open a public issue for a security
vulnerability.
