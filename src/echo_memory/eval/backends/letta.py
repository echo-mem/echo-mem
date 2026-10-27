"""Letta, self hosted.

Letta is the odd one of the four and the comparison is more interesting for it.
Mem0 and Zep call a model on every write to turn a turn into a derived memory.
Letta's archival memory is a passage store: `agents.passages.create` takes text
and tags, and retrieval is `agents.passages.search`. Inserting a passage embeds
it; it does not run an extraction model over it.

So Letta should look like Echo Memory on write cost and unlike Mem0 and Zep,
and if the full run says otherwise that is the interesting result rather than a
bug. Saying this in advance is deliberate: a prediction written before the
measurement is checkable, and one written after is not.

The SDK shapes below were read off letta-client rather than guessed, which the
Mem0 and Zep adapters established as necessary - every assumption about both of
those was wrong:

    agents.passages.create(agent_id, text=..., tags=[...])
    agents.passages.search(agent_id, query=..., top_k=..., tags=...)

A search result is `{passage, score, metadata}` where the passage carries
`text` and `tags`, so the turn key goes in `tags` and comes back there. `tags`
is a list of strings, not a mapping, which is why the key is stored bare rather
than as `turn_key=...`: it is matched against the gold set by value anyway.

**Two things it needs that the hosted backends do not.** A server, at
LETTA_BASE_URL (default http://localhost:8283), and a model provider
configured IN THAT SERVER for the embedding model, which a passage insert
needs even though no extraction model runs. Neither is something this adapter
can supply, so `check` reports them separately and by name.

**Unverified against a live server at the time of writing.** The API shapes are
read from the installed SDK and the adapter is written to them, but no ingest
has run, because the server needs a provider key this machine does not have.
Marked here rather than in a commit message nobody will read again: the first
real run may well find something, as it did for both hosted backends.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable

from echo_memory.eval.backends import BackendUnavailable

DEFAULT_BASE_URL = "http://localhost:8283"
BASE_URL_VAR = "LETTA_BASE_URL"
TOKEN_VAR = "LETTA_TOKEN"

#: The agent Letta hangs archival memory off. One per instance, named after the
#: scope so a resumed run finds it, for the same reason Zep looks its graph up
#: by name.
AGENT_PREFIX = "echo-bench"


class LettaBackend:
    name = "letta"
    attributes_results = True

    def __init__(self, model: str = "", embedding: str = ""):
        # Named by the caller, never defaulted: picking a model is choosing how
        # much of somebody's money to spend, and for a comparison it is also
        # choosing what is being compared.
        self._model = model or os.environ.get("LETTA_MODEL", "")
        self._embedding = embedding or os.environ.get("LETTA_EMBEDDING", "")
        self._client = None
        self._agents: dict[str, str] = {}

    def check(self) -> None:
        try:
            from letta_client import Letta
        except ImportError as exc:  # pragma: no cover - depends on the env
            raise BackendUnavailable(
                "letta-client is not installed. The benchmark backends pull "
                "heavy and mutually incompatible dependency trees, so they are "
                "not in this project's requirements: install them into a "
                "separate environment. See docs/BENCHMARKS.md."
            ) from exc
        if not self._model or not self._embedding:
            raise BackendUnavailable(
                "set LETTA_MODEL and LETTA_EMBEDDING. Letta needs both named: "
                "the embedding model because inserting a passage embeds it, and "
                "the chat model because creating an agent to hang archival "
                "memory off requires one even though no extraction model runs "
                "on write. Neither is defaulted, because for a benchmark the "
                "choice of model is part of what is being measured."
            )
        base = os.environ.get(BASE_URL_VAR, "").strip() or DEFAULT_BASE_URL
        token = os.environ.get(TOKEN_VAR, "").strip()
        self._client = Letta(base_url=base, token=token) if token else Letta(base_url=base)
        # client.health() is a METHOD, not a resource with .retrieve(). Worth
        # being exact about: the first version called health.retrieve() and the
        # AttributeError was caught by the except below, so a coding mistake in
        # here was reported to the operator as "no Letta server at ...". An
        # error handler that can describe its own bug as the user's problem is
        # worse than no error handler.
        try:
            self._client.health()
        except AttributeError:
            raise
        except Exception as exc:  # any transport failure has the same answer
            raise BackendUnavailable(
                f"no Letta server at {base}: {exc}. Unlike mem0 and zep there "
                f"is no hosted endpoint; start one, e.g. docker run -d "
                f"-p 8283:8283 letta/letta:latest, with its provider key in "
                f"the container's environment."
            ) from exc

    def _agent(self, group_id: str) -> str:
        """This scope's agent id, creating it if it is new.

        Looked up by name, and the lookup takes the LOWEST id among any
        duplicates rather than whichever came first, for the reason Zep taught:
        a name-based lookup that is not deterministic can resume into an empty
        twin and score the whole scope as a miss.
        """
        if group_id in self._agents:
            return self._agents[group_id]
        wanted = f"{AGENT_PREFIX}:{group_id}"
        found = [
            a.id for a in self._client.agents.list(limit=200)
            if getattr(a, "name", None) == wanted
        ]
        if found:
            self._agents[group_id] = min(found)
            return self._agents[group_id]
        created = self._client.agents.create(
            name=wanted,
            model=self._model,
            embedding=self._embedding,
            # No tools and no base tools: this agent exists to own a passage
            # store, and is never asked to reason. A benchmark that let the
            # agent decide what to retrieve would be measuring the agent.
            include_base_tools=False,
            description="echo-mem external benchmark: archival memory only",
        )
        self._agents[group_id] = created.id
        return created.id

    def already_ingested(self, group_id: str, expected: int) -> bool:
        """Passages are one per turn, so the count is comparable and resume can
        be exact, as with Zep's episodes and unlike Mem0's derived memories."""
        wanted = f"{AGENT_PREFIX}:{group_id}"
        existing = [
            a.id for a in self._client.agents.list(limit=200)
            if getattr(a, "name", None) == wanted
        ]
        if not existing:
            return False
        agent_id = min(existing)
        self._agents[group_id] = agent_id
        return len(self._passages(agent_id)) >= expected

    def _passages(self, agent_id: str) -> list:
        """Every passage, following the cursor.

        One page would truncate silently, which understates both the resume
        check and nothing else here - but it is the same trap Zep's episode
        list had and worth not stepping in twice.
        """
        out: list = []
        after = None
        while True:
            page = self._client.agents.passages.list(
                agent_id, limit=100, **({"after": after} if after else {})
            )
            items = list(page or [])
            if not items:
                return out
            out.extend(items)
            if len(items) < 100:
                return out
            after = getattr(items[-1], "id", None)
            if not after:
                return out

    def ingest(self, instance, progress: Callable[[str], None] | None = None) -> int:
        agent_id = self._agent(instance.group_id)
        written = 0
        started = time.time()
        for turn in instance.turns:
            self._client.agents.passages.create(
                agent_id,
                text=f"{turn.speaker}: {turn.fact}",
                # A bare value, because tags are a list of strings and the key
                # is matched against the gold set by value.
                tags=[turn.key],
            )
            written += 1
            if progress and written % 200 == 0:
                rate = written / max(time.time() - started, 1e-9)
                progress(f"  {instance.group_id}: {written:,} passages, {rate:.1f}/s")
        return written

    def settle(self, group_id: str, expected: int,
               progress: Callable[[str], None] | None = None) -> None:
        """Nothing to wait for, as far as the API says: a passage insert
        returns the stored passage rather than a queued job, unlike Mem0's
        PENDING. If a full run turns out to disagree, this is where the wait
        goes."""

    def query(self, group_id: str, question: str, top_k: int) -> list[dict]:
        agent_id = self._agent(group_id)
        found = self._client.agents.passages.search(
            agent_id, query=question, top_k=top_k
        )
        out = []
        for item in list(found or []):
            passage = getattr(item, "passage", None) or item
            tags = list(getattr(passage, "tags", None) or [])
            out.append({
                "fact": getattr(passage, "text", "") or "",
                "provenance": {"projects": tags},
                "score": getattr(item, "score", None),
            })
        return out
