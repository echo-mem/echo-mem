"""cognee, self hosted.

cognee splits what the others do in one step into two, and the split is the
interesting part for a write cost comparison:

    cognee.add(...)      ingests raw, no extraction model
    cognee.cognify(...)  builds the knowledge graph, and THIS is the model cost

So a cognee "write" is cheap and a cognee store is useless until cognify has
run. Reporting add alone as its write cost would flatter it enormously, which
is the opposite of the error this benchmark has to avoid elsewhere: `ingest`
here does both, and the time reported is both.

Shapes read off cognee 1.6.5 rather than guessed:

    add(data, dataset_name=..., node_set=[...], llm_config=..., embedding_config=...)
    cognify(datasets=...)
    search(query_text, query_type=SearchType.CHUNKS, datasets=...)

`SearchType.CHUNKS` is the retrieval analogue - the stored text, ranked -
rather than GRAPH_COMPLETION, which returns a model-written answer. Comparing
our fact list against somebody else's generated prose would be comparing two
different things, and the generated answer is what the separate judged pass is
for.

**Attribution goes through `DataItem.external_metadata` and `node_set`, both.**
cognee's `add` takes a bare string with no metadata slot, but a `DataItem`
carries `external_metadata` and a `node_set`. Both get the turn key, because
which of them survives chunking and graph building into a CHUNKS result is not
documented and is not something to find out from a ten hour run.

**It reads this repository's .env and that file WINS over the environment.**
On import it logs "Loaded settings from .../echo-mem/.env; its values take
precedence over preset environment variables". So exporting a variable in the
shell does not reliably configure cognee if .env names the same one, which is
the reverse of every other backend here and a genuinely surprising way for a
run to be misconfigured. llm_config and embedding_config are passed to `add`
explicitly for that reason, rather than relying on the environment at all.

**`literal_text=True` is not optional.** A `DataItem` whose `data` is a string
is passed to `save_data_item_to_storage`, which reads it as a URL, an S3 path or
a local path unless that flag is set; the flag defaults to False. Every turn
here is conversational text, so without it the ingest resolves paths instead of
storing turns. Found by reading cognee's own
`save_data_item_to_storage_detailed` rather than by a run, which is the only
reason it is not sitting in a corpus of nothing.

**Unverified against a live cognify at the time of writing.** The API shapes
come from the installed package; no graph has been built, because cognify needs
a provider key this machine does not have. Said here rather than in a commit
message: for both hosted backends every assumption that had not been run was
wrong, and there is no reason to think this one is different.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable

from echo_memory.eval.backends import BackendUnavailable, _env

#: cognee's own config names. Kept here rather than reusing the engine's,
#: because the engine's provider and the one a competitor extracts with are
#: different choices that happen to be the same key today.
KEY_VAR = "COGNEE_LLM_API_KEY"
MODEL_VAR = "COGNEE_LLM_MODEL"
PROVIDER_VAR = "COGNEE_LLM_PROVIDER"
EMBED_MODEL_VAR = "COGNEE_EMBEDDING_MODEL"


class CogneeBackend:
    name = "cognee"
    attributes_results = True

    def __init__(self):
        self._cognee = None
        self._search_type = None
        self._llm_config: dict = {}
        self._embedding_config: dict = {}

    def check(self) -> None:
        try:
            import cognee
            from cognee import SearchType
        except ImportError as exc:  # pragma: no cover - depends on the env
            raise BackendUnavailable(
                "cognee is not installed. The benchmark backends pull heavy and "
                "mutually incompatible dependency trees, so they are not in "
                "this project's requirements: install them into a separate "
                "environment. See docs/BENCHMARKS.md."
            ) from exc
        key = _env(KEY_VAR)
        model = os.environ.get(MODEL_VAR, "").strip()
        if not model:
            raise BackendUnavailable(
                f"set {MODEL_VAR}. cognify runs a model over every chunk and "
                f"the model is part of what the benchmark measures, so it is "
                f"not defaulted."
            )
        self._cognee = cognee
        self._search_type = SearchType
        # Passed per call rather than read from the environment, because
        # cognee loads this repository's .env on import and says its values
        # take precedence over preset environment variables.
        self._llm_config = {
            "llm_api_key": key,
            "llm_model": model,
            "llm_provider": os.environ.get(PROVIDER_VAR, "").strip() or "gemini",
        }
        embed = os.environ.get(EMBED_MODEL_VAR, "").strip()
        self._embedding_config = {"embedding_model": embed} if embed else {}

    def _dataset(self, group_id: str) -> str:
        """cognee names datasets, and the name has to survive being one.

        A LoCoMo group id is `locomo:conv-26`, and a colon in a dataset name is
        asking for trouble in whatever it ends up being part of, so it is
        flattened. Reversibility does not matter: nothing reads the name back.
        """
        return "echo_bench__" + group_id.replace(":", "__").replace("-", "_")

    def already_ingested(self, group_id: str, expected: int) -> bool:
        """Always False, deliberately, and it is the honest answer.

        There is no cheap "how many chunks does this dataset hold" that also
        tells you cognify finished, and a dataset that was added but not
        cognified looks populated while answering nothing. Resuming into that
        state would score a scope as a total miss. So a killed cognee run
        restarts, which is stated rather than hidden - `prune` is how you clear
        it.
        """
        return False

    def ingest(self, instance, progress: Callable[[str], None] | None = None) -> int:
        import asyncio

        from cognee.tasks.ingestion.data_item import DataItem

        dataset = self._dataset(instance.group_id)
        started = time.time()
        items = [
            DataItem(
                data=f"{turn.speaker}: {turn.fact}",
                # Without this the string is read as a URL, an S3 path or a
                # local path. save_data_item_to_storage only stores a string
                # verbatim when literal_text is set, and it defaults to False,
                # so every conversational turn here would have been resolved as
                # a path instead of ingested. Found by reading that function
                # rather than by a run, which is the only reason it is not a
                # silent corpus of nothing.
                literal_text=True,
                # Both routes to the turn key, because which one survives
                # chunking into a CHUNKS result is not documented.
                external_metadata={"turn_key": turn.key},
                node_set=[turn.key],
            )
            for turn in instance.turns
        ]

        async def _run():
            await self._cognee.add(
                items,
                dataset_name=dataset,
                llm_config=self._llm_config,
                embedding_config=self._embedding_config or None,
            )
            if progress:
                progress(
                    f"  {instance.group_id}: {len(items):,} added in "
                    f"{time.time() - started:.0f}s, now cognifying"
                )
            # The model cost, and the reason this is one step and not two: a
            # store that has been added to but not cognified answers nothing.
            await self._cognee.cognify(
                datasets=[dataset], llm_config=self._llm_config
            )

        asyncio.run(_run())
        if progress:
            progress(
                f"  {instance.group_id}: cognified in total "
                f"{time.time() - started:.0f}s"
            )
        return len(items)

    def settle(self, group_id: str, expected: int,
               progress: Callable[[str], None] | None = None) -> None:
        """Nothing to wait for: cognify is awaited in ingest, so it has
        finished by the time this is reached."""

    def query(self, group_id: str, question: str, top_k: int) -> list[dict]:
        import asyncio

        async def _search():
            return await self._cognee.search(
                query_text=question,
                query_type=self._search_type.CHUNKS,
                datasets=[self._dataset(group_id)],
                top_k=top_k,
            )

        found = asyncio.run(_search()) or []
        out = []
        for item in found:
            text, keys = _text_and_keys(item)
            out.append({"fact": text, "provenance": {"projects": keys}})
        return out


def _text_and_keys(item) -> tuple[str, list[str]]:
    """The chunk's text and whatever turn keys came back with it.

    Written defensively on purpose. A CHUNKS result is a chunk, a dict or a
    string depending on the search type and the version, and both metadata
    routes may be absent; returning no keys makes the row unattributed, which
    `attribution_rate` then reports, rather than raising halfway through a
    long run.
    """
    if isinstance(item, str):
        return item, []
    if isinstance(item, dict):
        text = item.get("text") or item.get("chunk") or item.get("content") or ""
        meta = item.get("external_metadata") or item.get("metadata") or {}
        keys = _keys_from(meta, item.get("node_set"))
        return str(text), keys
    text = getattr(item, "text", "") or getattr(item, "content", "") or ""
    meta = getattr(item, "external_metadata", None) or getattr(item, "metadata", None)
    keys = _keys_from(meta or {}, getattr(item, "node_set", None))
    return str(text), keys


def _keys_from(meta, node_set) -> list[str]:
    keys = []
    if isinstance(meta, dict):
        one = meta.get("turn_key")
        if one:
            keys.append(str(one))
    for value in list(node_set or []):
        if value and str(value) not in keys:
            keys.append(str(value))
    return keys
