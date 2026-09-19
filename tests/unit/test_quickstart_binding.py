"""The database quickstart starts must not be reachable from the network.

`docker run -p 5433:5432` binds 0.0.0.0 and [::], not loopback. Combined with
the password quickstart sets, that made every memory graph created by this
command readable by anyone on the same network: a café, an office, a hotel.
Verified on 2026-09-20 by connecting to a real quickstart container as
superuser over a laptop's LAN address.

The password is not the bug and changing it would not have fixed this. An
attacker on the same network can reach an open port either way. What should
never have been true is that the port was theirs to reach.
"""

from __future__ import annotations

import pathlib

from echo_memory.cli import quickstart


class _Recorder:
    """Captures the docker argv instead of running it."""

    def __init__(self, existing: str = "none"):
        self.calls: list[list[str]] = []
        self.existing = existing

    def __call__(self, args, timeout=60):
        self.calls.append(args)

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()


def test_a_new_container_is_published_on_loopback_only(monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr(quickstart, "_run", recorder)
    monkeypatch.setattr(quickstart, "container_state", lambda name=None: "none")
    monkeypatch.setattr(quickstart, "free_port", lambda port: port)

    quickstart.start_database(port=5433)

    run_cmd = next(c for c in recorder.calls if "run" in c)
    published = run_cmd[run_cmd.index("-p") + 1]
    assert published == "127.0.0.1:5433:5432", published
    assert not published.startswith("5433:"), "a bare port binds every interface"


def test_every_install_gets_its_own_password(monkeypatch):
    """The second lock, and only the second. Loopback is what decides whether
    anything can reach the port; this decides what happens if something does,
    which is the case you do not get to assume away."""
    seen = []
    for _ in range(2):
        recorder = _Recorder()
        monkeypatch.setattr(quickstart, "_run", recorder)
        monkeypatch.setattr(quickstart, "container_state", lambda name=None: "none")
        monkeypatch.setattr(quickstart, "free_port", lambda port: port)
        quickstart.start_database(port=5433)
        run_cmd = next(c for c in recorder.calls if "run" in c)
        seen.append(next(a for a in run_cmd if a.startswith("POSTGRES_PASSWORD=")))

    assert seen[0] != seen[1], "two installs got the same password"
    assert "POSTGRES_PASSWORD=postgres" not in seen
    assert len(seen[0].split("=", 1)[1]) >= 24


def test_a_generated_password_survives_a_connection_string(monkeypatch):
    """A password carrying @ or : builds a URL that parses into the wrong
    fields rather than failing, which is a bug nobody can read."""
    from urllib.parse import urlparse

    password = quickstart.new_password()
    monkeypatch.setattr(quickstart, "container_password", lambda name=None: password)

    parsed = urlparse(quickstart.database_url(5433))

    assert parsed.hostname == "localhost"
    assert parsed.port == 5433
    assert parsed.password == password
    assert parsed.username == "postgres"


def test_the_password_is_read_back_from_the_container(monkeypatch):
    """Nothing new is stored on disk, and a container made before this change
    still works: those carry "postgres" and this returns it."""
    class Inspect:
        returncode = 0
        stdout = "PATH=/usr/bin\nPOSTGRES_PASSWORD=older-container\nPOSTGRES_DB=echo_memory\n"
        stderr = ""

    monkeypatch.setattr(quickstart, "_run", lambda *a, **k: Inspect())

    assert quickstart.container_password() == "older-container"
    assert "older-container@localhost:5433" in quickstart.database_url(5433)


def test_no_credential_is_hardcoded_anywhere_in_the_module():
    """What the scanner was pointing at, pinned so it cannot come back. The
    finding was wrong about the danger and right that a shared secret should
    not be a literal in the source."""
    source = pathlib.Path(quickstart.__file__).read_text()
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )

    assert "postgres:postgres@" not in code
    assert "POSTGRES_PASSWORD=postgres" not in code


def test_a_container_bound_to_every_interface_is_reported_as_exposed(monkeypatch):
    for host_ip in ("0.0.0.0", "::", ""):
        monkeypatch.setattr(quickstart, "published_interface", lambda name=None, h=host_ip: h)
        assert quickstart.exposed_to_network() is True, host_ip


def test_a_loopback_container_is_not_reported_as_exposed(monkeypatch):
    monkeypatch.setattr(quickstart, "published_interface", lambda name=None: "127.0.0.1")

    assert quickstart.exposed_to_network() is False


def test_an_exposed_container_is_warned_about_on_the_final_screen():
    """Docker cannot rebind a container that already exists, so anyone who ran
    quickstart before this release is exposed now and has no reason to suspect
    it. The recreate command has to be on the last screen they read."""
    page = quickstart.render({
        "database": "started", "port": 5433, "url": "postgresql://x",
        "schema": "at head", "clients": [], "python": "python", "exposed": True,
    })

    assert "0.0.0.0" in page
    assert "every machine on" in page
    assert f"docker rm -f {quickstart.CONTAINER}" in page
    assert "not touched" in page, "must say the memory survives the recreate"


def test_a_loopback_container_gets_no_warning():
    page = quickstart.render({
        "database": "started", "port": 5433, "url": "postgresql://x",
        "schema": "at head", "clients": [], "python": "python", "exposed": False,
    })

    assert "WARNING" not in page
