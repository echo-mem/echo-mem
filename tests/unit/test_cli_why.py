from unittest.mock import Mock

import pytest

from echo_memory.cli import main as cli
from echo_memory.cli.why import render_entry, render_history
from echo_memory.infra.config import Config


def test_render_entry_includes_timestamp_and_summary():
    entry = {"timestamp": "2026-08-21T00:00:00+00:00", "summary": "created: uses Postgres"}
    assert render_entry(entry) == "2026-08-21T00:00:00+00:00: created: uses Postgres"


def test_render_history_empty():
    assert render_history("123", []) == "No audit history found for fact '123'."


def test_render_history_multiple_entries_in_order():
    entries = [
        {"timestamp": "t1", "summary": "created: fact A"},
        {"timestamp": "t2", "summary": "invalidated 'fact A', superseded by 'fact B'"},
    ]
    result = render_history("123", entries)
    assert "History for fact 123:" in result
    lines = result.splitlines()
    assert lines[1] == "  t1: created: fact A"
    assert lines[2] == "  t2: invalidated 'fact A', superseded by 'fact B'"


@pytest.fixture
def why_cli(monkeypatch):
    config = Config(
        user_id="reader", agent_id="cli", solo_key="private-store", database_url="unused"
    )
    histories = {}
    conn = object()
    lookup = Mock(side_effect=lambda connection, group, fact: {
        "entries": histories.get((group, fact), [])
    })
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli, "connect", lambda url: conn)
    monkeypatch.setattr(cli, "get_fact_history", lookup)
    return config, histories, lookup


@pytest.mark.parametrize("scope,argv", [("solo", []), ("shared", ["--scope", "shared"])])
def test_why_points_to_history_in_other_scope(why_cli, capsys, scope, argv):
    config, histories, lookup = why_cli
    other = "shared" if scope == "solo" else "solo"
    histories[config.group_id(other), "123"] = [
        {"timestamp": "t1", "summary": "created: private fact content"}
    ]

    assert cli.main([*argv, "why", "123"]) == 0

    out = capsys.readouterr().out
    assert f"Fact 123 has audit history in the {other} scope, not {scope}." in out
    assert f"echo-memory --scope {other} why 123" in out
    assert "No audit history" not in out
    assert "private fact content" not in out
    assert [call.args[1:] for call in lookup.call_args_list] == [
        (config.group_id(scope), "123"), (config.group_id(other), "123")
    ]


@pytest.mark.parametrize("scope", ["solo", "shared"])
def test_why_found_in_selected_scope_does_not_probe_other_scope(why_cli, capsys, scope):
    config, histories, lookup = why_cli
    histories[config.group_id(scope), "123"] = [
        {"timestamp": "t1", "summary": "created: fact A"}
    ]

    assert cli.main(["--scope", scope, "why", "123"]) == 0

    assert capsys.readouterr().out == "History for fact 123:\n  t1: created: fact A\n"
    assert [call.args[1:] for call in lookup.call_args_list] == [(config.group_id(scope), "123")]


def test_why_missing_in_both_scopes_keeps_no_history_message(why_cli, capsys):
    config, _, lookup = why_cli

    assert cli.main(["why", "123"]) == 0

    assert capsys.readouterr().out == "No audit history found for fact '123'.\n"
    assert [call.args[1:] for call in lookup.call_args_list] == [
        (config.group_id("solo"), "123"), (config.group_id("shared"), "123")
    ]
