"""The inference provider, which is absent by default and must stay optional.

The central claim this product rests on is not "no model anywhere". It is that
storing a memory never depends on one, and that retrieving one never depends on
one either. Everything in this package works with no provider configured, and
that is not a degraded mode - it is the mode the engine is tested, benchmarked
and sold in.

So the default is a provider that cannot do anything, and it is a class rather
than `None` so that every call site has one object with one shape. A `None`
default means an `if provider is not None` at every use, and the one that gets
forgotten is a crash on the path that was supposed to be optional.

## What a provider may and may not do

May: propose. A provider can look at facts already stored and suggest a
conclusion, and that conclusion arrives as `kind="inferred"` with
`basis="correlational"` - never `verified_causal`, which requires a verifier
asserting the link and naming a method, and a model is not one. Nothing a
provider produces can be promoted by producing more of it.

May not: be on a read or write hot path. Not as a fallback, not as a cache
warmer, not "only when the local embedder is unavailable". The reason is
specific rather than philosophical: a write path that calls a provider fails
when the provider does, bills per write, and makes the latency of storing a
memory depend on somebody else's capacity. This project has already measured
what the alternative is worth - 6 seconds a turn against a competitor that
ingests through a model - and the answer is that writes must not wait.

`cli/llm.py` is the other provider in this codebase and the boundary is the
same one, stated from the other side: it is for offline batch commands a person
types, it is deliberately not wired into `infra.config.Config`, and nothing
under ingestion/ or retrieval/ imports it.

## Nothing calls `load()`

This module states a boundary; it does not yet enforce one. No code in `src/`
consults it, so `ECHO_MEMORY_INFERENCE_PROVIDER=openai` raises nothing at
startup and is simply ignored - the operator gets the no-provider behaviour
they would have got anyway, without being told their variable did nothing.
The claim the package actually keeps is structural and is checked elsewhere:
`tests/unit/test_no_model_on_the_write_path.py` pins every call site that
reaches a model to the commands the README names, and asserts `cli/llm.py` is
still the only module holding an outbound request. `UnknownProvider` becomes a
live guard the first time a caller asks this module what is configured.
"""

from __future__ import annotations

import os
from typing import Any

#: The one variable. Unset means NullProvider, which is the default and the
#: configuration every feature must work under.
PROVIDER_VAR = "ECHO_MEMORY_INFERENCE_PROVIDER"

#: Values that explicitly ask for nothing. Accepted so that an operator can
#: write the variable down as off rather than having to delete it, which is the
#: difference between a config that says "no provider" and one that forgot.
EXPLICIT_NONE = frozenset({"", "null", "none", "off"})


class UnknownProvider(Exception):
    """A provider was named and this build has no implementation of it.

    Raised rather than falling back to NullProvider. A silent fallback would
    make a configured provider indistinguishable from an absent one: the
    operator sets the variable, nothing happens, no inferred candidates ever
    appear, and the honest conclusion available from the outside is that the
    feature does not work.
    """


class NullProvider:
    """The default, and the one every feature must work through.

    `available` is False and there is nothing else on it. A caller asks
    `provider.available` and takes the non-inference path, which is the path
    with the measurements behind it.
    """

    available = False

    name = "null"

    def __repr__(self) -> str:
        return "NullProvider()"


def load(env: dict[str, str] | None = None) -> Any:
    """The configured provider, or NullProvider when none is named.

    `env` is a parameter rather than a read of `os.environ` inside, for the
    same reason `envelope.parse` takes `now`: it makes the whole of the policy
    testable without touching the process environment, and `load({})` is then
    a real assertion about the default rather than an assertion about whatever
    the test runner happened to export.
    """
    source = os.environ if env is None else env
    named = str(source.get(PROVIDER_VAR, "") or "").strip().lower()
    if named in EXPLICIT_NONE:
        return NullProvider()
    raise UnknownProvider(
        f"{PROVIDER_VAR}={named!r} names no provider this build implements. "
        f"There is currently no inference provider: the engine's read and "
        f"write paths call no model, and a provider may only propose "
        f"correlational candidates off to one side. Unset the variable, or set "
        f"it to one of {sorted(EXPLICIT_NONE - {''})}."
    )
