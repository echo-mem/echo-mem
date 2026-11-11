"""A deterministic embedder that never raises, for tests about structure.

`fake_embedder.VectorEmbedder` refuses text it has not been given a vector
for, and that is the right default: a test about a similarity threshold must
not quietly fall through to accidental behaviour. These tests are not about
similarity. The event path composes its own fact text from the payload, so
registering every string up front would mean restating the formatting of
`intelligence/outcomes.py` inside the test - and then a change to the wording
would fail as a KeyError from the embedder rather than as the assertion that
actually cares.

Lifted from the one in test_eval_sweep.py, including the mistake that one
records: every dimension has to come from the text. A first version there
repeated a 32-byte digest and XORed in the position, which made the position
dominate, so distinct names came out near-parallel and resolution merged
entities that should have stayed apart.
"""

from __future__ import annotations

import hashlib
import math


class HashEmbedder:
    """Deterministic, says nothing about ranking quality, and says so."""

    dimension = 384

    def embed(self, text: str) -> list[float]:
        raw: list[float] = []
        block = 0
        while len(raw) < self.dimension:
            digest = hashlib.sha256(f"{block}:{text}".encode()).digest()
            raw.extend(b - 127.5 for b in digest)
            block += 1
        raw = raw[: self.dimension]
        norm = math.sqrt(sum(x * x for x in raw)) or 1.0
        return [x / norm for x in raw]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]
