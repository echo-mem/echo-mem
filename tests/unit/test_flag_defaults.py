"""What a store does with nothing exported, pinned.

Every retrieval test passes its switch as a parameter - `trust=True`,
`lexical_bm25=False` - which is right, because that is how an ablation is run.
It also means nothing in the suite reads the module level default at all. A
one-character edit flipping `ECHO_MEMORY_TRUST_RETRIEVAL` on by default would
add two queries to every read in every existing store, change which facts come
back for every customer, and pass 1491 tests.

The defaults are a product decision argued at length in the source - BM25 on
because the eval cleared it, salience and rerank and route expansion off
because it did not, trust and lessons off because every existing store has no
verification rows so turning them on buys two queries and no change, the graph
write gate off because it costs 2.6x write throughput to guard a fault the
normal one-writer deployment cannot hit. The README repeats three of them to
customers. None of that is worth anything if the value can move unnoticed.

Read in a subprocess with the variables deleted, for two reasons. These are
module level constants evaluated at import, so `importlib.reload` would be
needed to see a change - and reload rebinds this process's globals while every
`from ... import` elsewhere keeps the old object, which is a way to poison
later tests in the same session. And a clean environment is the point: a value
read from whatever the test runner happens to export is an assertion about the
runner.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

#: Constant -> (module, the value with nothing exported).
#:
#: Written out rather than derived from `os.environ.get`'s second argument,
#: which would compare the source with itself and pass through any change to
#: the truthiness test around it. `LEXICAL_BM25` is the one True here and the
#: list is the better for having it: a file of `is False` assertions reads as
#: a policy of timidity rather than as six measured decisions.
DEFAULTS = {
    "LEXICAL_BM25": ("echo_memory.retrieval.query_memory", True),
    "USE_SALIENCE": ("echo_memory.retrieval.query_memory", False),
    "ROUTE_EXPANSION": ("echo_memory.retrieval.query_memory", False),
    "RERANK": ("echo_memory.retrieval.query_memory", False),
    "TRUST_RETRIEVAL": ("echo_memory.retrieval.query_memory", False),
    "LESSON_CHANNEL": ("echo_memory.retrieval.query_memory", False),
}

#: Every variable above, plus the two read per call rather than at import.
#: Deleted from the child's environment so the child sees an unconfigured
#: install.
VARS = (
    "ECHO_MEMORY_LEXICAL_BM25",
    "ECHO_MEMORY_SALIENCE",
    "ECHO_MEMORY_ROUTE_EXPANSION",
    "ECHO_MEMORY_RERANK",
    "ECHO_MEMORY_TRUST_RETRIEVAL",
    "ECHO_MEMORY_LESSONS",
    "ECHO_MEMORY_MAX_GRAPH_WRITERS",
    "ECHO_MEMORY_OTEL",
)


def _in_a_clean_process(probe: str):
    env = {k: v for k, v in os.environ.items() if k not in VARS}
    out = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, check=True, env=env,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("name", sorted(DEFAULTS))
def test_the_retrieval_switch_defaults_to_what_the_eval_decided(name):
    module, expected = DEFAULTS[name]
    probe = (
        f"import json, importlib; "
        f"m = importlib.import_module({module!r}); "
        f"print(json.dumps(bool(getattr(m, {name!r}))))"
    )
    assert _in_a_clean_process(probe) is expected


def test_the_graph_write_gate_is_off_with_nothing_exported():
    """Read per call rather than at import, so this is a function result and
    not a constant. UNLIMITED is 0 and means the gate does nothing; see the
    note on it for why that default was reversed, and what it costs when on.
    """
    probe = (
        "import json; "
        "from echo_memory.infra import graphlock; "
        "print(json.dumps(graphlock.max_graph_writers() == graphlock.UNLIMITED))"
    )
    assert _in_a_clean_process(probe) is True


def test_the_otel_bridge_is_on_with_nothing_exported():
    """The one default that is ON and reaches outside the process - or would,
    if an application had configured an SDK. Pinned in the same place as the
    others so that "on by default" stays a decision somebody made rather than
    a line nobody reread.
    """
    probe = (
        "import json; "
        "from echo_memory.infra import metrics; "
        "print(json.dumps(metrics._otel_enabled()))"
    )
    assert _in_a_clean_process(probe) is True


def test_the_readme_names_the_three_it_tells_customers_about():
    """The README's capability section says two retrieval switches and the
    write gate default off. If one of them starts defaulting on, that sentence
    becomes false to exactly the person who read it before deciding to
    self-host.
    """
    from pathlib import Path

    text = Path(__file__).resolve().parents[2].joinpath("README.md").read_text(
        encoding="utf-8"
    )
    for var in ("ECHO_MEMORY_TRUST_RETRIEVAL", "ECHO_MEMORY_LESSONS",
                "ECHO_MEMORY_MAX_GRAPH_WRITERS"):
        assert var in text, f"the README stopped naming {var}"
