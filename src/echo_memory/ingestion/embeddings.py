"""Embedding: a local model, no external API call (see the design doc's MCP
tool contract "architecture pivot" note). Swappable behind the Embedder
protocol if local quality proves insufficient later."""

import threading
from typing import Protocol


class Embedder(Protocol):
    dimension: int

    def embed(self, text: str) -> list[float]: ...

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        """Embed several texts at once, in order. **Optional.**

        Optional because this protocol is offered as swappable and gained this
        method after implementations already existed. Callers go through
        `_batch`, which falls back to a loop over `embed`, so an embedder
        written against the older shape still works - slower, same answers.


        A transformer forward pass has fixed overhead - tokenisation, tensor
        setup, the call into torch - that one text pays in full and twelve
        share. Measured on this machine: twelve texts one call each is 39.2 ms,
        the same twelve in one batch is 6.1 ms.

        `reindex` re-embeds every active fact in a scope: 290 of them cost
        2059.8 ms one at a time and 437.8 ms in batches, 4.7x, and that gap
        widens with the store.

        write_episode was measured as not benefiting, and that finding was
        wrong - not mismeasured, but taken against code where a single
        unindexed edge lookup was 93% of a write. Batching saved 83 ms of 4210
        and vanished into the noise. With that lookup fixed the same 21 texts
        an episode embeds cost 90.5 ms one at a time against 7.9 ms batched,
        out of a 330 ms write: a quarter of it.

        The lesson is about profiling, not embedders. A component's share of a
        total is only meaningful once the total is not dominated by a defect,
        and "we measured it and it did not help" is not durable while anything
        upstream is still broken.
        """
        ...


class LocalEmbedder:
    """all-MiniLM-L6-v2 (384-dim). A placeholder default pending real quality
    tuning (see MATHS.local.md's open questions), not a considered final
    choice. Lazy-loads the model so importing this module never triggers a
    download; only calling embed() does."""

    MODEL_NAME = "all-MiniLM-L6-v2"
    dimension = 384

    def __init__(self):
        self._model = None
        # Loading takes about seven seconds and can now be asked for from two
        # places at once - a warm-up thread and a real query. Without the lock
        # both build a SentenceTransformer, which is the cost this exists to
        # pay once.
        self._lock = threading.Lock()

    def _load(self):
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is None:
                import logging

                from sentence_transformers import SentenceTransformer

                # sentence-transformers logs device/model-load info at INFO by
                # default; an MCP stdio server's stderr is meant for structured
                # JSON logs (see infra/logging.py), not this library's own
                logging.getLogger("sentence_transformers").setLevel(logging.WARNING)
                self._model = SentenceTransformer(self.MODEL_NAME)
        return self._model

    def warm(self) -> None:
        """Load the model now rather than inside someone's first question.

        Measured on this machine: constructing LocalEmbedder costs nothing and
        the first embed() costs 7.1 seconds, the second 6 milliseconds. Claude
        Desktop's own server log shows exactly that shape in production - two
        query_memory calls at 6099ms and 5994ms against others at 5ms.

        Lazy loading is right for the import, which is why it is there: a CLI
        that only prints a queue must not download a model. It is wrong for a
        long-lived server, which knows it will need the model and has an idle
        moment at startup to pay for it.
        """
        self.embed("warm")

    def embed(self, text: str) -> list[float]:
        model = self._load()
        return model.encode(text, normalize_embeddings=True, show_progress_bar=False).tolist()

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        """One forward pass for the lot. Order matches the input."""
        if not texts:
            return []
        model = self._load()
        return model.encode(
            texts, normalize_embeddings=True, show_progress_bar=False
        ).tolist()


def _batch(inner: Embedder, texts: list[str]) -> list[list[float]]:
    """`embed_many` when the embedder has one, a loop over `embed` when it does
    not.

    `Embedder` is a Protocol and its own docstring offers it as swappable, so
    implementations exist that this repository cannot see - a test double in a
    downstream service, someone's hosted model. Adding `embed_many` to the
    protocol made every one of them break at runtime rather than at import,
    with an AttributeError from inside a write that had already started. That
    happened the same day, to echo-mem-cloud's StubEmbedder.

    Batching is an optimisation. An optimisation is not allowed to be a
    breaking change to a published interface, so it degrades instead: an
    embedder with only `embed` gets the old cost and the same answers.
    """
    if hasattr(inner, "embed_many"):
        return inner.embed_many(texts)
    return [inner.embed(t) for t in texts]


class Prefetched:
    """An embedder that has already computed the texts it was told to expect.

    Wraps another embedder, embeds a predicted set in one batch, and serves
    those from memory. Anything unpredicted falls through to the wrapped
    embedder and is remembered, so a wrong prediction costs nothing but the
    saving - it can never change an answer, only how long it took.

    That fallback is the design, not a safety net bolted on. Threading a batch
    through every call site would mean write_episode, resolve_entities and
    _create_edge all agreeing in advance about exactly which strings get
    embedded, and the first branch anyone adds breaks the agreement silently.
    Here a missed text is served correctly and slowly, which is the failure
    mode to want.
    """

    def __init__(self, inner: Embedder, texts: list[str]):
        self._inner = inner
        self.dimension = inner.dimension
        wanted = list(dict.fromkeys(t for t in texts if t))
        self._cache = dict(zip(wanted, _batch(inner, wanted), strict=True))

    def embed(self, text: str) -> list[float]:
        hit = self._cache.get(text)
        if hit is None:
            hit = self._inner.embed(text)
            self._cache[text] = hit
        return hit

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        self.extend(texts)
        return [self._cache[t] for t in texts]

    def extend(self, texts: list[str]) -> None:
        """Add another batch, once its texts are known.

        An episode cannot predict everything at once. Resolution needs the
        entity names embedded before it can say which mentions are ambiguous,
        and which facts get written depends on that answer - a fact touching an
        ambiguous mention is deferred and never embedded at all. Prefetching
        every fact up front embedded work the write then threw away.
        """
        missing = [t for t in dict.fromkeys(texts) if t and t not in self._cache]
        if missing:
            self._cache.update(zip(missing, _batch(self._inner, missing), strict=True))
