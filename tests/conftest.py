"""Suite-wide setup that has to happen before any test module is imported.

Only one thing lives here, and it is about a failure mode rather than a
fixture: pinning the embedding model to the local cache.

`LocalEmbedder` constructs a `SentenceTransformer`, and sentence-transformers
asks the HuggingFace hub about the model before it will use a copy already on
disk. When the hub is reachable that costs a round trip. When it is reachable
but not answering - a captive portal, a proxy that accepts the connection and
then says nothing, a flaky link - the read blocks, and nothing in the suite
bounds it. Observed 2026-11-25: a run sat at 29% for ten and a half hours with
no output, no CPU and an open TLS socket, and looked from the outside exactly
like a slow test.

`docs/DEVELOPMENT.md` has documented the symptom and the remedy for a while:
run with `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1`. That is advice, and the
command a contributor actually types is `pytest`. Advice that only works when
remembered is the same shape of defect as a guard that asserts a correction
exists without looking at the claim, so this sets the variables itself.

It sets them ONLY when the model is already cached, which keeps the first run
on a new machine working: there, the download is the point, and forcing
offline would turn a one-time fetch into a hard error. After that first run the
suite never touches the network for this again.

Set ECHO_MEMORY_TEST_ALLOW_HUB=1 to opt out, for the case where the cached
model is suspected stale and a real hub check is what you want.
"""

from __future__ import annotations

import os
from pathlib import Path

# The only model the suite needs; KNOWN_384 in ingestion/embeddings.py is the
# list this has to stay a member of.
_MODEL_DIR = "models--sentence-transformers--all-MiniLM-L6-v2"


def _hub_cache() -> Path:
    """Where huggingface_hub keeps downloaded models, honouring its own env."""
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"])
    if os.environ.get("HF_HOME"):
        return Path(os.environ["HF_HOME"]) / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def _model_is_cached() -> bool:
    """A snapshot directory with something in it.

    Checked rather than assumed, because an interrupted download leaves the
    directory present and empty, and pinning offline against that turns a
    retryable fetch into a confusing failure.
    """
    snapshots = _hub_cache() / _MODEL_DIR / "snapshots"
    if not snapshots.is_dir():
        return False
    return any(any(d.iterdir()) for d in snapshots.iterdir() if d.is_dir())


_allow_hub = os.environ.get("ECHO_MEMORY_TEST_ALLOW_HUB", "") in ("1", "true", "yes")

if not _allow_hub and _model_is_cached():
    # setdefault, not assignment: an explicit value on the command line is
    # the operator saying something, and this should not overrule it.
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
