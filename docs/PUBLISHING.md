# Publishing to the MCP registry

`server.json` in the repository root is the entry. It is validated against the
published schema; check it after editing:

```bash
python3 -c "import json,jsonschema,urllib.request as u; \
  jsonschema.validate(json.load(open('server.json')), \
  json.load(u.urlopen('https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json'))); \
  print('ok')"
```

Two things about it are easy to get wrong and expensive to get wrong.

**The PyPI package is `echo-mem`, not `echo-memory`.** `echo-memory` on PyPI
belongs to Textstone Labs, a different company shipping a product with the same
name since March 2026. Publishing that identifier would send every installer to
somebody else's SDK.

**`description` has a 100 character limit.** The schema enforces it and the
publish fails rather than truncating.

## Publishing

**Nothing to do. `release.yml` publishes on every tag.** The step runs after
PyPI and after the GitHub Release, authenticating by DNS, and
`scripts/check_registry_is_current.py` runs daily to say so if it ever stops.

This used to be a manual runbook, and the manual step was missed for a month:
the registry served 0.5.3 while PyPI was on 0.5.5, and nothing anywhere said
so.

### Why the name is `com.echo-mem/echo-mem`

It was `io.github.ayushcodes10/echo-mem`, a namespace belonging to a **personal
GitHub account**, so no Actions token could ever claim it and the publish could
not be automated at all. `io.github.echo-mem/*` would have worked today and
broken the next time the repository moves organisation, which has already
happened once. A domain survives that.

Verification is a TXT record on `echo-mem.com`, alongside the SPF and Google
records that were already there:

```
v=MCPv1; k=ed25519; p=<base64 public key>
```

The matching private key is the `MCP_REGISTRY_DNS_KEY` repository secret. To
rotate it, generate a new ed25519 pair, UPSERT the TXT record with **all** of
the apex values rather than only the new one, and replace the secret.

### The ordering that is not obvious

The registry verifies PyPI ownership by finding `mcp-name: com.echo-mem/echo-mem`
in the package description, which is the line at the bottom of `README.md`. It
checks the README of the version **already on PyPI**, not the one in the repo.

So the name cannot be registered until a release carrying that line has reached
PyPI first. That is why the workflow does PyPI, then the GitHub Release, then
the registry, and why the first automated publish is the first release after
the rename.

### Doing it by hand, if the workflow is ever the problem

```bash
brew install mcp-publisher     # or download from the registry's releases
mcp-publisher login dns --domain echo-mem.com --private-key <hex>
mcp-publisher publish
```

### The old listing

`io.github.ayushcodes10/echo-mem` is frozen at 0.5.3 and should be deprecated
with `mcp-publisher status`, which needs an interactive `mcp-publisher login
github` as that account. Until then it is a live listing pointing at a release
from September, and anyone who already has that name in a config keeps
resolving to it.

Several downstream directories ingest the registry on a schedule, so publishing
here propagates to listings that would otherwise each need a manual submission.
