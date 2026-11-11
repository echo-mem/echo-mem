"""The registry, and the two properties that make it safe to turn on.

A metric is a log field with a longer life: it reaches a collector and is
retained there. So the tests that matter are the ones about what it refuses to
record, and the one about what happens when a label's value varies per call -
which is how a counter becomes a memory leak in a long lived MCP server.
"""

from __future__ import annotations

import io
import json
import logging
import threading

import pytest

from echo_memory.infra import metrics
from echo_memory.infra.logging import JsonFormatter


@pytest.fixture(autouse=True)
def clean_registry():
    metrics.reset()
    yield
    metrics.reset()


def _capture() -> io.StringIO:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = metrics._logger
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    return stream


def _counter(snapshot, name):
    return [c for c in snapshot["counters"] if c["name"] == name]


def _timer(snapshot, name):
    return [t for t in snapshot["timers"] if t["name"] == name]


# --- counters -----------------------------------------------------------


def test_a_counter_accumulates():
    metrics.counter("writes")
    metrics.counter("writes")
    metrics.counter("writes", 3)

    assert _counter(metrics.snapshot(), "writes")[0]["value"] == 5


def test_labels_separate_series():
    metrics.counter("writes", outcome="stored")
    metrics.counter("writes", outcome="deferred")
    metrics.counter("writes", outcome="stored")

    by_outcome = {
        c["labels"]["outcome"]: c["value"] for c in _counter(metrics.snapshot(), "writes")
    }
    assert by_outcome == {"stored": 2, "deferred": 1}


def test_label_order_does_not_create_a_second_series():
    """Kwargs arrive in call order, so the same two labels written the other
    way round would otherwise split one series in two and halve both."""
    metrics.counter("reads", scope="solo", outcome="hit")
    metrics.counter("reads", outcome="hit", scope="solo")

    assert len(_counter(metrics.snapshot(), "reads")) == 1
    assert _counter(metrics.snapshot(), "reads")[0]["value"] == 2


def test_a_missing_label_value_is_recorded_as_null_not_dropped():
    """None is a real answer - "no agent_id was passed" - and dropping the
    label would merge those calls into the series for the calls that did pass
    one."""
    metrics.counter("writes", agent_id=None)
    assert _counter(metrics.snapshot(), "writes")[0]["labels"]["agent_id"] == "null"


def test_the_snapshot_is_json_serialisable():
    """The one consumer is a /metrics style handler or a log line, and both
    call json.dumps. A tuple keyed dict would raise there, not here."""
    metrics.counter("writes", scope="solo")
    with metrics.timer("query", scope="solo"):
        pass
    json.dumps(metrics.snapshot())


# --- what it refuses to record ------------------------------------------


@pytest.mark.parametrize(
    "key",
    ["fact", "fact_text", "text", "query", "query_text", "entity", "name", "target", "message"],
)
def test_a_label_that_would_carry_memory_content_is_rejected(key):
    """Not dropped. A missing label reads as a label nobody passed, and the
    person reading the snapshot has no way to tell those apart."""
    metrics.counter("writes", **{key: "the deploy branch is main, never master"})

    labels = _counter(metrics.snapshot(), "writes")[0]["labels"]
    assert labels[key] == metrics.REJECTED


@pytest.mark.parametrize("key", ["name", "n"])
def test_a_label_named_like_a_parameter_does_not_raise(key):
    """`name` is on the denylist exactly because somebody will reach for it,
    and `n` is the counter's increment. Without positional-only parameters both
    are a TypeError raised from inside the telemetry of whatever was being
    measured."""
    metrics.counter("writes", **{key: "x"})
    with metrics.timer("write_ms", **{key: "x"}):
        pass

    assert key in _counter(metrics.snapshot(), "writes")[0]["labels"]
    assert key in _timer(metrics.snapshot(), "write_ms")[0]["labels"]


def test_a_callers_outcome_label_does_not_collide_with_the_timers():
    """The timer's `outcome` is observed; a passed one is a guess made before
    the call. Overwritten rather than raising a duplicate kwarg."""
    with pytest.raises(ValueError), metrics.timer("write_ms", outcome="stored"):
        raise ValueError("no agent_id")

    assert _timer(metrics.snapshot(), "write_ms")[0]["labels"]["outcome"] == "error"


def test_a_rejected_key_is_matched_regardless_of_case():
    metrics.counter("writes", FACT="secret text", Query="secret query")
    labels = _counter(metrics.snapshot(), "writes")[0]["labels"]
    assert labels["FACT"] == metrics.REJECTED
    assert labels["Query"] == metrics.REJECTED


