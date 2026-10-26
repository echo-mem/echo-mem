"""Mem0, hosted.

The most cited comparison in this category, and the one whose write path is
the thing being compared: Mem0 calls a model on every write to turn a turn
into a "memory", which is exactly the cost Echo Memory does not pay.

Three things measured against the live API on 2026-10-27, each of which the
obvious implementation gets wrong:

**`add` is asynchronous.** It returns `{"event_id": ..., "status": "PENDING"}`
and the memory does not exist yet. Three single turn adds returned in well
under a second and took about five seconds to appear in `get_all`. Hence
`settle`, and hence it being part of the protocol rather than a detail in here:
a harness that ingests and immediately queries reports Mem0 at zero, which is
the most embarrassing way to win a benchmark.

**`search` will not take `user_id` as a keyword**, only
`filters={"user_id": ...}`, and it raises if you pass it the way `add` accepts
it. The two calls are inconsistent and the error only arrives at call time.

**`metadata` survives extraction.** A `turn_key` passed to `add` comes back on
the search result even with `infer=True`, the mode where a model rewrites the
turn. This was the opposite of what was assumed, and it is good news: it means
Mem0's results can be matched to gold turns, so the turn-keyed metrics are
computable for it rather than null.

It is not guaranteed, which is why `attribution_rate` is reported per run.
Mem0 merges and replaces memories - the stored records carry `replaced_by` and
`synthesized` - and a memory synthesised from several turns cannot carry one
turn's key. Those results score as a miss, correctly, because they genuinely
cannot be credited to a gold turn; the rate is what tells a reader how much of
the answer that was.

**Attribution can be present and still be wrong, which is worse than absent.**
The first end to end run on the LoCoMo fixture returned `attribution_rate`
1.0 - every memory carried a turn key - with `recall@k` 0.000 and
`session@10` 0.750. The sessions were right and the turns were not. A memory
Mem0 synthesised from several turns carries ONE of their keys, so a result
whose content answers the question can be keyed to a sibling turn and score as
a miss. `attribution_rate` measures whether a key was there, not whether it was
the right one, and nothing from outside can tell the difference.

So the turn-keyed metrics understate Mem0 by an unknown amount, and that
belongs in the writeup rather than in a footnote. `session@k` is the more
honest turn-keyed comparison for it, being coarser, and `answer_words@k` and
the judged answer need no attribution at all. Four questions on a twelve turn
fixture say nothing statistically; the mechanism is what was being checked.

`infer` is left at the default True on purpose. `infer=False` stores the turn
verbatim with no model call, which would be a much faster and cheaper Mem0 and
would not be the product anybody buys. Benchmarking the mode nobody runs would
flatter them and mislead the reader.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from echo_memory.eval.backends import BackendUnavailable, _env

#: How long to wait for the hosted queue to drain, and how often to look.
#: Generous because the penalty for being impatient is scoring a competitor as
#: having stored nothing.
SETTLE_TIMEOUT = 900
SETTLE_POLL = 5


class Mem0Backend:
    name = "mem0"
    attributes_results = True

    def __init__(self, infer: bool = True):
        self._infer = infer
        self._client = None

    def check(self) -> None:
        key = _env("MEM0_API_KEY")
        try:
            from mem0 import MemoryClient
        except ImportError as exc:  # pragma: no cover - depends on the env
            raise BackendUnavailable(
                "mem0ai is not installed. The benchmark backends pull heavy and "
                "mutually incompatible dependency trees, so they are not in this "
                "project's requirements: install them into a separate "
                "environment. See docs/BENCHMARKS.md."
            ) from exc
        self._client = MemoryClient(api_key=key)

    def _user(self, group_id: str) -> str:
        """Mem0's tenant key. The instance's group id, used verbatim so a run
        can be inspected in their dashboard afterwards and a resumed run finds
        what the first one wrote."""
        return group_id

    def already_ingested(self, group_id: str, expected: int) -> bool:
        """Mem0 stores a DERIVED number of memories, not one per turn, so
        "does it hold exactly `expected`" is the wrong question and would never
        be true. Non-empty is the most that can honestly be asked, which makes
        resume coarser here than for Echo Memory: a scope half ingested looks
        done. Stated rather than hidden, because it means a killed Mem0 run
        should be restarted into a fresh scope.
        """
        got = self._client.get_all(filters={"user_id": self._user(group_id)}, page_size=1)
        items = got.get("results", got) if isinstance(got, dict) else got
        return bool(items)

    def ingest(self, instance, progress: Callable[[str], None] | None = None) -> int:
        user = self._user(instance.group_id)
        written = 0
        started = time.time()
        for turn in instance.turns:
            self._client.add(
                [{"role": "user", "content": f"{turn.speaker}: {turn.fact}"}],
                user_id=user,
                metadata={"turn_key": turn.key},
                infer=self._infer,
            )
            written += 1
            if progress and written % 100 == 0:
                rate = written / max(time.time() - started, 1e-9)
                progress(f"  {instance.group_id}: {written:,} turns queued, {rate:.1f}/s")
        return written

    def settle(self, group_id: str, expected: int,
               progress: Callable[[str], None] | None = None) -> None:
        """Wait for the queue, then say what actually landed.

        It cannot wait for a count, because extraction decides how many
        memories `expected` turns become. So it waits for the number to stop
        moving: two consecutive polls with the same total, which is the only
        signal available from outside.
        """
        user = self._user(group_id)
        deadline = time.time() + SETTLE_TIMEOUT
        previous = -1
        while time.time() < deadline:
            time.sleep(SETTLE_POLL)
            total = self._count(user)
            if total and total == previous:
                if progress:
                    progress(
                        f"  {group_id}: settled at {total:,} memories from "
                        f"{expected:,} turns"
                    )
                return
            previous = total
        if progress:
            progress(
                f"  {group_id}: WARNING still moving after {SETTLE_TIMEOUT}s at "
                f"{previous:,} memories; scoring anyway and the result "
                f"understates mem0"
            )

    def _count(self, user: str) -> int:
        total = 0
        page = 1
        while True:
            got = self._client.get_all(
                filters={"user_id": user}, page=page, page_size=100
            )
            items = got.get("results", got) if isinstance(got, dict) else got
            if not items:
                return total
            total += len(items)
            if len(items) < 100:
                return total
            page += 1

    def query(self, group_id: str, question: str, top_k: int) -> list[dict]:
        res = self._client.search(
            question, filters={"user_id": self._user(group_id)}, top_k=top_k
        )
        items = res.get("results", res) if isinstance(res, dict) else res
        out = []
        for item in items or []:
            key = (item.get("metadata") or {}).get("turn_key") or ""
            out.append({
                "fact": item.get("memory") or "",
                # Shaped like Echo Memory's provenance so score_question reads
                # one thing, not one per backend.
                "provenance": {"project": key},
                "score": item.get("score"),
            })
        return out
