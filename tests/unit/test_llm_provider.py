"""The provider config for offline batch commands: named by the caller, or absent.

No default model and no shared key. A command that picked a model would be
choosing how much of someone else's money to spend, and the both-or-neither rule
is the cloud's: a key with no model names nothing to call, a model with no key
cannot call it, and either alone gives a command that fails at the last moment
instead of the first.
"""

from __future__ import annotations

import pytest

from echo_memory.cli.llm import (
    DEFAULT_BASE_URL,
    KEY_VAR,
    MODEL_VAR,
    ModelUnavailable,
    Provider,
    configured,
    provider_from_env,
)


def test_nothing_configured_names_both_variables():
    with pytest.raises(ModelUnavailable) as e:
        provider_from_env({})
    # The message is the whole help a person gets, so it has to say what to set.
    assert KEY_VAR in str(e.value) and MODEL_VAR in str(e.value)


def test_a_key_with_no_model_is_refused_before_any_call():
    with pytest.raises(ModelUnavailable) as e:
        provider_from_env({KEY_VAR: "sk-test"})
    assert MODEL_VAR in str(e.value)


def test_a_model_with_no_key_is_refused_before_any_call():
    with pytest.raises(ModelUnavailable) as e:
        provider_from_env({MODEL_VAR: "some-model"})
    assert KEY_VAR in str(e.value)


def test_both_set_gives_the_callers_own_model_and_the_default_endpoint():
    provider = provider_from_env({KEY_VAR: "sk-test", MODEL_VAR: "some-model"})
    assert provider.model == "some-model"
    assert provider.endpoint == f"{DEFAULT_BASE_URL}/v1/messages"


def test_a_base_url_override_keeps_one_trailing_slash_out_of_the_path():
    provider = Provider("sk-test", "some-model", "https://gateway.internal/")
    assert provider.endpoint == "https://gateway.internal/v1/messages"


def test_configured_is_false_until_both_are_present():
    assert not configured({})
    assert not configured({KEY_VAR: "sk-test"})
    assert configured({KEY_VAR: "sk-test", MODEL_VAR: "some-model"})
