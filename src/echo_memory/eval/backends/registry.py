"""Name to backend, for the CLI.

Separate from `backends/__init__.py` so importing the protocol does not import
every adapter, and separate from each adapter so a missing SDK is reported by
the backend that needs it rather than at import time for all of them.
"""

from __future__ import annotations

from collections.abc import Callable

from echo_memory.eval.backends import BackendUnavailable


def _echo(conn, embedder):
    from echo_memory.eval.backends.echo import EchoBackend

    return EchoBackend(conn, embedder)


def _mem0(conn, embedder):
    from echo_memory.eval.backends.mem0 import Mem0Backend

    return Mem0Backend()


def _zep(conn, embedder):
    from echo_memory.eval.backends.zep import ZepBackend

    return ZepBackend()


#: Every backend, by the name the CLI takes. echo-mem is the default, so
#: `eval-external locomo <path>` keeps meaning what it has always meant.
BACKENDS: dict[str, Callable] = {
    "echo-mem": _echo,
    "mem0": _mem0,
    "zep": _zep,
}

#: Which backends need a database. Only ours: a competitor run should not
#: refuse to start because ECHO_MEMORY_DATABASE_URL is unset or because the
#: database holds somebody's facts, since it is not going to touch it.
NEEDS_DATABASE = frozenset({"echo-mem"})


def build(name: str, conn=None, embedder=None):
    if name not in BACKENDS:
        raise BackendUnavailable(
            f"unknown backend {name!r}; choose one of {', '.join(sorted(BACKENDS))}"
        )
    return BACKENDS[name](conn, embedder)
