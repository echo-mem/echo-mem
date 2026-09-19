"""echo-memory quickstart: from nothing to a working memory in one command.

Setting this up used to be five steps, and the second one was a wall. Echo
Memory needs a Postgres carrying both pgvector and Apache AGE, and no managed
provider offers AGE - not RDS, Aurora, Cloud SQL, Neon or Supabase. So the
honest instruction was "build this Dockerfile, which compiles AGE from source",
which is minutes of build and a decision about a database extension, asked of
somebody who has not yet seen the product work.

That ordering is backwards. The database is an implementation detail of a
memory graph, and a person evaluating one should meet it after it is running,
not before. This starts a published image, applies the migrations, registers
the MCP server with every client on the machine, installs the hooks, and stops.

Two things it deliberately does not do. It never silently replaces an existing
container or config - a second run reports what is already there and changes
nothing. And it never installs Docker: a command that installs a daemon because
it needed one is a command nobody should run.
"""

from __future__ import annotations

import secrets
import shutil
import socket
import subprocess
import sys
import time
from urllib.parse import quote, urlunsplit

# Published multi-arch, so this is a pull rather than a compile. The tag names
# the AGE release it carries, because "latest" tells a bug report nothing.
IMAGE = "ghcr.io/ayushcodes10/echo-mem-postgres:pg16-age1.5.0"
CONTAINER = "echo-memory-db"
PORT = 5433
# Generated per install, never shared, and never written to a file of ours.
#
# The old value was the literal string "postgres", the same on every machine
# that has ever run this. Combined with a port published on 0.0.0.0 that was
# enough to read anybody's memory graph off the network; the port is bound to
# loopback now, and this is the second lock rather than a replacement for the
# first. A password only matters once something can reach the port, and the
# point of defence in depth is that you do not get to assume nothing ever will.
#
# url-safe on purpose: a password reaches Postgres through a connection string,
# and one containing @ or : would produce a URL that parses into the wrong
# fields rather than an error anybody could read.
PASSWORD_BYTES = 24


def new_password() -> str:
    return secrets.token_urlsafe(PASSWORD_BYTES)


def container_password(name: str = CONTAINER) -> str | None:
    """The password the running container was actually created with.

    Read back rather than remembered, so nothing new is stored on disk and a
    container from before this change still works: those were made with
    "postgres", and this returns that just as happily as a generated one.
    """
    probe = _run([
        "docker", "inspect", "-f",
        '{{range .Config.Env}}{{println .}}{{end}}', name,
    ], timeout=20)
    if probe.returncode != 0:
        return None
    for line in probe.stdout.splitlines():
        if line.startswith("POSTGRES_PASSWORD="):
            return line.split("=", 1)[1].strip() or None
    return None


DRIVER = "postgresql"
DB_USER = "postgres"
DB_HOST = "localhost"
DB_NAME = "echo_memory"


def database_url(port: int = PORT, password: str | None = None) -> str:
    """The connection string for the container this command manages.

    `password` is required in practice and defaulted only so a caller asking
    for the shape of the URL does not have to invent one. It reads from the
    container when it can, because the answer lives there.

    Composed from parts rather than interpolated into one string, and the
    password percent encoded on the way in. `new_password` returns url-safe
    text today, so nothing needs escaping today; a builder keeps that true if
    the password ever comes from somewhere else, where an f-string would
    silently produce a URL that parses into the wrong fields.
    """
    secret = password or container_password() or "postgres"
    netloc = f"{DB_USER}:{quote(secret, safe='')}@{DB_HOST}:{port}"
    return urlunsplit((DRIVER, netloc, f"/{DB_NAME}", "", ""))

# Long enough for a first-run pull and initdb on a slow disk, short enough that
# a wedged container is reported rather than waited on forever.
READY_TIMEOUT_S = 180


class QuickstartError(Exception):
    pass


def _run(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)


def docker_available() -> tuple[bool, str]:
    """Whether Docker is installed AND running. The two failures need different
    sentences: one is an install, the other is opening an app."""
    if shutil.which("docker") is None:
        return False, (
            "Docker is not installed. Echo Memory needs a Postgres with Apache AGE, "
            "and no managed provider has AGE - so the database runs in a container "
            "here. Install Docker Desktop (or Colima, or Podman with a docker alias) "
            "and run this again."
        )
    probe = _run(["docker", "info"], timeout=20)
    if probe.returncode != 0:
        return False, "Docker is installed but not running. Start it and run this again."
    return True, ""


