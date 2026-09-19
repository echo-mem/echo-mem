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


def test_the_password_is_still_the_known_one(monkeypatch):
    """Deliberately unchanged. It is a local throwaway container and the
    connection string is printed on screen; rotating it would imply the
    exposure was about secrecy rather than about reachability."""
    recorder = _Recorder()
    monkeypatch.setattr(quickstart, "_run", recorder)
    monkeypatch.setattr(quickstart, "container_state", lambda name=None: "none")
    monkeypatch.setattr(quickstart, "free_port", lambda port: port)

    quickstart.start_database(port=5433)

    run_cmd = next(c for c in recorder.calls if "run" in c)
    assert "POSTGRES_PASSWORD=postgres" in run_cmd
    assert "postgres:postgres@localhost" in quickstart.database_url(5433)


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
