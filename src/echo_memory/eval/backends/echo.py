"""Echo Memory itself, as a backend.

This is the behaviour `eval/external.py` had inline, moved behind the protocol
unchanged so the competitor numbers are produced by the same harness as ours.
A comparison where our side ran through one code path and theirs through
another is measuring the harness.

It is the only backend with `attributes_results = True`. One turn becomes one
fact, the turn key goes in as `project` and comes back in the fact's
provenance, so matching a result to a gold turn is exact rather than inferred.
That is a property of storing the turns rather than something derived from
them, and it is the reason the turn-keyed metrics exist at all.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from echo_memory.ingestion.write_episode import write_episode
from echo_memory.retrieval.query_memory import query_memory


class WriteRefused(RuntimeError):
    """A turn the store would not take. Never swallowed: a benchmark that
    quietly drops the turns it finds awkward is measuring a corpus nobody
    has, and in LongMemEval the refused ones are the long turns, which are
    disproportionately the ones carrying an answer."""


class EchoBackend:
    name = "echo-mem"
    attributes_results = True

    def __init__(self, conn, embedder, agent_id: str = "external-benchmark"):
        self._conn = conn
        self._embedder = embedder
        self._agent_id = agent_id

    def check(self) -> None:
        """Nothing to check. The connection and the embedder were built by the
        caller, so a failure has already happened by now."""

    def already_ingested(self, group_id: str, expected: int) -> bool:
        from echo_memory.eval.external import already_ingested

        return already_ingested(self._conn, group_id, expected)

    def ingest(self, instance, progress: Callable[[str], None] | None = None) -> int:
        """One fact per turn, keyed so scoring can find it again.

        Both entity mentions are asserted as new, and both assertions are load
        bearing. Without the utterance one the turns supersede each other: a
        fact is an edge keyed by (source, target, relation_type), so
        `speaker --said--> session` writes one fact per session, and 419 turns
        became 18. Without the speaker one, two names that embed near each
        other are flagged ambiguous and every fact touching the second is
        DEFERRED rather than written, with nothing raised - Tim and John in
        LoCoMo's conv-43, which cost 344 of 680 turns and looked like a
        successful ingest. An exact name match still overrides the assertion,
        so a speaker's second turn resolves onto the node their first one made.
        """
        written = 0
        started = time.time()
        for turn in instance.turns:
            result = write_episode(
                self._conn, instance.group_id, turn.session,
                [{"name": turn.speaker, "type": "speaker"},
                 {"name": turn.node, "type": "utterance"}],
                [{"source": turn.speaker, "target": turn.node,
                  "relation_type": "said", "fact": turn.fact,
                  "confidence": "extracted"}],
                {turn.node: {"resolved_to": "new"},
                 turn.speaker: {"resolved_to": "new"}},
                self._embedder,
                project=turn.key,
                agent_id=self._agent_id,
            )
            if not result.get("edges_created"):
                raise WriteRefused(
                    f"write refused for {turn.node} in {instance.group_id}: "
                    f"{result.get('error') or result}"
                )
            written += 1
            # Reported during ingest rather than only after it, because a long
            # run is hours of silence otherwise, and the rate is the number
            # that says whether it is still worth waiting for: write
            # throughput decays as the database grows (29ms to 129ms across
            # 1,057 to 24,054 nodes, see docs/BENCHMARKS.md), so a run can slow
            # to a stop without erroring.
            if progress and written % 500 == 0:
                rate = written / max(time.time() - started, 1e-9)
                progress(f"  {instance.group_id}: {written:,} turns written, {rate:.0f}/s")
        return written

    def settle(self, group_id: str, expected: int,
               progress: Callable[[str], None] | None = None) -> None:
        """Nothing to wait for. write_episode has committed and read back
        before it returns, which is the property the whole product is about."""

    def query(self, group_id: str, question: str, top_k: int) -> list[dict]:
        found = query_memory(self._conn, group_id, question, top_k, self._embedder)
        return found.get("facts", [])
