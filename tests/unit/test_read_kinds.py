"""Every read surface registers the kind it records under.

`read_event` answers "which facts has this agent been given", which is the
first question a data protection or governance review asks. It can only answer
it for surfaces that record, and for months it recorded for one of four:
query_memory on the self hosted path. The hosted service imported the retrieval
function directly and wrote nothing, and trace_cause and get_audit_log wrote
nothing anywhere.

So the point of this file is the inventory. A read surface that hands facts to
an agent and has no kind here is a hole in the delivery record, and a hole is
worth failing a test over because nothing else notices one.
"""

from __future__ import annotations

import re
from pathlib import Path

from echo_memory.trial import reads

ROOT = Path(__file__).resolve().parents[2]


def test_every_kind_is_in_the_inventory():
    """KINDS exists so a reader of the table knows the set without grepping,
    and so a new surface has an obvious place to register."""
    assert set(reads.KINDS) == {reads.HOOK, reads.QUERY, reads.TRACE, reads.AUDIT}
    assert len(reads.KINDS) == len(set(reads.KINDS)), "a kind is listed twice"


def test_the_kinds_are_the_tool_names_a_reader_would_expect():
    """The value lands in a column somebody reads during an incident. "q" or
    "0" would need a lookup table nobody has."""
    assert reads.QUERY == "query_memory"
    assert reads.TRACE == "trace_cause"
    assert reads.AUDIT == "get_audit_log"


def test_record_accepts_the_returned_fact_ids():
    """The column migration 0021 added, and the reason the table is evidence
    rather than a counter: a count says somebody read, the ids say what they
    were given."""
    source = (ROOT / "src" / "echo_memory" / "trial" / "reads.py").read_text()

    assert "returned_fact_ids" in source
    assert re.search(r"def record\([^)]*fact_ids", source, re.DOTALL), (
        "record() no longer takes the fact ids"
    )


def test_recording_never_raises_and_says_why():
    """A prompt must reach the agent whether or not logging works. This is the
    one place where swallowing an exception is right, so it should stay
    deliberate rather than become a habit somebody tidies away."""
    source = (ROOT / "src" / "echo_memory" / "trial" / "reads.py").read_text()

    assert "Never raises" in source
    assert "read_event_not_recorded" in source, "a swallowed failure is silent"


def test_query_memory_says_recording_is_the_callers_job():
    """The seam that was crossed. query_memory deliberately does not record,
    because the eval harness and the benchmark backends call it thousands of
    times and must not fill the delivery log. That makes recording a contract
    the caller has to keep, and a contract nobody wrote down is one the hosted
    service broke by importing the function directly."""
    source = (ROOT / "src" / "echo_memory" / "retrieval" / "query_memory.py").read_text()
    flat = " ".join(source.split())

    assert "Reading does not record a read" in flat
    assert "echo_cloud/tools.py" in flat, (
        "the contract does not name the callers that have to keep it"
    )


def test_the_self_hosted_server_records_its_reads():
    """The surface that was already right, pinned so it stays that way."""
    source = (ROOT / "src" / "echo_memory" / "server.py").read_text()

    assert "_reads.record(" in source
    assert "_reads.QUERY" in source
