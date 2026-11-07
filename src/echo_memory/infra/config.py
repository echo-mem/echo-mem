"""Server startup configuration.

See docs/designs/echo-memory-design.md's "Configuration" section: group_id is
never typed or constructed by the calling agent, it's resolved server-side
from these values plus the scope ("solo" | "shared") passed on each call.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from echo_memory.infra.project import UNKNOWN, detect_project


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    user_id: str
    agent_id: str
    database_url: str
    # Which private store this process writes to. Defaults to agent_id, which is
    # what every existing install resolves to, so nothing moves.
    #
    # It exists because attribution and tenancy were the same value. `solo` was
    # `user:X:agent:<agent_id>`, and giving each MCP client its own agent_id -
    # the entire point of `adopt` - would have forked solo into one invisible
    # store per client. Worse, every reader derives its group ids from its OWN
    # Config, so five of six solo scopes would have been invisible to
    # `trial check`, `status` and the dashboard: the same measurement blind spot
    # that made criterion 6 unreadable for three weeks, reintroduced by the
    # command written to fix it.
    #
    # agent_id now answers "who wrote this". solo_key answers "whose private
    # store is this". `adopt` sets one solo_key across every client and a
    # distinct agent_id for each.
    solo_key: str | None = None
    # Which project this process is writing from. Resolved from cwd, never
    # passed by the calling agent (see infra/project.py). Defaults to
    # "unknown" so a Config built by hand - tests, embedders - stays valid.
    project: str = UNKNOWN

    def solo_group_id(self) -> str:
        return f"user:{self.user_id}:agent:{self.solo_key or self.agent_id}"

    def shared_group_id(self) -> str:
        return f"user:{self.user_id}:shared"

    def group_id(self, scope: str) -> str:
        if scope == "solo":
            return self.solo_group_id()
        if scope == "shared":
            return self.shared_group_id()
        raise ConfigError(f'scope must be "solo" or "shared", got {scope!r}')


def load_config(env: Mapping[str, str] | None = None) -> Config:
    # Mapping, not dict: the default is os.environ, which is an _Environ
    # rather than a dict, and nothing here mutates it. Annotating the
    # parameter as dict made every read of it an error, which is the type
    # checker correctly pointing out that the signature described
    # something the function is never given.
    env = env if env is not None else os.environ
    required = ["ECHO_MEMORY_USER_ID", "ECHO_MEMORY_AGENT_ID"]
    if not env.get("ECHO_MEMORY_DATABASE_URL_FILE"):
        required.append("ECHO_MEMORY_DATABASE_URL")
    missing = [name for name in required if not env.get(name)]
    if missing:
        raise ConfigError(f"missing required environment variable(s): {', '.join(missing)}")
    return Config(
        user_id=env["ECHO_MEMORY_USER_ID"],
        agent_id=env["ECHO_MEMORY_AGENT_ID"],
        database_url=_database_url(env),
        project=detect_project(env=env),
        solo_key=env.get("ECHO_MEMORY_SOLO_KEY") or None,
    )


def _database_url(env: Mapping[str, str]) -> str:
    """The connection string, from a file when one is named.

    `install` writes the URL - password inline - into config files and used to
    tell the user to commit them. `adopt` would fan that out to six
    machine-global files, several of which sync to a cloud backup by default.
    Shell inheritance is not a fix: Claude Desktop launched from Finder gets no
    shell environment at all. A file the config merely points at works for every
    client and keeps the secret out of anything committable."""
    path = env.get("ECHO_MEMORY_DATABASE_URL_FILE")
    if path:
        try:
            url = Path(path).expanduser().read_text().strip()
        except OSError as e:
            raise ConfigError(f"cannot read ECHO_MEMORY_DATABASE_URL_FILE {path}: {e}") from e
        if not url:
            raise ConfigError(f"ECHO_MEMORY_DATABASE_URL_FILE {path} is empty")
        return url
    return env["ECHO_MEMORY_DATABASE_URL"]
