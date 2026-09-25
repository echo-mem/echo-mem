"""End-to-end tests for EchoMemory, the direct Python API for non-MCP
agents (see client.py and docs/INTEGRATIONS.md). Mirrors test_server.py's
coverage since it's the same engine, just a different entry point - no
stdio, no @server.tool() wiring, plain method calls."""

from fake_embedder import REFERENCE, VectorEmbedder, unit_vector_at_angle

from echo_memory.client import EchoMemory
from echo_memory.infra.config import Config


def _client(migrated_db, embedder=None):
    config = Config(user_id="ayush", agent_id="devops-bot", database_url=migrated_db)
    return EchoMemory(config=config, embedder=embedder or VectorEmbedder({}))


def test_write_query_audit_round_trip(migrated_db):
    mem = _client(
        migrated_db,
        VectorEmbedder(
            {
                "prod-db": REFERENCE,
                "Incident": REFERENCE,
                "prod-db ran out of disk at 2am, resized to 500GB": REFERENCE,
            }
        ),
    )

    write_result = mem.write_episode(
        "solo",
        "sess-1",
        [{"name": "prod-db", "type": "resource"}, {"name": "Incident", "type": "event"}],
        [
            {
                "source": "Incident",
                "target": "prod-db",
                "relation_type": "affected",
                "fact": "prod-db ran out of disk at 2am, resized to 500GB",
                "confidence": "extracted",
            }
        ],
    )
    assert len(write_result["edges_created"]) == 1

    query_result = mem.query_memory("solo", "prod-db ran out of disk at 2am, resized to 500GB", 10)
    assert len(query_result["facts"]) == 1

    audit_result = mem.get_audit_log("solo")
    assert len(audit_result["entries"]) >= 1


def test_invalid_scope_returns_typed_error_not_exception(migrated_db):
    mem = _client(migrated_db)

    assert "error" in mem.write_episode("org", "s1", [], [])
    assert "error" in mem.query_memory("org", "x", 10)
    assert "error" in mem.get_audit_log("org")


def test_digest_available_without_query(migrated_db):
    mem = _client(
        migrated_db,
        VectorEmbedder({"A": REFERENCE, "B": REFERENCE, "server restarted cleanly": REFERENCE}),
    )
    mem.write_episode(
        "solo", "sess-1",
        [{"name": "A", "type": "resource"}, {"name": "B", "type": "event"}],
        [
            {
                "source": "B", "target": "A", "relation_type": "affected",
                "fact": "server restarted cleanly", "confidence": "extracted",
            }
        ],
    )

    result = mem.query_memory("solo", None, 5, digest=True)

    assert result["facts"][0]["fact"] == "server restarted cleanly"


def test_trace_cause_is_reachable_without_mcp(migrated_db):
    """Parity, and the reason it matters: INTEGRATIONS.md sends every non-MCP
    agent to this class, so a tool that exists only on the MCP surface exists
    for half the audience."""
    mem = _client(
        migrated_db,
        VectorEmbedder(
            {
                "pool size 5": REFERENCE,
                "checkout 502s": unit_vector_at_angle(-0.5),
                "the pool was sized 5 and checkout started returning 502s": REFERENCE,
            }
        ),
    )

    written = mem.write_episode(
        "solo",
        "sess-1",
        [{"name": "pool size 5", "type": "config"},
         {"name": "checkout 502s", "type": "incident"}],
        [{
            "source": "pool size 5", "target": "checkout 502s",
            "relation_type": "caused", "confidence": "extracted",
            "causal_hint": "led_to",
            "fact": "the pool was sized 5 and checkout started returning 502s",
        }],
        assume_new=True,
    )
    assert len(written["edges_created"]) == 1

    traced = mem.trace_cause("solo", "checkout 502s", direction="upstream")
    assert [a["name"] for a in traced["anchors"]] == ["checkout 502s"]
    assert [link["fact"] for link in traced["causes"][0]] == [
        "the pool was sized 5 and checkout started returning 502s"
    ]

    assert "error" in mem.trace_cause("org", "anything")
