# Security Policy

## Reporting a vulnerability

Please **do not** open a public GitHub issue, and please do not post it anywhere
public before we have had a chance to respond.

**The engine**, this repository, including the MCP server, CLI and dashboard:
use [GitHub's private vulnerability reporting](https://github.com/echo-mem/echo-mem/security/advisories/new).

**The hosted service** at `app.echo-mem.com` and `api.echo-mem.com`: email
<hello@echo-mem.com> with **security** in the subject, because the service is
not in this repository and an advisory here would be the wrong place for it.

Either way, please include:

- what the vulnerability is and what it lets somebody do
- steps to reproduce it
- any logs, proof of concept or configuration that help

We acknowledge within **72 hours** and tell you what we intend to do and when.

## Testing the hosted service

The hosted service holds other people's memory, on one server. So, as the
[security page](https://www.echo-mem.com/security) also says: please do not run
automated scanners against it, do not access an account that is not yours, and
do not degrade the service for anyone else while testing.

Against your own self hosted install, on your own machine, test whatever you
like. That is the whole point of the source being readable, and it is the
easiest way to look at the engine without touching anybody's data.

If you want to test the hosted service specifically, write first and we will
agree a scope. Authorisation is in writing or it does not exist.

## What we offer

Credit in the release notes, if you want it. **There is no bug bounty.** This
is a small project and implying a payout process exists when it does not would
be dishonest. Saying so plainly up front seems better than letting somebody
spend a weekend expecting one.

## Supported versions

The latest release on PyPI is supported; at the time of writing that is
**0.5.5**. Fixes go to the newest version rather than being backported, so
upgrading is the remedy.

Versions up to and including **0.4.1** remain under Apache 2.0 permanently (see
[`LICENSE-APACHE-2.0`](LICENSE-APACHE-2.0)) and **0.5.0** onward are BSL 1.1.
That is a licensing statement, not a support one: no released version older
than the current one receives fixes.

## What this project does and does not claim

No SOC 2, ISO 27001 or HIPAA certification, and no third party audit has been
performed. The [security page](https://www.echo-mem.com/security) lists what is
in place and what is missing, in both directions, and is the more detailed
document.
