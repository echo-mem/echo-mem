"""The lock key is a tenancy claim, so it is tested as one.

`attest()` says one account's writes must not be able to block another's. Under
`hashtext()` that is false at a rate the birthday bound gives exactly, and the
rate is not visible from any single write: two colliding scopes just wait for
each other and look slow. So these tests check the properties the claim rests
on - determinism, namespace separation, range - and then count actual
collisions over a corpus of realistic group ids rather than asserting the maths
and hoping the implementation matches it.
"""

from __future__ import annotations

import pytest
from psycopg.pq import TransactionStatus

from echo_memory.infra.graphlock import (
    MAX_GRAPH_WRITERS_ENV,
    graph_write_gate,
    lock_key,
    max_graph_writers,
)

INT64_MIN, INT64_MAX = -(2**63), 2**63 - 1


class RecordingConnection:
    """Enough of a psycopg connection for the gate: it takes one lock and
    reads one status. Faked rather than opened, because what is under test is
    which statement the gate sends, and a real connection would make that a
    database test for no extra coverage."""

    def __init__(self, status=TransactionStatus.INTRANS):
        self.statements: list[tuple[str, tuple]] = []
        self.info = type("Info", (), {"transaction_status": status})()

    def execute(self, sql, params=()):
        self.statements.append((sql, params))
        return self


# --- the key ------------------------------------------------------------


def test_lock_key_is_deterministic():
    assert lock_key("graph", "echo_memory") == lock_key("graph", "echo_memory")


def test_lock_key_separates_namespaces():
    """`write_episode` and `attest()` take two locks for the same scope in one
    lock space. A third caller reusing either key would deadlock against code
    that does not mention it."""
    scope = "user:ayush:agent:claude"
    assert lock_key("episode", scope) != lock_key("attest", scope)
    assert lock_key("graph", scope) != lock_key("episode", scope)


def test_lock_key_fits_in_signed_64_bit():
    """pg_advisory_xact_lock takes bigint. A key outside that range is a
    NumericValueOutOfRange at the database, on the write path, under load."""
    for value in ["echo_memory", "", "a" * 4096, "user:x:shared", "é中\U0001f600"]:
        key = lock_key("graph", value)
        assert INT64_MIN <= key <= INT64_MAX, (value, key)


def test_lock_key_uses_the_whole_signed_range():
    """Both signs appear. A derivation that masked the top bit would pass every
    test above while halving the space, which is the bug this helper exists to
    fix one octave up."""
    keys = [lock_key("graph", f"scope-{i}") for i in range(200)]
    assert any(k < 0 for k in keys)
    assert any(k > 0 for k in keys)


def test_lock_key_does_not_collide_where_hashtext_would():
    """Measured, not argued. 400,000 group ids in this store's actual shape
    (`user:<u>:agent:<a>`), one fixed corpus, one fixed digest, so the counts
    below are exact rather than probable:

        width                collisions
        32 bit (hashtext)            22
        64 bit (lock_key)             0

    400,000 rather than a plausible 10,000 because 10,000 puts the expected
    number of colliding pairs at 1.16 and seeing zero at 31%, which is a test
    that passes by luck a third of the time. The int4 side is the same digest
    truncated, so both widths see identical inputs and only the width differs.
    0.2s."""
    ids = [f"user:u{i // 7}:agent:a{i % 7}" for i in range(400_000)]
    keys = [lock_key("episode", g) for g in ids]

    assert len(set(keys)) == len(ids), f"{len(ids) - len(set(keys))} collisions at 64 bit"
    assert len({k & 0xFFFFFFFF for k in keys}) == len(ids) - 22


# --- the limit ----------------------------------------------------------


def test_one_writer_per_graph_by_default(monkeypatch):
    monkeypatch.delenv(MAX_GRAPH_WRITERS_ENV, raising=False)
    assert max_graph_writers() == 1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1", 1), ("4", 4), ("  8  ", 8), ("", 1), ("0", 1), ("-3", 1), ("lots", 1), ("1.5", 1)],
)
def test_max_graph_writers_reads_the_environment(monkeypatch, raw, expected):
    """Garbage reads as 1, not as "off". An unparseable limit is somebody's
    typo, and a typo must not silently remove the protection."""
    monkeypatch.setenv(MAX_GRAPH_WRITERS_ENV, raw)
    assert max_graph_writers() == expected


# --- the gate -----------------------------------------------------------


def test_the_gate_takes_one_lock_when_the_limit_is_one(monkeypatch):
    monkeypatch.setenv(MAX_GRAPH_WRITERS_ENV, "1")
    conn = RecordingConnection()

    with graph_write_gate(conn) as held:
        assert held is True

    assert len(conn.statements) == 1
    sql, params = conn.statements[0]
    assert "pg_advisory_xact_lock" in sql
    assert params == (lock_key("graph", "echo_memory"),)


def test_the_gate_keys_on_the_graph_not_the_scope(monkeypatch):
    """A per scope key is what the gate exists to replace. If this key ever
    varies with the scope, the gate has silently become the lock
    write_episode already takes."""
    monkeypatch.setenv(MAX_GRAPH_WRITERS_ENV, "1")
    first, second = RecordingConnection(), RecordingConnection()

    with graph_write_gate(first, graph="echo_memory"):
        pass
    with graph_write_gate(second, graph="echo_memory"):
        pass

    assert first.statements[0][1] == second.statements[0][1]


def test_the_gate_is_a_no_op_above_one_writer(monkeypatch):
    monkeypatch.setenv(MAX_GRAPH_WRITERS_ENV, "4")
    conn = RecordingConnection()

    with graph_write_gate(conn) as held:
        assert held is False

    assert conn.statements == [], "the gate locked while explicitly disabled"


def test_the_gate_does_not_check_the_transaction_when_disabled(monkeypatch):
    """Turning the limit up must not start raising on callers that never
    needed a transaction. The no-op has to be a real no-op."""
    monkeypatch.setenv(MAX_GRAPH_WRITERS_ENV, "4")
    conn = RecordingConnection(status=TransactionStatus.IDLE)

    with graph_write_gate(conn) as held:
        assert held is False


def test_the_gate_refuses_to_lock_outside_a_transaction(monkeypatch):
    """Every connection this package opens is autocommit, so a transaction
    scoped lock taken without `with conn.transaction():` is released before
    the next statement. Silent, and indistinguishable from protection."""
    monkeypatch.setenv(MAX_GRAPH_WRITERS_ENV, "1")
    conn = RecordingConnection(status=TransactionStatus.IDLE)

    with pytest.raises(RuntimeError, match="open transaction"), graph_write_gate(conn):
        pass

    assert conn.statements == []
