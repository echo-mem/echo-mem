"""End-to-end tests for get_audit_log against a real Postgres+AGE+pgvector
database. See conftest.py for the migrated_db fixture and DB-reachability
skip."""

from fake_embedder import REFERENCE, VectorEmbedder

from echo_memory.audit.get_audit_log import MAX_ENTRIES, get_audit_log
from echo_memory.infra.db import connect
from echo_memory.ingestion.write_episode import write_episode


def _write(conn, embedder, group_id, session_id, source, target, relation_type, fact):
    return write_episode(
        conn,
        group_id,
        session_id,
        [{"name": source, "type": "tool"}, {"name": target, "type": "tool"}],
        [
            {
                "source": source,
                "target": target,
                "relation_type": relation_type,
                "fact": fact,
                "confidence": "extracted",
            }
        ],
        {},
        embedder,
    )


def test_created_entry_recorded(migrated_db):
    conn = connect(migrated_db)
    embedder = VectorEmbedder({"Decision": REFERENCE, "Postgres": REFERENCE, "uses Postgres": REFERENCE})
    _write(conn, embedder, "g1", "sess-1", "Decision", "Postgres", "uses", "uses Postgres")

    result = get_audit_log(conn, "g1")

    created = [e for e in result["entries"] if e["mutation_type"] == "created"]
    assert len(created) == 1
    assert created[0]["session_id"] == "sess-1"
    assert created[0]["affected_edge_ids"] != []
    assert created[0]["affected_node_id"] is None


def test_fact_superseded_records_before_and_after(migrated_db):
    conn = connect(migrated_db)
    embedder = VectorEmbedder(
        {
            "Decision": REFERENCE,
            "Postgres": REFERENCE,
            "using SQLite for now": REFERENCE,
            "switched to Postgres for durability": REFERENCE,
        }
    )
    _write(conn, embedder, "g1", "s1", "Decision", "Postgres", "uses", "using SQLite for now")
    _write(
        conn, embedder, "g1", "s2", "Decision", "Postgres", "uses",
        "switched to Postgres for durability",
    )

    result = get_audit_log(conn, "g1")

    superseded = [e for e in result["entries"] if e["mutation_type"] == "fact_superseded"]
    assert len(superseded) == 1
    assert superseded[0]["before_fact"] == "using SQLite for now"
    assert superseded[0]["after_fact"] == "switched to Postgres for durability"
    assert len(superseded[0]["affected_edge_ids"]) == 2


def test_entity_resolved_records_node_id_and_detail(migrated_db):
    conn = connect(migrated_db)
    embedder = VectorEmbedder({"Postgres": REFERENCE, "postgres": REFERENCE, "uses postgres": REFERENCE})
    _write(conn, embedder, "g1", "s1", "Postgres", "Postgres", "self", "uses postgres")
    # second call resolves "postgres" via exact (case-insensitive) match
    write_episode(
        conn, "g1", "s2",
        [{"name": "postgres", "type": "tool"}],
        [],
        {},
        embedder,
    )

    result = get_audit_log(conn, "g1")

    resolved = [e for e in result["entries"] if e["mutation_type"] == "entity_resolved"]
    assert any(e["resolution_detail"] == "exact match" for e in resolved)
    assert all(e["affected_node_id"] is not None for e in resolved)


def test_entries_ordered_chronologically(migrated_db):
    conn = connect(migrated_db)
    embedder = VectorEmbedder({"A": REFERENCE, "B": REFERENCE, "fact one": REFERENCE, "fact two": REFERENCE})
    _write(conn, embedder, "g1", "s1", "A", "B", "rel1", "fact one")
    _write(conn, embedder, "g1", "s2", "A", "B", "rel2", "fact two")

    result = get_audit_log(conn, "g1")

    timestamps = [e["timestamp"] for e in result["entries"]]
    assert timestamps == sorted(timestamps)


def test_since_filters_out_earlier_entries(migrated_db):
    conn = connect(migrated_db)
    embedder = VectorEmbedder({"A": REFERENCE, "B": REFERENCE, "fact one": REFERENCE, "fact two": REFERENCE})
    _write(conn, embedder, "g1", "s1", "A", "B", "rel1", "fact one")
    midpoint = get_audit_log(conn, "g1")["entries"][-1]["timestamp"]
    _write(conn, embedder, "g1", "s2", "A", "B", "rel2", "fact two")

    result = get_audit_log(conn, "g1", since=midpoint)

    assert all(e["timestamp"] >= midpoint for e in result["entries"])
    assert any(e["session_id"] == "s2" for e in result["entries"])


