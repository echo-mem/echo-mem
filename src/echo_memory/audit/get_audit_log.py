"""get_audit_log: read-only access to the append-only audit trail (see the
design doc's Concrete Schema "Audit log table" section). Entries are
returned chronologically (oldest first): since `since` is a lower bound
moving forward in time, that's the natural reading order for a trail of
what happened, not a "most recent first" feed."""

import time
from datetime import UTC, datetime

from echo_memory.infra.logging import get_logger

MAX_ENTRIES = 500

_logger = get_logger("get_audit_log")


class ValidationError(Exception):
    pass


def _parse_since(since: str | None) -> datetime | None:
    if since is None:
        return None
    try:
        dt = datetime.fromisoformat(since)
    except ValueError as e:
        raise ValidationError(f"invalid since timestamp: {since!r}") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


_AUDIT_COLUMNS = """
    "timestamp", mutation_type, affected_edge_ids::text[],
    affected_node_id::text, before_fact, after_fact, session_id,
    summary, resolution_detail
"""


def _row_to_entry(row) -> dict:
    (
        ts,
        mutation_type,
        affected_edge_ids,
        affected_node_id,
        before_fact,
        after_fact,
        session_id,
        summary,
        resolution_detail,
    ) = row
    return {
        "timestamp": ts.isoformat(),
        "mutation_type": mutation_type,
        "affected_edge_ids": affected_edge_ids or [],
        "affected_node_id": affected_node_id,
        "before_fact": before_fact,
        "after_fact": after_fact,
        "session_id": session_id,
        "summary": summary,
        "resolution_detail": resolution_detail,
    }


def get_audit_log(conn, group_id: str, since: str | None = None) -> dict:
    """At most MAX_ENTRIES entries; `truncated` is true only when more than
    that many entries exist (exactly MAX_ENTRIES is not truncated). Page
    forward with `since` (an ISO8601 lower bound; the returned entries start
    at or after it)."""
    start = time.perf_counter()
    try:
        since_dt = _parse_since(since)
    except ValidationError as e:
        _logger.info(
            "get_audit_log",
            extra={"fields": {"group_id": group_id, "error": str(e)}},
        )
        return {"error": str(e)}

    rows = conn.execute(
        f"""
        SELECT {_AUDIT_COLUMNS}
        FROM public.audit_entry
        WHERE group_id = %s
          AND (%s::timestamptz IS NULL OR "timestamp" >= %s::timestamptz)
        -- id breaks the tie, and the tie is the common case: every entry an
        -- episode writes shares one transaction and therefore one
        -- timestamp, so ordering on it alone leaves the sequence to the
        -- database. `why` exists to show what happened in what order, and
        -- a history that reports a supersession before the creation it
        -- superseded is worse than no history. id is a SERIAL and
        -- monotonic within a transaction, so it is the real order.
        ORDER BY "timestamp" ASC, id ASC
        LIMIT %s
        """,
        # Ask for one more than we return: with exactly MAX_ENTRIES rows in
        # the store, LIMIT MAX_ENTRIES cannot tell "that is all there is" from
        # "there is more", and the flag would be wrong in the one case it
        # exists to get right.
        (group_id, since_dt, since_dt, MAX_ENTRIES + 1),
    ).fetchall()

    truncated = len(rows) > MAX_ENTRIES
    entries = [_row_to_entry(row) for row in rows[:MAX_ENTRIES]]

    _logger.info(
        "get_audit_log",
        extra={
            "fields": {
                "group_id": group_id,
                "n_entries": len(entries),
                "duration_ms": round((time.perf_counter() - start) * 1000, 1),
            }
        },
    )
    return {"entries": entries, "truncated": truncated}


def get_fact_history(conn, group_id: str, fact_id: str) -> dict:
    """Every audit entry that mentions this fact_id, whether as the subject
    of a `created`/`entity_resolved` entry or as either side of a
    `fact_superseded` pair (see the CEO plan's `echo-memory why` spec: reads
    audit_entry rows for that fact's edge, not a full multi-hop supersession
    chain across every fact this one ever descended from or led to)."""
    rows = conn.execute(
        f"""
        SELECT {_AUDIT_COLUMNS}
        FROM public.audit_entry
        WHERE group_id = %s AND %s = ANY(affected_edge_ids)
        -- id breaks the tie, and the tie is the common case: every entry an
        -- episode writes shares one transaction and therefore one
        -- timestamp, so ordering on it alone leaves the sequence to the
        -- database. `why` exists to show what happened in what order, and
        -- a history that reports a supersession before the creation it
        -- superseded is worse than no history. id is a SERIAL and
        -- monotonic within a transaction, so it is the real order.
        ORDER BY "timestamp" ASC, id ASC
        """,
        (group_id, fact_id),
    ).fetchall()
    return {"entries": [_row_to_entry(row) for row in rows]}
