"""Signed, redacted, one directional knowledge bundles.

A bundle is a file. One deployment builds it from one scope, redacts it,
signs it and hands it over; another deployment verifies it and writes its
facts into a quarantine scope that ordinary retrieval is never given. There
is no endpoint one store calls on another, no shared trust root, and no live
protocol to get wrong.

The modules, in the order a bundle passes through them:

    redact   which claims may leave, and what is stripped from the ones that do
    bundle   the wire format, the canonical bytes and the content digest
    sign     a signature over the manifest, verified against an explicit anchor
    importer verification, replay refusal, quarantine, promotion and revocation

`cli/exchange.py` is the only surface that drives them. None of this is an
MCP tool, for the reason `cli/forget.py` records: granting trust and handing
data to another organisation must not be one prompt injection away from any
agent holding a key.
"""
