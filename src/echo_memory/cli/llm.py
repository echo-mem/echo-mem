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

#: The default variable prefix. A second provider is configured under another
#: prefix rather than by a second module: the benchmark's judged QA pass needs
#: two judges from DIFFERENT vendors, because agreement between two models from
#: one vendor says much less about how far a verdict depends on the judge.
DEFAULT_PREFIX = "ECHO_MEMORY_LLM"

#: Two request shapes, which is all that is needed to reach every provider
#: anybody here uses. Anthropic's /v1/messages, and the OpenAI
#: /chat/completions shape that Gemini, Grok, OpenAI and most self hosted
#: servers all speak.
ANTHROPIC = "anthropic"
OPENAI = "openai"
SHAPES = (ANTHROPIC, OPENAI)


class ModelUnavailable(Exception):
    """No provider is configured, or the configured one could not be reached."""


@dataclass(frozen=True)
class Provider:
    api_key: str
    model: str
    base_url: str = DEFAULT_BASE_URL
    #: Which wire format to speak. Not inferred from the base URL: a guess
    #: that is usually right fails confusingly on the provider it is wrong
    #: about, and the error arrives as a parse failure on a response rather
    #: than as "that is the wrong shape".
    shape: str = ANTHROPIC

    @property
    def endpoint(self) -> str:
        base = (self.base_url or DEFAULT_BASE_URL).rstrip("/")
        if self.shape == OPENAI:
            return f"{base}/chat/completions"
        return f"{base}/v1/messages"


def configured(env: dict | None = None, prefix: str = DEFAULT_PREFIX) -> bool:
    env = env if env is not None else os.environ
    return bool(
        env.get(f"{prefix}_API_KEY", "").strip()
        and env.get(f"{prefix}_MODEL", "").strip()
    )


def provider_from_env(
    env: dict | None = None,
    prefix: str = DEFAULT_PREFIX,
    default_shape: str = ANTHROPIC,
) -> Provider:
    """The caller's own model, named by the caller.

    No default model. A command that picked one would be choosing how much of
    someone else's money to spend, and the choice depends on the size of the
    store it is about to read.

    `prefix` selects the variable family, so a second provider is configured
    without a second module: ECHO_MEMORY_JUDGE2_API_KEY and friends alongside
    ECHO_MEMORY_LLM_*. `default_shape` is what a prefix means when it does not
    say, which lets the second judge default to the OpenAI shape it will
    almost certainly need while the engine's own commands keep defaulting to
    Anthropic.
    """
    env = env if env is not None else os.environ
    key_var, model_var = f"{prefix}_API_KEY", f"{prefix}_MODEL"
    base_var, shape_var = f"{prefix}_BASE_URL", f"{prefix}_SHAPE"
    key = env.get(key_var, "").strip()
    model = env.get(model_var, "").strip()
    base = env.get(base_var, "").strip().rstrip("/")
    shape = env.get(shape_var, "").strip().lower() or default_shape
    if not key and not model:
        raise ModelUnavailable(
            f"no model configured. This command reads fact text with your own "
            f"model: set {key_var} and {model_var} (and {base_var} for a "
            f"provider other than Anthropic)."
        )
    if bool(key) != bool(model):
        missing = model_var if key else key_var
        raise ModelUnavailable(
            f"{missing} is not set. {key_var} and {model_var} go together: "
            f"one without the other cannot make a call."
        )
    if shape not in SHAPES:
        raise ModelUnavailable(
            f"{shape_var}={shape!r} is not a known request shape; "
            f"use one of {', '.join(SHAPES)}."
        )
    if shape == OPENAI and not base:
        # Anthropic has a default base URL in this module; the OpenAI shape is
        # spoken by many hosts and none of them is the obvious one, so refusing
        # beats picking.
        raise ModelUnavailable(
            f"{shape_var} is {OPENAI}, so {base_var} has to say which host. "
            f"For Gemini that is "
            f"https://generativelanguage.googleapis.com/v1beta/openai"
        )
    return Provider(
        api_key=key, model=model, base_url=base or DEFAULT_BASE_URL, shape=shape
    )


def complete(provider: Provider, system: str, prompt: str, max_tokens: int = 2000) -> str:
    """One request, one response, no retry.

    No retry on purpose: the caller is a batched loop that records what it
    learned per batch, so the resumable thing already exists and re-running the
    command is the retry. A retry here would double a bill quietly instead.
    """
    if provider.shape == OPENAI:
        # The system prompt is a message with role "system" here rather than a
        # top level field, and the token cap is spelled differently. Those two
        # differences are the whole of it for a single turn completion.
        body_out = {
            "model": provider.model,
            "max_completion_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }
        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {provider.api_key}",
        }
    else:
        body_out = {
            "model": provider.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {
            "content-type": "application/json",
            "x-api-key": provider.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        }
    request = urllib.request.Request(
        provider.endpoint,
        data=json.dumps(body_out).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # The status and the provider's own message, because "it failed" sends
        # an operator to the wrong place: an expired key, a model name that does
        # not exist and a rate limit all look identical without it.
        try:
            detail = e.read().decode("utf-8")[:500]
        except Exception as read_failure:  # noqa: BLE001
            # The body is optional context on an error that is already being
            # raised; losing it must not replace the status code with a
            # traceback about failing to read it.
            detail = f"(body unreadable: {read_failure})"
        raise ModelUnavailable(f"{provider.model} returned HTTP {e.code}: {detail}") from e
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        raise ModelUnavailable(f"could not reach {provider.endpoint}: {e}") from e

    if provider.shape == OPENAI:
        choices = body.get("choices") or []
        if not choices:
            raise ModelUnavailable(
                f"{provider.model} returned no choices: {str(body)[:300]}"
            )
        message = (choices[0] or {}).get("message") or {}
        return (message.get("content") or "").strip()
    parts = body.get("content") or []
    return "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