def test_scoped_to_group_id(migrated_db):
    conn = connect(migrated_db)
    embedder = VectorEmbedder({"A": REFERENCE, "B": REFERENCE, "g1 fact": REFERENCE, "g2 fact": REFERENCE})
    _write(conn, embedder, "g1", "s1", "A", "B", "rel", "g1 fact")
    _write(conn, embedder, "g2", "s1", "A", "B", "rel", "g2 fact")

    result = get_audit_log(conn, "g1")

    assert all(e["session_id"] != "g2-only" for e in result["entries"])
    summaries = [e["summary"] for e in result["entries"]]
    assert any("g1 fact" in s for s in summaries)
    assert not any("g2 fact" in s for s in summaries)


def test_empty_group_returns_empty_list_not_error(migrated_db):
    conn = connect(migrated_db)
    result = get_audit_log(conn, "nonexistent-group")
    assert result == {"entries": [], "truncated": False}


def test_validation_rejects_malformed_since(migrated_db):
    conn = connect(migrated_db)
    result = get_audit_log(conn, "g1", since="not-a-timestamp")
    assert "error" in result


def test_entries_from_one_episode_come_back_in_the_order_they_happened(migrated_db):
    """A creation and the supersession it caused are written in one
    transaction, so they carry the same timestamp. Ordering on timestamp alone
    left the sequence to the database, and it duly came back reversed in one
    full-suite run while passing in isolation - a history that reports a
    supersession before the creation it superseded is worse than none.

    Same bug class as the traversal channel's unspecified tie order, found the
    same week: a sort key that is not unique, holding for a while by luck.
    """
    import json as _json

    group = "user:order:shared"
    with connect(migrated_db) as conn:
        for _ in range(6):
            conn.execute("DELETE FROM public.audit_entry WHERE group_id = %s", (group,))
            conn.execute(
                """INSERT INTO public.audit_entry (group_id, session_id, mutation_type, summary)
                   VALUES (%s, 's', 'created', 'a'), (%s, 's', 'fact_superseded', 'b')""",
                (group, group),
            )
            kinds = [e["mutation_type"] for e in get_audit_log(conn, group)["entries"]]
            assert kinds == ["created", "fact_superseded"], _json.dumps(kinds)


def test_truncated_flag_is_false_at_499_and_500_and_true_at_501(migrated_db):
    """truncated must be true only when more than MAX_ENTRIES entries exist.

    Exactly MAX_ENTRIES used to report truncated=True because a query for
    MAX_ENTRIES rows cannot tell "that is all there is" from "there is more".
    Asking the database for one extra row makes the boundary exact, so 499 and
    the 500-entry cap are both false and only 501 is true. The 500 case is the
    whole point and is the one the previous test could not express.
    """
    conn = connect(migrated_db)

    def count(group: str) -> int:
        # Count in SQL rather than reading through get_audit_log: the reader
        # returns at most MAX_ENTRIES, so it cannot report the 501-entry case.
        return conn.execute(
            "SELECT count(*) FROM public.audit_entry WHERE group_id = %s",
            (group,),
        ).fetchone()[0]

    def write_one(group: str, n: int) -> None:
        # entity names reach the embedder inside "<source> <target>. <fact>";
        # register the fact text so the fallback in fake_embedder resolves it,
        # and the name itself for the node embedding
        embedder = VectorEmbedder({f"entry {n}": REFERENCE, f"tool-{n}": REFERENCE})
        write_episode(
            conn, group, f"sess-{n}",
            [{"name": f"tool-{n}", "type": "tool"}],
            [{
                "source": f"tool-{n}",
                "target": f"tool-{n}",
                "relation_type": "uses",
                "fact": f"entry {n}",
                "confidence": "extracted",
            }],
            {f"tool-{n}": {"resolved_to": "new"}},
            embedder,
            assume_new=True,
        )

    # Derive how many audit entries one episode writes from the log itself
    # rather than assuming a number, so this survives a change in an episode's
    # footprint (it is more than one entry: a created row plus its resolution).
    probe = "g-trunc-probe"
    write_one(probe, 0)
    per_episode = count(probe)
    assert per_episode >= 1

    def fill_to(group: str, target: int) -> None:
        # Whole episodes carry the group most of the way while keeping their
        # real footprint; direct rows then land it exactly on the boundary,
        # because 499 and 501 are not multiples of a two-entry episode.
        n = 0
        while count(group) + per_episode <= target:
            write_one(group, n)
            n += 1
        for _ in range(target - count(group)):
            conn.execute(
                """INSERT INTO public.audit_entry (group_id, session_id, mutation_type, summary)
                   VALUES (%s, 's', 'created', 'pad')""",
                (group,),
            )
        assert count(group) == target

    for target, expected in ((499, False), (MAX_ENTRIES, False), (MAX_ENTRIES + 1, True)):
        group = f"g-trunc-{target}"
        fill_to(group, target)
        result = get_audit_log(conn, group)
        assert len(result["entries"]) == min(target, MAX_ENTRIES), target
        assert result["truncated"] is expected, target
