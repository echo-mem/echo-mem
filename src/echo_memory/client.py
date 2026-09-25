"""Direct Python API for agents that don't speak MCP: a DevOps agent, a
booking agent, a plain chatbot loop, anything with its own function-calling
system. Same engine as the MCP server (server.py) - write_episode,
query_memory, get_audit_log - called in-process instead of over stdio, so
there's no separate server process to run and no protocol framing to worry
about. See docs/INTEGRATIONS.md for worked examples.

The one thing this class does NOT do, same as the MCP server: it never
calls an LLM itself. entities/facts must already be extracted by the
calling agent before write_episode is called; see the design doc's MCP
tool contract for the entity_resolutions round-trip."""

from echo_memory.audit.get_audit_log import get_audit_log as _get_audit_log
from echo_memory.infra.config import Config, ConfigError, load_config
from echo_memory.infra.pool import make_pool
from echo_memory.ingestion.embeddings import Embedder, LocalEmbedder
from echo_memory.ingestion.write_episode import write_episode as _write_episode
from echo_memory.retrieval.query_memory import query_memory as _query_memory


class EchoMemory:
    """One instance per agent process; holds a connection pool and embedder
    for the process's lifetime. Reads ECHO_MEMORY_USER_ID/AGENT_ID/
    DATABASE_URL from the environment unless a Config is passed explicitly."""

    def __init__(self, config: Config | None = None, embedder: Embedder | None = None):
        self._config = config or load_config()
        self._pool = make_pool(self._config.database_url)
        self._embedder = embedder or LocalEmbedder()

    def write_episode(
        self,
        scope: str,
        session_id: str,
        entities: list[dict],
        facts: list[dict],
        entity_resolutions: dict | None = None,
        assume_new: bool = False,
    ) -> dict:
        try:
            group_id = self._config.group_id(scope)
        except ConfigError as e:
            return {"error": str(e)}
        with self._pool.connection() as conn:
            return _write_episode(
                conn, group_id, session_id, entities, facts, entity_resolutions, self._embedder,
                project=self._config.project, agent_id=self._config.agent_id,
                assume_new=assume_new,
            )

    def query_memory(
        self, scope: str, query: str | None = None, top_k: int = 10, digest: bool = False
    ) -> dict:
        try:
            group_id = self._config.group_id(scope)
        except ConfigError as e:
            return {"error": str(e)}
        with self._pool.connection() as conn:
            return _query_memory(conn, group_id, query, top_k, self._embedder, digest=digest)

    def get_audit_log(self, scope: str, since: str | None = None) -> dict:
        try:
            group_id = self._config.group_id(scope)
        except ConfigError as e:
            return {"error": str(e)}
        with self._pool.connection() as conn:
            return _get_audit_log(conn, group_id, since)
