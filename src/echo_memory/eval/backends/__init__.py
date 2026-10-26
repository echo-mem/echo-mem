"""Memory systems the external benchmarks can be run against.

`eval/external.py` put Echo Memory on LoCoMo and LongMemEval so a claim about
it could be checked by somebody who has never seen this store. It did not
answer the question anybody actually asks, which is how that compares to Mem0,
Zep, Letta or cognee (issue #147). Every number in docs/BENCHMARKS.md is about
this system alone, with no baseline beside it.

A backend is the whole surface the harness needs: put the turns of one instance
in, ask a question, get ranked results back. Nothing else about a system is
assumed.

## The attribution problem, which is the whole difficulty

`score_question` matches what came back against the gold turns by reading a
turn key out of each result's provenance. That works for Echo Memory because
one turn becomes one fact, so the key survives the round trip exactly.

It does not generalise, and pretending it does is the way this comparison gets
quietly dishonest. Mem0 and Zep call a model on write and store something
*derived* from the turns: one memory can summarise several turns, and several
can be dropped. Asking "which turn did this come from" can then have no single
answer. Where a system cannot answer it, the turn-keyed metrics - `rr`,
`recall@k`, `hit@k`, `session@k` - are not computable.

The tempting thing is to score a missing attribution as a miss. That reports
0.0 for a competitor that retrieved the right content and merely could not name
its source turn, which would understate it, in our favour, in the one direction
a reader has most reason to suspect. So a backend DECLARES whether it can
attribute a result to a source turn, and the harness reports those metrics as
null rather than zero when it cannot.

What stays comparable across every backend either way:

  - `answer_words@k`, computed from returned TEXT, needing no attribution
  - the judged QA answer, which is what Mem0 and Zep publish
  - ingest cost in wall clock, and in model calls where a backend reports them

Each adapter is imported lazily inside its own factory, so the harness runs
with none of their SDKs installed and a missing one names itself.

## Credentials

Read from the environment, never passed as an argument and never written to a
results file. A benchmark result is published; a key must not be.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Protocol, runtime_checkable


class BackendUnavailable(RuntimeError):
    """A backend that cannot run, said as early and as specifically as
    possible.

    Raised for a missing SDK or a missing credential, by name. A run against
    four systems that dies on hour three because the fourth was never reachable
    has wasted the first three, so every backend is checked before any of them
    ingests anything.
    """


@runtime_checkable
class Backend(Protocol):
    """One memory system, as the harness sees it."""

    name: str

    #: Can a returned result be traced to the turn it came from? False for any
    #: system that stores something derived from the turns rather than the
    #: turns, which makes the turn-keyed metrics not computable rather than
    #: zero. See the module docstring.
    attributes_results: bool

    def check(self) -> None:
        """Raise BackendUnavailable now rather than mid-run."""

    def already_ingested(self, group_id: str, expected: int) -> bool:
        """True if this scope already holds the turns, so a killed run resumes."""

    def ingest(self, instance, progress: Callable[[str], None] | None = None) -> int:
        """Write every turn of one instance. Returns turns written."""

    def query(self, group_id: str, question: str, top_k: int) -> list[dict]:
        """Ranked results, most relevant first.

        Each dict carries `fact`, the text, and for a backend with
        `attributes_results` a `provenance` mapping whose `project` is the turn
        key given at ingest. `score_question` reads exactly those two things.
        """


def _env(*names: str) -> str:
    """The first of these environment variables that is set, or a
    BackendUnavailable naming all of them.

    Several of these SDKs read a key from the environment themselves and fail
    deep inside a client constructor with a message that does not say which
    variable was wanted. Checking here means the error names it.
    """
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    raise BackendUnavailable(
        f"set one of {', '.join(names)} in the environment. Export it in your "
        f"shell; it is deliberately not a command line argument, because a "
        f"benchmark command ends up in a shell history and a results file."
    )