def test_a_long_value_under_an_unlisted_key_is_truncated():
    """The key denylist cannot be complete, so length is the backstop: a fact
    smuggled through a key nobody thought of loses all but its first 64
    characters."""
    fact = "the deploy branch is main and the release tag is cut from it, never from master"
    metrics.counter("writes", note=fact)

    value = _counter(metrics.snapshot(), "writes")[0]["labels"]["note"]
    assert len(value) == metrics.MAX_LABEL_LENGTH
    assert fact.startswith(value)


def test_no_recorded_value_contains_the_text_that_was_passed():
    """The property, stated once over the whole snapshot rather than per
    field, so a new field added to a series cannot leak past the tests above."""
    secret = "dugout pays 149 dollars a month for 500k units"
    metrics.counter("writes", fact=secret, group_id="user:x:shared")
    metrics.observe("write_ms", 12.0, query=secret)

    assert secret not in json.dumps(metrics.snapshot())


# --- cardinality --------------------------------------------------------


def test_a_per_call_label_cannot_grow_without_bound():
    """A session_id or an edge_id as a label is the realistic mistake, and in
    a long lived MCP server it is an OOM rather than a wrong number."""
    for i in range(metrics.MAX_SERIES_PER_NAME + 250):
        metrics.counter("writes", session=f"sess-{i}")

    series = _counter(metrics.snapshot(), "writes")
    assert len(series) == metrics.MAX_SERIES_PER_NAME + 1


def test_the_total_survives_the_cap():
    """Overflowing observations are counted under one label rather than
    discarded. A capped metric that undercounts is worse than one that is
    coarse, because the total is the number an operator reasons with."""
    n = metrics.MAX_SERIES_PER_NAME + 250
    for i in range(n):
        metrics.counter("writes", session=f"sess-{i}")

    assert sum(c["value"] for c in _counter(metrics.snapshot(), "writes")) == n
    overflow = [
        c for c in _counter(metrics.snapshot(), "writes")
        if c["labels"].get(metrics.OVERFLOW) == "true"
    ]
    assert overflow[0]["value"] == 250


def test_the_cap_is_announced_once(caplog):
    with caplog.at_level(logging.WARNING, logger="echo_memory.metrics"):
        for i in range(metrics.MAX_SERIES_PER_NAME + 50):
            metrics.counter("writes", session=f"sess-{i}")

    capped = [r for r in caplog.records if r.message == "metric_label_cardinality_capped"]
    assert len(capped) == 1, f"said it {len(capped)} times"


# --- timers -------------------------------------------------------------


def test_a_timer_records_count_sum_min_and_max():
    for value in (10.0, 30.0, 20.0):
        metrics.observe("write_ms", value)

    slot = _timer(metrics.snapshot(), "write_ms")[0]
    assert slot["count"] == 3
    assert slot["sum_ms"] == 60.0
    assert slot["min_ms"] == 10.0
    assert slot["max_ms"] == 30.0
    assert slot["mean_ms"] == 20.0


def test_the_mean_is_derived_not_stored():
    """So it cannot disagree with the sum and count beside it, which is the
    way a hand maintained running mean goes wrong."""
    metrics.observe("write_ms", 1.0)
    metrics.observe("write_ms", 2.0)
    slot = _timer(metrics.snapshot(), "write_ms")[0]
    assert slot["mean_ms"] == pytest.approx(slot["sum_ms"] / slot["count"], abs=1e-3)


def test_the_timer_context_manager_measures_the_block():
    with metrics.timer("write_ms"):
        sum(range(200_000))

    slot = _timer(metrics.snapshot(), "write_ms")[0]
    assert slot["count"] == 1
    assert slot["min_ms"] > 0


def test_a_raising_block_is_still_timed_and_labelled_error():
    """A timing that covers only successes hides the slow path somebody is
    looking for: the failing write is usually the slow one."""
    with pytest.raises(ValueError), metrics.timer("write_ms"):
        raise ValueError("no agent_id")

    slot = _timer(metrics.snapshot(), "write_ms")[0]
    assert slot["count"] == 1
    assert slot["labels"]["outcome"] == "error"


def test_a_successful_block_is_labelled_ok():
    with metrics.timer("write_ms"):
        pass
    assert _timer(metrics.snapshot(), "write_ms")[0]["labels"]["outcome"] == "ok"


# --- emission -----------------------------------------------------------


def test_emit_writes_one_json_line_through_the_existing_logger():
    """Through infra/logging.py's formatter, not a second logging system: one
    JSON object per line on stderr is what anything reading this process's
    output already expects."""
    stream = _capture()
    metrics.counter("writes", outcome="stored")
    metrics.emit()

    lines = [ln for ln in stream.getvalue().splitlines() if ln.strip()]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["message"] == "metrics"
    assert payload["counters"][0]["name"] == "writes"


