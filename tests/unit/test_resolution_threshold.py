"""LOW_THRESHOLD is configurable, and the default has not moved.

The bar depends on what an entity name is in a given store: prose names
separate at 0.45, short technical identifiers sharing a prefix do not. A store
that knows its own names should be able to raise it without waiting for a
release. What must not happen is the default drifting by accident, so it is
asserted here as a number."""

import importlib

import pytest

from echo_memory.ingestion import resolution


def _reloaded(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("ECHO_MEMORY_RESOLUTION_LOW", raising=False)
    else:
        monkeypatch.setenv("ECHO_MEMORY_RESOLUTION_LOW", value)
    return importlib.reload(resolution)


@pytest.fixture(autouse=True)
def _restore():
    yield
    importlib.reload(resolution)


def test_default_is_still_0_45(monkeypatch):
    assert _reloaded(monkeypatch, None).LOW_THRESHOLD == 0.45


def test_the_environment_can_raise_it(monkeypatch):
    assert _reloaded(monkeypatch, "0.8").LOW_THRESHOLD == 0.8


def test_high_threshold_is_not_configurable():
    """Deliberate. LOW_THRESHOLD decides whether to ask; HIGH_THRESHOLD is the
    silent-merge boundary, where a wrong number joins two entities with nobody
    watching. Calibration governs that one, not an environment variable."""
    assert resolution.HIGH_THRESHOLD == 0.92
