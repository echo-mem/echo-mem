"""The registry drift check has to tell the two mistakes apart.

A check that reports "something is wrong with the registry" when the real
problem is a tagged release with a stale manifest sends somebody to publish
something that is already published. These pin that each mismatch names
itself, and that a network failure is not reported as a version mismatch.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "check_registry_is_current",
    Path(__file__).resolve().parents[2] / "scripts" / "check_registry_is_current.py",
)
check = importlib.util.module_from_spec(_spec)
sys.modules["check_registry_is_current"] = check
_spec.loader.exec_module(check)


def _registry(version: str | None, name: str = check.SERVER_NAME) -> dict:
    if version is None:
        return {"servers": []}
    return {"servers": [{
        "server": {"name": name, "version": version},
        "_meta": {"io.modelcontextprotocol.registry/official": {"isLatest": True}},
    }]}


@pytest.fixture
def wire(monkeypatch, tmp_path):
    """Drive the script's three inputs without touching the network."""
    def go(*, manifest: str, pypi: str, registry: str | None):
        (tmp_path / "server.json").write_text(f'{{"version": "{manifest}"}}')
        monkeypatch.setattr(check, "ROOT", tmp_path)

        def fake_get(url, what):
            return _registry(registry) if "registry" in url else {"info": {"version": pypi}}

        monkeypatch.setattr(check, "_get", fake_get)
        return check.main()
    return go


def test_agreement_passes(wire, capsys):
    assert wire(manifest="0.5.5", pypi="0.5.5", registry="0.5.5") == 0
    assert "agree" in capsys.readouterr().out


def test_a_stale_registry_says_to_publish(wire, capsys):
    """The real case on 2026-10-26."""
    assert wire(manifest="0.5.5", pypi="0.5.5", registry="0.5.3") == 1

    err = capsys.readouterr().err
    assert "registry serves 0.5.3" in err
    assert "publish step failed or was skipped" in err


def test_a_stale_manifest_is_a_different_message(wire, capsys):
    """Tagged a release without updating server.json. Publishing would not
    fix it, so the output must not suggest publishing as the whole answer."""
    assert wire(manifest="0.5.4", pypi="0.5.5", registry="0.5.4") == 1

    err = capsys.readouterr().err
    assert "server.json says 0.5.4 and PyPI has 0.5.5" in err


def test_an_unlisted_server_is_not_reported_as_a_version_mismatch(wire, capsys):
    assert wire(manifest="0.5.5", pypi="0.5.5", registry=None) == 1

    assert "lists no version" in capsys.readouterr().err


def test_both_mistakes_at_once_are_both_reported(wire, capsys):
    """So one run fixes everything rather than one thing per day."""
    assert wire(manifest="0.5.4", pypi="0.5.5", registry="0.5.3") == 1

    err = capsys.readouterr().err
    assert "server.json says 0.5.4" in err
    assert "registry serves 0.5.3" in err


def test_a_network_failure_is_not_a_version_mismatch(monkeypatch, tmp_path):
    """Reporting unreachable as stale sends somebody to republish a release
    that is already live."""
    (tmp_path / "server.json").write_text('{"version": "0.5.5"}')
    monkeypatch.setattr(check, "ROOT", tmp_path)

    def boom(url, what):
        raise SystemExit(f"could not reach {what}: timed out")

    monkeypatch.setattr(check, "_get", boom)

    with pytest.raises(SystemExit) as e:
        check.main()

    assert "could not reach" in str(e.value)


def test_another_server_matching_the_search_is_ignored(monkeypatch, tmp_path):
    """The registry search is a substring match, so somebody else's
    echo-mem-something must not be mistaken for ours."""
    (tmp_path / "server.json").write_text('{"version": "0.5.5"}')
    monkeypatch.setattr(check, "ROOT", tmp_path)
    monkeypatch.setattr(check, "_get", lambda url, what: (
        _registry("9.9.9", name="io.github.somebody/echo-mem-clone")
        if "registry" in url else {"info": {"version": "0.5.5"}}
    ))

    assert check.main() == 1
    assert check.registry_latest() is None