def test_recording_a_metric_writes_nothing_by_itself():
    """The hot path touches a dict, not stderr. A line per increment makes a
    six fact write cost dozens of writes and ties write latency to whatever is
    reading the pipe."""
    stream = _capture()
    for _ in range(100):
        metrics.counter("writes")
        metrics.observe("write_ms", 1.0)

    assert stream.getvalue() == ""


def test_reset_clears_everything():
    metrics.counter("writes")
    metrics.observe("write_ms", 1.0)
    metrics.reset()
    assert metrics.snapshot() == {"counters": [], "timers": []}


# --- concurrency --------------------------------------------------------


def test_counters_do_not_lose_increments_across_threads():
    """Eight threads is the shape the concurrency suite runs, and a lost
    update under a lock-free dict would show up as a count that is nearly
    right, which is the hardest kind of wrong number to notice."""
    threads = [
        threading.Thread(target=lambda: [metrics.counter("writes") for _ in range(2000)])
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert _counter(metrics.snapshot(), "writes")[0]["value"] == 16_000


# --- the optional bridge ------------------------------------------------


def test_opentelemetry_is_present_without_being_a_declared_dependency():
    """Pinned because it is the fact the bridge's design rests on, and because
    it is not visible from pyproject.toml: `opentelemetry-api` is not in this
    package's dependencies, it arrives under `mcp>=2.0`. If mcp ever drops it,
    the bridge starts latching off in every install and this test is what says
    so, rather than a silent loss of telemetry nobody was watching."""
    otel = pytest.importorskip("opentelemetry.metrics")
    assert otel.get_meter("echo_memory") is not None


def test_the_bridge_is_on_by_default_and_reaches_an_instrument():
    """On by default is the one choice in that module worth defending, so it
    is pinned. Nothing leaves the process: with no SDK configured, get_meter
    hands back a proxy whose record is measured at 0.044 us."""
    pytest.importorskip("opentelemetry.metrics")
    monkey = pytest.MonkeyPatch()
    monkey.delenv(metrics.OTEL_ENV, raising=False)
    try:
        metrics.reset()
        metrics.counter("writes", scope="solo")
        metrics.observe("write_ms", 1.0, scope="solo")

        assert isinstance(metrics._otel, dict)
        assert "writes" in metrics._otel["counters"]
        assert "write_ms" in metrics._otel["histograms"]
    finally:
        monkey.undo()


@pytest.mark.parametrize("raw", ["0", "false", "off", "NO"])
def test_the_bridge_can_be_turned_off(monkeypatch, raw):
    monkeypatch.setenv(metrics.OTEL_ENV, raw)
    metrics.reset()
    metrics.counter("writes")

    assert metrics._otel is False


def test_the_registry_still_records_with_the_bridge_off(monkeypatch):
    """The dict is the implementation and OTel is a second emission, so
    turning the bridge off must not cost the snapshot anything."""
    monkeypatch.setenv(metrics.OTEL_ENV, "0")
    metrics.reset()
    metrics.counter("writes", 4)

    assert _counter(metrics.snapshot(), "writes")[0]["value"] == 4


def test_an_instrument_that_raises_disables_the_bridge_without_failing_the_call():
    """Telemetry must not be able to fail the operation it measures, and a
    bridge that raises once will raise again, so it latches off rather than
    costing a try/except per observation for the rest of the process."""
    metrics.reset()

    class Exploding:
        def add(self, *a, **k):
            raise RuntimeError("collector gone")

    metrics._otel = {"meter": None, "counters": {"writes": Exploding()}, "histograms": {}}
    metrics.counter("writes", 2)

    assert metrics._otel is False
    assert _counter(metrics.snapshot(), "writes")[0]["value"] == 2

    metrics.counter("writes")
    assert _counter(metrics.snapshot(), "writes")[0]["value"] == 3


def test_the_bridge_never_sees_a_rejected_label_value():
    """The bridge is handed the already cleaned series key, not the caller's
    kwargs. Passing the raw labels would send fact text to a collector while
    the local snapshot showed it rejected, which is the worst of both."""
    pytest.importorskip("opentelemetry.metrics")
    metrics.reset()
    recorded = []

    class Recording:
        def record(self, value, attributes=None):
            recorded.append(attributes)

    metrics._otel = {"meter": None, "counters": {}, "histograms": {"write_ms": Recording()}}
    metrics.observe("write_ms", 1.0, fact="dugout pays 149 a month")

    assert recorded == [{"fact": metrics.REJECTED}]
