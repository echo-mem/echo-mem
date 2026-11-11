"""What the graph write gate actually covers, pinned so it cannot over-promise.

`infra/graphlock.py` exists because AGE's label catalogue is per graph while
`write_episode`'s lock is per scope. The gate is real and it works, and the
thing worth testing is not that it works but exactly where it stops, because
three separate claims have been made about this bug and only one of them is
the gate's business.

The boundary, established by the tests below:

    covered        write_episode, and the inbox drain which shares the key,
                   within one database, when an operator has asked for it
    NOT covered    migrations: create_graph and drop_graph in migration 0001
                   take no lock at all, so the entire DDL path is outside it
    NOT covered    other databases: advisory lock keys are scoped to a
                   database, so the same key is grantable twice
    NOT covered    anything writing Cypher directly rather than through
                   write_episode

That matters because of which failure was actually reproduced. Concurrent
cross-scope WRITES did not corrupt anything here in about 30,000 episodes.
Concurrent graph DDL did, once, with `label (relation) cache corrupted`. So on
the evidence available the gate does not cover the path that broke, and the
mitigation for that path is operational rather than a lock: do not migrate
several databases of one instance at the same time.

Stating that in a test rather than only in a docstring, because a gate whose
advertised scope is wider than its real scope is worse than no gate. Somebody
reads the module, believes they are protected, and stops looking.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from echo_memory.infra import graphlock

MIGRATIONS = Path(__file__).parent.parent.parent / "src" / "echo_memory" / "migrations"


def test_migrations_take_no_graph_lock():
    """Read as source rather than executed, because the claim is about what the
    DDL path does NOT do and an absence cannot be observed by running it.

    If a migration ever starts taking the gate this fails, which is the right
    time to revisit the docstring above rather than six months later.
    """
    ddl_files = [
        MIGRATIONS / "versions" / "0001_initial_schema.py",
        MIGRATIONS / "env.py",
    ]
    for path in ddl_files:
        body = path.read_text()
        assert "create_graph" in body or "run_migrations" in body, (
            f"{path.name} is not the file this test thinks it is"
        )
        assert "advisory" not in body.lower(), (
            f"{path.name} now takes a lock; the gate's documented boundary in "
            f"this file's docstring is stale"
        )
        assert "graph_gate" not in body, f"{path.name} now takes the gate"


def test_only_write_episode_enters_the_gate():
    """One caller, found by reading the source. A second caller appearing is
    not wrong, it just means the boundary above needs rewriting."""
    src = MIGRATIONS.parent
    callers = sorted(
        p.relative_to(src).as_posix()
        for p in src.rglob("*.py")
        if "graph_gate.enter" in p.read_text() and p.name != "graph_gate.py"
    )
    assert callers == ["ingestion/write_episode.py"], (
        f"the gate's callers changed: {callers}"
    )


def test_the_same_lock_key_is_grantable_in_another_database(migrated_db):
    """Advisory locks are scoped to a database, measured rather than taken from
    the documentation.

    This is why the gate cannot address the cross-database case at all: there
    is no instance-wide lock in SQL to reach for. Whoever reads graphlock.py
    hoping it covers a CI runner migrating several databases at once needs to
    know that before they rely on it.
    """
    key = graphlock.lock_key("graph", "echo_memory")

    held = psycopg.connect(migrated_db, autocommit=False)
    other_url = migrated_db.rsplit("/", 1)[0] + "/postgres"
    try:
        held.execute("SELECT pg_advisory_xact_lock(%s::bigint)", (key,))

        # Same database, same key: correctly refused.
        same = psycopg.connect(migrated_db, autocommit=True)
        try:
            blocked = same.execute(
                "SELECT pg_try_advisory_lock(%s::bigint)", (key,)
            ).fetchone()[0]
        finally:
            same.close()
        assert blocked is False, "the gate does not serialise within one database"

        # Different database, same key: granted, which is the finding.
        try:
            elsewhere = psycopg.connect(other_url, autocommit=True)
        except psycopg.OperationalError:
            pytest.skip("no second database reachable on this instance")
        try:
            granted = elsewhere.execute(
                "SELECT pg_try_advisory_lock(%s::bigint)", (key,)
            ).fetchone()[0]
            elsewhere.execute("SELECT pg_advisory_unlock(%s::bigint)", (key,))
        finally:
            elsewhere.close()
        assert granted is True, (
            "advisory locks appear to be instance wide on this build, which "
            "would make graphlock.py's stated boundary too pessimistic"
        )
    finally:
        held.rollback()
        held.close()


def test_the_gate_is_off_by_default_so_nothing_pays_for_it(monkeypatch):
    """The default, restated here because the cost is 2.6x on every write and
    a reader of this file should not have to go and look."""
    monkeypatch.delenv(graphlock.MAX_GRAPH_WRITERS_ENV, raising=False)
    assert graphlock.max_graph_writers() == graphlock.UNLIMITED

    from echo_memory.infra import graph_gate

    class Refuses:
        def execute(self, *a, **k):
            raise AssertionError("the gate took a lock while switched off")

    assert graph_gate.enter(Refuses()) is False
