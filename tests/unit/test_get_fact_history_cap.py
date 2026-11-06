"""Unit tests for get_fact_history's MAX_ENTRIES cap (#190).

Uses a fake connection so the boundary cases do not need Postgres: the
interesting behaviour is which rows we keep and when `truncated` flips.
"""

from datetime import UTC, datetime

from echo_memory.audit.get_audit_log import MAX_ENTRIES, get_fact_history


def _row(n: int):
    ts = datetime(2026, 1, 1, tzinfo=UTC).replace(day=min(n % 28 + 1, 28))
    return (
        ts,
        "created" if n == 0 else "fact_superseded",
        [f"fact-{n}"],
        None,
        None,
        {"fact": f"entry {n}"},
        f"sess-{n}",
        f"summary {n}",
        None,
    )


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class FakeConn:
    """Applies the LIMIT from the last query param the way Postgres would."""

    def __init__(self, rows):
        self.rows = rows
        self.last_params = None

    def execute(self, sql, params):
        self.last_params = params
        limit = params[-1]
        return _Result(self.rows[:limit])


def test_truncated_false_at_and_below_cap_and_true_above():
    for target, expected in ((499, False), (MAX_ENTRIES, False), (MAX_ENTRIES + 1, True)):
        conn = FakeConn([_row(i) for i in range(target)])
        result = get_fact_history(conn, "g1", "fact-0")
        assert len(result["entries"]) == min(target, MAX_ENTRIES), target
        assert result["truncated"] is expected, target
        assert conn.last_params == ("g1", "fact-0", MAX_ENTRIES + 1)


def test_cap_keeps_the_oldest_entries_not_the_newest():
    """A fact history is a chain: creation is the anchor. Cap drops the tail."""
    conn = FakeConn([_row(i) for i in range(MAX_ENTRIES + 3)])
    result = get_fact_history(conn, "g1", "fact-0")
    assert result["truncated"] is True
    assert len(result["entries"]) == MAX_ENTRIES
    assert result["entries"][0]["summary"] == "summary 0"
    assert result["entries"][-1]["summary"] == f"summary {MAX_ENTRIES - 1}"
    assert all(e["summary"] != f"summary {MAX_ENTRIES}" for e in result["entries"])
