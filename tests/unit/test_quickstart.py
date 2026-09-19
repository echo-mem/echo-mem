"""One command from nothing to a working memory.

Setup used to be five steps and the second was a wall: Echo Memory needs a
Postgres carrying both pgvector and Apache AGE, no managed provider offers AGE,
so the honest instruction was "build this Dockerfile", which compiles AGE from
source. That asks somebody who has not yet seen the product work to make a
decision about a database extension.

What these tests pin is the behaviour that makes the command safe to run rather
than the happy path: it must never take a database away from somebody who
already has one, and it must tell the two Docker failures apart, because one is
an install and the other is opening an app.
"""

from __future__ import annotations

import subprocess

import pytest

from echo_memory.cli import quickstart


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_a_missing_docker_says_what_to_install(monkeypatch):
    monkeypatch.setattr(quickstart.shutil, "which", lambda _: None)

    ok, why = quickstart.docker_available()

    assert not ok
    assert "not installed" in why
    # The reason there is a container at all, or the instruction reads as
    # arbitrary weight for a memory tool.
    assert "Apache AGE" in why


def test_docker_installed_but_stopped_is_a_different_sentence(monkeypatch):
    """One is an install and the other is opening an app. Collapsing them sends
    somebody to download something they already have."""
    monkeypatch.setattr(quickstart.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(quickstart, "_run", lambda *a, **k: _completed(returncode=1))

    ok, why = quickstart.docker_available()

    assert not ok
    assert "not running" in why
    assert "not installed" not in why


def test_an_existing_database_is_never_replaced(monkeypatch):
    """The most important line here. Somebody running this twice, or on a
    machine already holding real memory, must not lose it to a command whose
    name suggests it only sets things up."""
    calls = []

    def fake_run(args, timeout=60):
        calls.append(args)
        if args[:2] == ["docker", "inspect"]:
            return _completed(stdout="running\n")
        return _completed()

    monkeypatch.setattr(quickstart, "_run", fake_run)

    assert quickstart.start_database()[0] == "already running"
    assert not any("run" in c and "-d" in c for c in calls), "it created a second container"


def test_a_stopped_container_is_started_not_recreated(monkeypatch):
    calls = []

    def fake_run(args, timeout=60):
        calls.append(args)
        if args[:2] == ["docker", "inspect"]:
            return _completed(stdout="exited\n")
        return _completed()

    monkeypatch.setattr(quickstart, "_run", fake_run)

    assert quickstart.start_database()[0] == "restarted"
    assert ["docker", "start", quickstart.CONTAINER] in calls
    assert not any("-d" in c for c in calls), "a stopped container was recreated"


def test_a_fresh_machine_gets_a_container_with_a_named_volume(monkeypatch):
    """The volume is what makes the memory survive `docker rm`, which is the
    first thing anyone does when a container misbehaves."""
    calls = []

    def fake_run(args, timeout=60):
        calls.append(args)
        if args[:2] == ["docker", "inspect"]:
            return _completed(returncode=1)
        return _completed()

    monkeypatch.setattr(quickstart, "_run", fake_run)
    monkeypatch.setattr(quickstart, "port_is_free", lambda p: True)

    assert quickstart.start_database()[0] == "started"
    create = next(c for c in calls if "-d" in c)
    assert f"{quickstart.CONTAINER}-data:/var/lib/postgresql/data" in create
    assert "unless-stopped" in create
    # Pinned to the AGE release, because "latest" tells a bug report nothing.
    assert quickstart.IMAGE in create
    assert "age1.5.0" in quickstart.IMAGE


def test_a_database_that_never_becomes_ready_is_reported_not_awaited(monkeypatch):
    monkeypatch.setattr(quickstart, "_run", lambda *a, **k: _completed(returncode=1))
    monkeypatch.setattr(quickstart.time, "sleep", lambda _: None)

    with pytest.raises(quickstart.QuickstartError) as e:
        quickstart.wait_until_ready(timeout_s=0.01)

    assert "docker logs" in str(e.value), "it did not say how to find out why"


def _rendered(**over):
    base = {
        "database": "started", "port": 5433, "schema": "at head",
        "clients": ["Claude Code"], "python": "/venv/bin/python",
        "url": "postgresql://postgres:postgres@localhost:5433/echo_memory",
        "hosted_hint": True,
    }
    return quickstart.render({**base, **over})


def test_the_next_step_is_a_command_that_exists(tmp_path):
    """The first version printed `echo-memory serve`, which is not a
    subcommand, and passed none of the three variables the server needs - so
    following it produced a client that fails at startup. A wrong next step is
    worse than none: it spends the one moment somebody will debug."""
    out = _rendered()

    assert "echo-memory serve" not in out
    assert "-m echo_memory.server" in out
    for var in ("ECHO_MEMORY_USER_ID", "ECHO_MEMORY_AGENT_ID", "ECHO_MEMORY_DATABASE_URL"):
        assert var in out, var


def test_the_printed_url_matches_the_port_it_started_on(tmp_path):
    """A connection string for a port nothing listens on is the same dead end
    as no connection string."""
    out = _rendered(port=5437, url="postgresql://postgres:postgres@localhost:5437/echo_memory")

    assert ":5437/echo_memory" in out
    assert ":5433/" not in out


def test_the_next_step_names_the_restart(tmp_path):
    """An MCP server holds the code it imported when the client started it, so
    a registration nobody restarts into does nothing. That has cost this
    project two data bugs; it belongs in the one screen everyone reads."""
    out = _rendered()

    assert "restart each client" in out.lower()
    assert "echo-memory install --global" in out
    assert "api.echo-mem.com" in out


def test_it_says_when_it_found_no_tools(tmp_path):
    assert "no agent tools" in _rendered(clients=[])


def test_clients_are_detected_from_disk_not_asked_for(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex/config.toml").write_text("")

    found = quickstart.detected_clients(tmp_path)

    assert found == ["Claude Code", "Codex"]


# --- the skill, once, for every project ---------------------------------------


def test_a_global_install_writes_only_the_skill(tmp_path):
    """`install` writes into a project because that is usually right: the
    wiring committed beside the code. But a skill that lives in one repo has to
    be installed again in the next, and when to write a memory is not
    repo-specific. Only the skill travels - .mcp.json and AGENTS.md belong to a
    repo and mean nothing in a home directory."""
    from echo_memory.cli import install

    done = install.install_global(tmp_path)

    skill = tmp_path / ".claude/skills/echo-memory/SKILL.md"
    assert skill.exists()
    assert skill.read_text().strip()
    assert done == [f"created  {skill}"]
    assert not (tmp_path / ".mcp.json").exists()
    assert not (tmp_path / "AGENTS.md").exists()


def test_installing_globally_twice_is_a_no_op(tmp_path):
    from echo_memory.cli import install

    install.install_global(tmp_path)
    again = install.install_global(tmp_path)

    assert again[0].startswith("unchanged")


def test_a_taken_port_moves_up_instead_of_failing(monkeypatch):
    """5433 is not a safe assumption on a machine that has met this project
    before: the repo's own compose file maps it, so following the old README
    and then running quickstart used to produce a raw Docker error as the first
    experience of a command named quickstart."""
    calls = []

    def fake_run(args, timeout=60):
        calls.append(args)
        if args[:2] == ["docker", "inspect"]:
            return _completed(returncode=1)
        return _completed()

    monkeypatch.setattr(quickstart, "_run", fake_run)
    monkeypatch.setattr(quickstart, "port_is_free", lambda p: p >= 5435)

    outcome, port = quickstart.start_database()

    assert port == 5435
    assert "5433 was taken" in outcome
    # Loopback prefixed since 0.4.1; the port it moved to is still the one used.
    assert "127.0.0.1:5435:5432" in next(c for c in calls if "-d" in c)


def test_the_url_follows_the_port_actually_used():
    """A connection string for a port nothing is listening on is worse than
    printing none."""
    assert quickstart.database_url(5437).endswith(":5437/echo_memory")


def test_no_free_port_is_reported_not_looped(monkeypatch):
    monkeypatch.setattr(quickstart, "port_is_free", lambda p: False)

    with pytest.raises(quickstart.QuickstartError) as e:
        quickstart.free_port(5433, tries=3)

    assert "--port" in str(e.value)
