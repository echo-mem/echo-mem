"""Shared fixtures for tests that need a real Postgres+AGE+pgvector database.
Skips automatically if ECHO_MEMORY_DATABASE_URL isn't set or unreachable
(e.g. a fresh clone before docker compose up); CI builds and starts the
database itself, see .github/workflows/ci.yml. Run locally with
`docker compose up -d` first."""

import os
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
DATABASE_URL = os.environ.get("ECHO_MEMORY_DATABASE_URL")


def _reachable(url: str | None) -> bool:
    if not url:
        return False
    try:
        psycopg.connect(url, connect_timeout=2).close()
        return True
    except psycopg.OperationalError:
        return False


def pytest_collection_modifyitems(items):
    if _reachable(DATABASE_URL):
        return
    n = sum(1 for item in items if "tests/integration" in str(item.fspath))
    # Skipping locally is right - a fresh clone has no database. Skipping in CI
    # is a fully green run with zero coverage of write_episode, retrieval,
    # entity resolution and the whole gate. ECHO_MEMORY_REQUIRE_DB=1 turns the
    # skip into a hard failure, and CI sets it.
    if os.environ.get("ECHO_MEMORY_REQUIRE_DB") == "1":
        raise pytest.UsageError(
            f"ECHO_MEMORY_REQUIRE_DB=1 but the database is unreachable: {n} "
            f"integration tests would have been skipped silently. "
            f"ECHO_MEMORY_DATABASE_URL={DATABASE_URL!r}"
        )
    skip = pytest.mark.skip(reason="ECHO_MEMORY_DATABASE_URL not set or unreachable")
    for item in items:
        if "tests/integration" in str(item.fspath):
            item.add_marker(skip)


def pytest_terminal_summary(terminalreporter):
    """Say it out loud. A quiet skip reads exactly like a pass."""
    if not _reachable(DATABASE_URL):
        terminalreporter.write_line(
            "SKIPPED all integration tests: database unreachable "
            f"({DATABASE_URL or 'ECHO_MEMORY_DATABASE_URL unset'})",
            yellow=True,
        )


GRAPH = "echo_memory"


def _version() -> str | None:
    """The revision alembic believes this database is at, or None for base."""
    with psycopg.connect(DATABASE_URL, autocommit=True) as admin:
        if admin.execute("SELECT to_regclass('public.alembic_version')").fetchone()[0] is None:
            return None
        row = admin.execute("SELECT version_num FROM public.alembic_version").fetchone()
        return row[0] if row else None


def _assert_clean_base() -> None:
    """Make 'alembic says base' and 'nothing of ours exists' agree.

    `drop_graph` is not atomic. A `downgrade base` that dies partway can leave
    the database claiming to be at base while the graph's SCHEMA survives.
    Migration 0001 guards `create_graph` on the ag_graph catalog ROW, so that
    guard passes, create_graph runs, and the next test's upgrade dies on

        UniqueViolation: duplicate key value violates unique constraint
                         "pg_namespace_nspname_index"
        DETAIL:  Key (nspname)=(echo_memory) already exists.
        CONTEXT:  SQL statement "SELECT create_graph('echo_memory')"

    One such teardown used to poison every test after it: every later fixture
    failed the same way and left its connection in BAD state. Six tests in
    test_resolved_to_is_checked.py failed that way while passing in isolation,
    which is the shape that hides a real regression. Forging the same state
    by hand surfaces it as `DuplicateSchema: schema "echo_memory" already
    exists`; same cause, and either way the chain stops at 0001.

    CI never hit this, which is why it went unnoticed: there the database is a
    fresh container whose only client is the test run, so there are never
    enough lingering connections to collide. Green CI was not evidence the
    suite was sound locally, and a red local run was not evidence CI was wrong.

    Repairing the precondition here, rather than making the teardown
    bulletproof, is deliberate. The teardown has 350-odd connections the tests
    never close contending for locks with it, so it will fail again; what
    matters is that its failure stops at the test that caused it.

    A mid-chain revision is left alone on purpose. That is a genuine migration
    bug and should be seen, not swept up.
    """
    if _version() is not None:
        return
    with psycopg.connect(DATABASE_URL, autocommit=True) as admin:
        catalog = admin.execute("SELECT to_regclass('ag_catalog.ag_graph')").fetchone()[0]
        if catalog is not None:
            known = admin.execute(
                "SELECT 1 FROM ag_catalog.ag_graph WHERE name::text = %s", (GRAPH,)
            ).fetchone()
            if known is not None:
                admin.execute("LOAD 'age'")
                admin.execute("SELECT ag_catalog.drop_graph(%s, true)", (GRAPH,))
        admin.execute(f'DROP SCHEMA IF EXISTS "{GRAPH}" CASCADE')
        # Everything else the migrations build lives in public, so at base it
        # should be empty. Dropping the schema beats enumerating the objects:
        # the first attempt at this listed migration 0001's tables, got past
        # 0001, and then died on 0002's leftover trial_observation_kind type.
        # This is safe only because DATABASE_URL is a dedicated test database
        # (docker compose, port 5433) that holds nothing but these migrations.
        # The vector extension goes with it and 0001 recreates it.
        admin.execute("DROP SCHEMA IF EXISTS public CASCADE")
        admin.execute("CREATE SCHEMA public")


def _alembic(*argv: str) -> None:
    env = {**os.environ, "ECHO_MEMORY_DATABASE_URL": DATABASE_URL}
    subprocess.run([sys.executable, "-m", "alembic", *argv], cwd=REPO_ROOT, env=env, check=True)


@pytest.fixture
def migrated_db():
    _assert_clean_base()
    _alembic("upgrade", "head")
    yield DATABASE_URL
    _alembic("downgrade", "base")
