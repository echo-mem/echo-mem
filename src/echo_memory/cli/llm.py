"""One model call, for offline CLI batch commands only.

The engine's write path makes no model call, and this module does not change
that. Nothing under ingestion/ or retrieval/ imports it, and it is deliberately
NOT wired into infra/config.Config: the server builds a Config at startup, so a
provider key living there would be a key the server could reach. Read from the
environment here, where the only callers are commands a person types.

The distinction the product's central claim rests on is not "no model anywhere",
it is that storing a memory never depends on one. An operator running a batch
pass over facts already stored is a different act: it is explicit, it happens
once, it can be refused, and the memory it reads was written without a model in
the loop.

Anthropic's messages shape, matching echo-cloud's ask.py, which is the other
place in this codebase that calls a provider. Same three variables under a
different prefix, and the same both-or-neither rule: a key with no model names
nothing to call, and a model with no key cannot call it.

A transport failure raises rather than returning empty. The cloud's answer box
degrades to retrieval-only on a provider outage because the facts are already
on their way to the reader; a batch pass has no such fallback, and a run that
read a timeout as "this sentence states no cause" would write that verdict
down permanently.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

DEFAULT_BASE_URL = "https://api.anthropic.com"
HTTP_TIMEOUT = 60
ANTHROPIC_VERSION = "2023-06-01"

KEY_VAR = "ECHO_MEMORY_LLM_API_KEY"
MODEL_VAR = "ECHO_MEMORY_LLM_MODEL"
BASE_URL_VAR = "ECHO_MEMORY_LLM_BASE_URL"


class ModelUnavailable(Exception):
    """No provider is configured, or the configured one could not be reached."""


@dataclass(frozen=True)
class Provider:
    api_key: str
    model: str
    base_url: str = DEFAULT_BASE_URL

    @property
    def endpoint(self) -> str:
        return f"{(self.base_url or DEFAULT_BASE_URL).rstrip('/')}/v1/messages"


def configured(env: dict | None = None) -> bool:
    env = env if env is not None else os.environ
    return bool(env.get(KEY_VAR, "").strip() and env.get(MODEL_VAR, "").strip())


def provider_from_env(env: dict | None = None) -> Provider:
    """The caller's own model, named by the caller.

    No default model. A command that picked one would be choosing how much of
    someone else's money to spend, and the choice depends on the size of the
    store it is about to read.
    """
    env = env if env is not None else os.environ
    key = env.get(KEY_VAR, "").strip()
    model = env.get(MODEL_VAR, "").strip()
    base = env.get(BASE_URL_VAR, "").strip().rstrip("/")
    if not key and not model:
        raise ModelUnavailable(
            f"no model configured. This command reads fact text with your own "
            f"model: set {KEY_VAR} and {MODEL_VAR} (and {BASE_URL_VAR} for a "
            f"provider other than Anthropic)."
        )
    if bool(key) != bool(model):
        missing = MODEL_VAR if key else KEY_VAR
        raise ModelUnavailable(
            f"{missing} is not set. {KEY_VAR} and {MODEL_VAR} go together: "
            f"one without the other cannot make a call."
        )
    return Provider(api_key=key, model=model, base_url=base or DEFAULT_BASE_URL)


def complete(provider: Provider, system: str, prompt: str, max_tokens: int = 2000) -> str:
    """One request, one response, no retry.

    No retry on purpose: the caller is a batched loop that records what it
    learned per batch, so the resumable thing already exists and re-running the
    command is the retry. A retry here would double a bill quietly instead.
    """
    payload = json.dumps({
        "model": provider.model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    request = urllib.request.Request(
        provider.endpoint,
        data=payload,
        headers={
            "content-type": "application/json",
            "x-api-key": provider.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # The status and the provider's own message, because "it failed" sends
        # an operator to the wrong place: an expired key, a model name that does
        # not exist and a rate limit all look identical without it.
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:500]
        except Exception:  # noqa: BLE001
            pass
        raise ModelUnavailable(f"{provider.model} returned HTTP {e.code}: {detail}") from e
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        raise ModelUnavailable(f"could not reach {provider.endpoint}: {e}") from e

    parts = body.get("content") or []
    return "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