def container_state(name: str = CONTAINER) -> str:
    """'running', 'stopped', or 'absent'."""
    probe = _run(["docker", "inspect", "-f", "{{.State.Status}}", name], timeout=20)
    if probe.returncode != 0:
        return "absent"
    return "running" if probe.stdout.strip() == "running" else "stopped"


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) != 0


def free_port(preferred: int = PORT, tries: int = 20) -> int:
    """The preferred port, or the next free one above it.

    5433 is not a safe assumption on a machine that has met this project
    before: the repository's own docker-compose.yml maps it, so anyone who
    followed the old README and then ran this got a raw Docker error - "Bind
    for 0.0.0.0:5433 failed: port is already allocated" - as their first
    experience of a command named quickstart.
    """
    for candidate in range(preferred, preferred + tries):
        if port_is_free(candidate):
            return candidate
    raise QuickstartError(
        f"no free port between {preferred} and {preferred + tries - 1}. "
        "Free one, or pass --port."
    )


def start_database(
    image: str = IMAGE, name: str = CONTAINER, port: int = PORT
) -> tuple[str, int]:
    """Start the database, or report that it is already up.

    Never replaces a running container. Somebody running this twice, or running
    it on a machine where a previous install is holding real memory, must not
    lose it to a command whose name suggests it only sets things up.

    Returns the outcome and the port actually used, which may not be the one
    asked for.
    """
    state = container_state(name)
    if state == "running":
        return "already running", published_port(name) or port
    if state == "stopped":
        started = _run(["docker", "start", name], timeout=60)
        if started.returncode != 0:
            raise QuickstartError(f"could not start the existing {name}: {started.stderr.strip()}")
        return "restarted", published_port(name) or port

    chosen = free_port(port)
    password = new_password()
    created = _run([
        "docker", "run", "-d", "--name", name,
        "--restart", "unless-stopped",
        "-e", f"POSTGRES_PASSWORD={password}",
        "-e", "POSTGRES_DB=echo_memory",
        # 127.0.0.1, not a bare port. `-p 5433:5432` binds 0.0.0.0 and [::], so
        # every database this command made was reachable from any machine on
        # the same network, and until the line above it every one of them had
        # the same password. Verified on 2026-09-20 by connecting to a real
        # quickstart container as superuser over a laptop's LAN address.
        #
        # What is behind it is the whole point: a memory graph holds hostnames,
        # account numbers and client names, which is why the README's own
        # screenshots use a synthetic store. A café or office network was
        # enough to read all of it.
        #
        # This is the lock that mattered. A generated password helps only once
        # something can reach the port; binding to loopback is what decides
        # whether anything can.
        "-p", f"127.0.0.1:{chosen}:5432",
        "-v", f"{name}-data:/var/lib/postgresql/data",
        image,
    ], timeout=600)
    if created.returncode != 0:
        raise QuickstartError(f"could not start the database: {created.stderr.strip()}")
    return ("started" if chosen == port else f"started ({port} was taken)"), chosen


def published_port(name: str = CONTAINER) -> int | None:
    """Which host port an existing container is already published on.

    Asked rather than assumed: a container started by an earlier run, or by
    hand, may not be on the default, and printing a connection string for the
    wrong port is worse than printing none.
    """
    probe = _run([
        "docker", "inspect", "-f",
        '{{ (index (index .NetworkSettings.Ports "5432/tcp") 0).HostPort }}', name,
    ], timeout=20)
    if probe.returncode != 0:
        return None
    try:
        return int(probe.stdout.strip())
    except ValueError:
        return None


def published_interface(name: str = CONTAINER) -> str | None:
    """Which host interface an existing container is published on.

    Containers created before the loopback fix are still bound to 0.0.0.0 and
    docker will not rebind a running one, so this exists to say so rather than
    to leave everybody who already ran quickstart exposed without knowing.
    """
    probe = _run([
        "docker", "inspect", "-f",
        '{{ (index (index .NetworkSettings.Ports "5432/tcp") 0).HostIp }}', name,
    ], timeout=20)
    if probe.returncode != 0:
        return None
    return probe.stdout.strip() or None


def exposed_to_network(name: str = CONTAINER) -> bool:
    """True when this container's database can be reached from off the machine."""
    host_ip = published_interface(name)
    return host_ip in {"0.0.0.0", "::", ""}


