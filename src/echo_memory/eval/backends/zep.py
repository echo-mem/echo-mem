"""Zep, hosted.

The closest architectural match to Echo Memory and the sharpest comparison:
Zep also builds a temporal knowledge graph of facts with validity intervals,
and its own description of ingestion is that every episode triggers multiple
LLM calls and that write cost scales with volume. That is the claim this
project exists to contradict, so this is the number that matters most.

Measured against the live API on 2026-10-27, since 4.x moved almost everything
from where the older docs put it:

**There is no `graph.add`.** Ingestion is `graph.episode.add(graph_uuid,
data=..., type="message")`, and `graph.create` takes no id at all - it takes a
`name` and the server assigns the uuid. So a scope has to be looked up by name
to resume, which is what `_graph_uuid` does.

**Graph names are not unique.** `graph.create` with a name that already
exists makes a SECOND graph rather than returning the first, which was found
by leaving two graphs both called `locomo:fixture-conv-1`, one holding twelve
episodes and one holding none. A resumed run that looked up by name and took
whichever came first could attach to the empty twin and score the entire scope
as a miss. Hence an `idempotency_key` on create, and a sorted pick among
duplicates so the choice is at worst stable rather than different on each run.

**Retrieval is `graph.search_edges`**, returning facts Zep's model derived,
with the text under `attributes.fact` rather than at the top level.

**And the good part: an edge names its sources.** Every result carries
`episode_uuids`, a LIST of the episodes it was derived from, and each episode
keeps the `metadata` given at ingest. So a fact Zep built from three turns can
be credited to all three.

That is strictly better attribution than Mem0, which carries a single
`turn_key` through extraction even for a memory synthesised from several turns,
and it is the reason `source_turns` accepts `provenance.projects` as a
collection. Scoring one key per result would have credited a three turn fact to
one turn and scored the other two as misses, understating Zep on the metric it
would otherwise win. That shape was wrong in our favour and is fixed before any
number is published.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from echo_memory.eval.backends import BackendUnavailable, _env

SETTLE_TIMEOUT = 1800
SETTLE_POLL = 6


#: Episodes come back a page at a time. 10,000 in one call is not an option -
#: `list` takes page_size and a cursor, not a `lastn` - and LongMemEval puts
#: tens of thousands of turns in one haystack, so paging is load bearing rather
#: than tidiness.
PAGE = 100


def _items(response):
    """Zep wraps a page in different attribute names across endpoints."""
    for attr in ("items", "episodes", "edges", "results"):
        got = getattr(response, attr, None)
        if got:
            return got
    return response if isinstance(response, list) else []


class ZepBackend:
    name = "zep"
    attributes_results = True

    def __init__(self):
        self._client = None
        #: group_id -> graph uuid, and group_id -> {episode uuid: turn key}.
        #: The second is rebuilt from the API on resume rather than kept only
        #: in memory, because a run that resumes in a new process still has to
        #: turn an edge's episode_uuids into turn keys.
        self._graphs: dict[str, str] = {}
        self._episodes: dict[str, dict[str, str]] = {}

    def check(self) -> None:
        key = _env("ZEP_API_KEY")
        try:
            from zep_cloud.client import Zep
        except ImportError as exc:  # pragma: no cover - depends on the env
            raise BackendUnavailable(
                "zep-cloud is not installed. The benchmark backends pull heavy "
                "and mutually incompatible dependency trees, so they are not in "
                "this project's requirements: install them into a separate "
                "environment. See docs/BENCHMARKS.md."
            ) from exc
        self._client = Zep(api_key=key)

    def _all_episodes(self, uuid: str) -> list:
        """Every episode in a graph, following the cursor.

        A single page would silently truncate: LongMemEval haystacks run to
        tens of thousands of turns, and a truncated episode list understates
        both the settle count and the uuid-to-turn map, which would understate
        Zep's recall.
        """
        out: list = []
        cursor = None
        while True:
            kwargs = {"page_size": PAGE}
            if cursor:
                kwargs["cursor"] = cursor
            page = self._client.graph.episode.list(uuid, **kwargs)
            items = _items(page)
            if not items:
                return out
            out.extend(items)
            cursor = getattr(page, "next_cursor", None) or (
                getattr(getattr(page, "response", None), "next_cursor", None)
            )
            if not cursor or len(items) < PAGE:
                return out

    def _existing_graph(self, group_id: str) -> str | None:
        """This scope's graph uuid if it already exists, without creating one.

        Separate from `_graph_uuid` so `already_ingested` can ask "is there
        anything here" without the asking itself creating an empty graph, and
        without catching a blind exception to find out.
        """
        if group_id in self._graphs:
            return self._graphs[group_id]
        matches = [
            getattr(g, "uuid_", None) or getattr(g, "uuid", None)
            for g in _items(self._client.graph.list(page_size=PAGE))
            if getattr(g, "name", None) == group_id
        ]
        if not matches:
            return None
        # SORTED, not "whichever came first". Zep does not enforce unique
        # graph names: calling create twice with the same name silently makes
        # a SECOND graph, and this was found the hard way - a run left two
        # graphs both called locomo:fixture-conv-1, one holding 12 episodes
        # and one holding none. An unordered `next(...)` over that can resume
        # into the empty twin and score the whole scope as a miss, which is
        # the same defect as a SELECT with no ORDER BY feeding a fetchone().
        # Sorting makes the choice wrong-but-stable at worst instead of
        # different on each run; the idempotency key below is what stops the
        # duplicate being created at all.
        uuid = min(u for u in matches if u)
        self._graphs[group_id] = uuid
        return uuid

    def _graph_uuid(self, group_id: str) -> str:
        """The uuid of this scope's graph, creating it if it is new.

        Looked up by NAME because `graph.create` does not accept an id - the
        server assigns the uuid - so the name is the only stable handle a
        resumed run has.
        """
        found = self._existing_graph(group_id)
        if found:
            return found
        # idempotency_key so a retried or re-run ingest cannot fork the scope
        # into two identically named graphs. See _existing_graph.
        created = self._client.graph.create(
            name=group_id,
            description="echo-mem benchmark",
            idempotency_key=group_id,
        )
        uuid = getattr(created, "uuid_", None) or getattr(created, "uuid", None)
        self._graphs[group_id] = uuid
        return uuid

    def _episode_keys(self, group_id: str) -> dict[str, str]:
        """episode uuid -> turn key, read from the API.

        Rebuilt rather than cached across processes, so a resumed run can still
        map an edge's sources back to turns.
        """
        if group_id in self._episodes:
            return self._episodes[group_id]
        uuid = self._graph_uuid(group_id)
        mapping: dict[str, str] = {}
        for episode in self._all_episodes(uuid):
            key = (getattr(episode, "metadata", None) or {}).get("turn_key")
            # source_description is set to the same key at ingest, as a second
            # route to it: metadata is the documented place and the one that
            # came back in testing, but losing attribution silently would
            # understate Zep and this costs nothing.
            key = key or getattr(episode, "source_description", None)
            episode_uuid = getattr(episode, "uuid_", None) or getattr(episode, "uuid", None)
            if key and episode_uuid:
                mapping[episode_uuid] = key
        self._episodes[group_id] = mapping
        return mapping

    def already_ingested(self, group_id: str, expected: int) -> bool:
        """Episodes are one per add, unlike Mem0's derived memories, so the
        count IS comparable and resume can be exact here."""
        uuid = self._existing_graph(group_id)
        if uuid is None:
            return False
        return len(self._all_episodes(uuid)) >= expected

    def ingest(self, instance, progress: Callable[[str], None] | None = None) -> int:
        uuid = self._graph_uuid(instance.group_id)
        written = 0
        started = time.time()
        for turn in instance.turns:
            self._client.graph.episode.add(
                uuid,
                data=f"{turn.speaker}: {turn.fact}",
                type="message",
                metadata={"turn_key": turn.key},
                source_description=turn.key,
            )
            written += 1
            if progress and written % 100 == 0:
                rate = written / max(time.time() - started, 1e-9)
                progress(f"  {instance.group_id}: {written:,} episodes queued, {rate:.1f}/s")
        # Any cached map is stale now.
        self._episodes.pop(instance.group_id, None)
        return written

    def settle(self, group_id: str, expected: int,
               progress: Callable[[str], None] | None = None) -> None:
        """Wait for every episode to report `processed`.

        Better than Mem0's "wait for the number to stop moving": episodes are
        one per add, so the target count is known, and each one says whether
        its extraction has finished.
        """
        uuid = self._graph_uuid(group_id)
        deadline = time.time() + SETTLE_TIMEOUT
        done = 0
        while time.time() < deadline:
            time.sleep(SETTLE_POLL)
            episodes = self._all_episodes(uuid)
            done = sum(1 for e in episodes if getattr(e, "processed", False))
            if done >= expected:
                if progress:
                    progress(f"  {group_id}: {done:,} of {expected:,} episodes processed")
                return
            if progress:
                progress(f"  {group_id}: {done:,}/{expected:,} processed, waiting")
        if progress:
            progress(
                f"  {group_id}: WARNING only {done:,} of {expected:,} episodes "
                f"processed after {SETTLE_TIMEOUT}s; scoring anyway and the "
                f"result understates zep"
            )

    def query(self, group_id: str, question: str, top_k: int) -> list[dict]:
        uuid = self._graph_uuid(group_id)
        keys = self._episode_keys(group_id)
        found = self._client.graph.search_edges(uuid, query=question, page_size=top_k)
        out = []
        for edge in _items(found):
            attributes = getattr(edge, "attributes", None) or {}
            sources = getattr(edge, "episode_uuids", None) or []
            out.append({
                "fact": attributes.get("fact") or getattr(edge, "fact", "") or "",
                # Plural, because an edge really does come from several turns.
                "provenance": {
                    "projects": [keys[u] for u in sources if u in keys],
                },
                "score": getattr(edge, "score", None),
            })
        return out
