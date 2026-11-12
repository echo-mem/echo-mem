"""The default is no provider, and that is the configuration everything else
is tested in.

One assertion matters more than the others: `load({})` returns a NullProvider.
Not because the code is hard, but because "works with no model configured" is
the product's central claim and a default that quietly changed would break it
without breaking anything that looks like a test.
"""

from __future__ import annotations

import pytest

from echo_memory.infra.provider import (
    PROVIDER_VAR,
    NullProvider,
    UnknownProvider,
    load,
)


def test_nothing_configured_gives_the_null_provider():
    provider = load({})
    assert isinstance(provider, NullProvider)
    assert provider.available is False


def test_the_null_provider_reports_unavailable_rather_than_raising():
    """Callers branch on `available`. A default that raised on being asked
    would push a try/except into every call site, and the one that gets
    forgotten is on the path that was supposed to be optional."""
    assert NullProvider.available is False


@pytest.mark.parametrize("value", ["", "none", "null", "off", "  OFF  "])
def test_a_provider_explicitly_turned_off_is_the_null_provider(value):
    """Accepted so that a config can say "off" rather than having to omit the
    line, which is the difference between deciding and forgetting."""
    assert isinstance(load({PROVIDER_VAR: value}), NullProvider)


def test_an_unimplemented_provider_is_refused_rather_than_ignored():
    """A silent fallback would make a configured provider indistinguishable
    from an absent one, and the only conclusion available from outside would be
    that the feature does not work."""
    with pytest.raises(UnknownProvider) as e:
        load({PROVIDER_VAR: "anthropic"})
    assert PROVIDER_VAR in str(e.value)


def test_the_refusal_says_the_engine_calls_no_model():
    """The message is where an operator finds out that there is nothing to
    configure yet, rather than concluding their key was wrong."""
    with pytest.raises(UnknownProvider) as e:
        load({PROVIDER_VAR: "openai"})
    assert "no model" in str(e.value)