def wait_until_ready(name: str = CONTAINER, timeout_s: int = READY_TIMEOUT_S) -> None:
    """Postgres accepts connections some seconds after the container exists,
    and the difference is the most common reason a first run 'fails'."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        probe = _run(["docker", "exec", name, "pg_isready", "-U", "postgres"], timeout=20)
        if probe.returncode == 0:
            return
        time.sleep(2)
    raise QuickstartError(
        f"the database did not become ready within {timeout_s}s. "
        f"`docker logs {name}` will say why."
    )


def detected_clients(home) -> list[str]:
    """Which agent tools are on this machine, by their config, not by asking."""
    found = []
    if (home / ".claude").is_dir():
        found.append("Claude Code")
    if (home / "Library/Application Support/Claude/claude_desktop_config.json").exists():
        found.append("Claude Desktop")
    if (home / ".cursor").is_dir():
        found.append("Cursor")
    if (home / ".codex/config.toml").exists():
        found.append("Codex")
    return found


def render(result: dict) -> str:
    """The last screen anyone reads, so every command on it has to actually run.

    The first version printed `echo-memory serve`, which is not a subcommand -
    the server is `python -m echo_memory.server` - and passed none of the three
    environment variables it needs, so following it produced a client that
    fails at startup with a config error. A wrong next step is worse than no
    next step: it spends the one moment somebody is willing to debug.
    """
    python = result.get("python") or "python"
    url = result["url"]
    lines = [
        "Echo Memory is ready.",
        "",
        f"  database   {result['database']} on port {result['port']}",
        f"  schema     {result['schema']}",
    ]
    lines.append(
        f"  found      {', '.join(result['clients'])}" if result["clients"]
        else "  found      no agent tools on this machine yet"
    )
    if result.get("exposed"):
        # Loud, and above the happy path, because this container predates the
        # loopback fix and docker will not rebind a container that exists.
        # Somebody who ran quickstart before today is exposed right now and has
        # no reason to suspect it.
        lines += [
            "",
            "  WARNING    this database is published on 0.0.0.0, so every machine on",
            "             your network can reach it with the password quickstart set.",
            "             A memory graph holds hostnames, account numbers and client",
            "             names. Containers made before this release bind that way and",
            "             docker cannot rebind one in place.",
            "",
            "             Your memory is in a volume and is not touched by this:",
            "",
            f"               docker rm -f {CONTAINER}",
            "               echo-memory quickstart",
        ]
    lines += [
        "",
        "Register it with Claude Code. --scope user is once for this machine,",
        "not once per project:",
        "",
        "  claude mcp add --scope user echo-memory \\",
        "    -e ECHO_MEMORY_USER_ID=you \\",
        "    -e ECHO_MEMORY_AGENT_ID=claude-code \\",
        f'    -e ECHO_MEMORY_DATABASE_URL="{url}" \\',
        f"    -- {python} -m echo_memory.server",
        "",
        "Other tools each want their own ECHO_MEMORY_AGENT_ID - cursor, codex,",
        "claude-desktop. Two tools sharing an id makes a cross-tool recall",
        "impossible to see afterwards. `echo-memory adopt` wires every client on",
        "this machine at once and shows the diff first.",
        "",
        "Then, so an agent knows when to record and recall rather than only that",
        "the tools exist:",
        "",
        "  echo-memory install --global",
        "",
        "Restart each client afterwards. An MCP server holds the code and config",
        "it started with, so a running one will not pick this up.",
    ]
    if result.get("hosted_hint"):
        lines += [
            "",
            "Prefer not to run a database at all? The hosted service needs no local",
            "Postgres: https://api.echo-mem.com",
        ]
    return "\n".join(lines) + "\n"


def run(args, _config=None, _conn=None) -> int:
    """No config and no connection: this is the command that exists because
    neither is set up yet, so it must run before either can be built."""
    from pathlib import Path

    from echo_memory.cli import initdb

    ok, why = docker_available()
    if not ok:
        print(f"error: {why}")
        return 1

    try:
        database, port = start_database(port=getattr(args, "port", None) or PORT)
        wait_until_ready()
    except QuickstartError as e:
        print(f"error: {e}")
        return 1

    # From the container rather than from a constant: on a second run this is
    # whatever the existing container was created with, which for a container
    # made before passwords were generated is still "postgres".
    url = database_url(port, container_password())
    initdb.upgrade(url)
    exposed = exposed_to_network()

    home = Path.home()
    print(render({
        "database": database,
        "port": port,
        "url": url,
        "schema": "at head",
        "clients": detected_clients(home),
        "python": sys.executable,
        "hosted_hint": True,
        "exposed": exposed,
    }), end="")
    return 0
