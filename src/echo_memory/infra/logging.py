"""Structured server-side logging: one JSON object per line to STDERR, no
raw fact/entity text (see log_write_episode's summary-only fields), so logs
are safe to ship off-box without also shipping user memory content.

Stderr, not stdout, and that is load-bearing rather than a style choice: the
MCP server speaks its protocol over stdout, so a log line written there would
be framed as a protocol message and corrupt the stream.

Fields may be passed either way. `extra={"fields": {...}}` is the original
shape; `extra={"k": v}` is the idiom Python's logging docs teach, and every
call site that reached for it was silently dropping every field it passed,
because the formatter only ever read `record.fields`. Both now work."""

import json
import logging
import sys
import time

# Everything logging puts on a LogRecord itself. Anything else on the record
# arrived via extra= and is therefore a field somebody meant to log.
_STANDARD_RECORD_ATTRS = frozenset(
    ["args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName", "levelname", "levelno", "lineno", "message", "module", "msecs", "msg", "name", "pathname", "process", "processName", "relativeCreated", "stack_info", "taskName", "thread", "threadName"]
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.time(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(
            {
                key: value
                for key, value in record.__dict__.items()
                if key not in _STANDARD_RECORD_ATTRS and key != "fields"
            }
        )
        nested = getattr(record, "fields", None)
        if nested:
            payload.update(nested)
        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """Install the JSON formatter on the echo_memory logger tree.

    Call this once at process start. Until PR #8 nothing did, so the formatter
    was dead code and every server log line fell through to Python's
    last-resort handler as bare text."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger("echo_memory")
    root.setLevel(level)
    root.handlers = [handler]
    root.propagate = False


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"echo_memory.{name}")


def log_write_episode(
    logger: logging.Logger,
    group_id: str,
    session_id: str,
    n_entities: int,
    n_facts: int,
    n_edges_created: int,
    n_ambiguous: int,
    duration_ms: float,
    error: str | None = None,
    stored: bool | None = None,
    not_stored: str | None = None,
) -> None:
    """Counts and timing only, never the actual entity/fact text: that's
    user memory content, not operational telemetry.

    `stored` and `not_stored` are here because every other field answered
    "did a write happen" and none answered "did an attempted write fail".
    A deferred episode logged n_edges_created 0, n_ambiguous 3 and error
    null, at INFO, which is indistinguishable from a successful write unless
    the reader knows to reason about n_ambiguous. The two incidents this
    project has been burned by were both invisible in this line.
    """
    logger.info(
        "write_episode",
        extra={
            "fields": {
                "group_id": group_id,
                "session_id": session_id,
                "n_entities": n_entities,
                "n_facts": n_facts,
                "n_edges_created": n_edges_created,
                "n_ambiguous": n_ambiguous,
                "duration_ms": round(duration_ms, 1),
                "error": error,
                "stored": stored,
                "not_stored": not_stored,
            }
        },
    )


def log_query_memory(
    logger: logging.Logger,
    group_id: str,
    n_vector_candidates: int,
    n_lexical_candidates: int,
    n_results: int,
    duration_ms: float,
    error: str | None = None,
) -> None:
    """Counts and timing only, never the query text or fact content."""
    logger.info(
        "query_memory",
        extra={
            "fields": {
                "group_id": group_id,
                "n_vector_candidates": n_vector_candidates,
                "n_lexical_candidates": n_lexical_candidates,
                "n_results": n_results,
                "duration_ms": round(duration_ms, 1),
                "error": error,
            }
        },
    )
