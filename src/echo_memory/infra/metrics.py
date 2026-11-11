"""Counters and timers, aggregated in process and emitted as one JSON line.

This is deliberately small, and the reason is not the one you would guess.
`opentelemetry-api` 1.44.0 IS already installed here, not because anyone chose
it but because `mcp>=2.0` depends on it, so it is present in every install of
this package. "Importable" is therefore no evidence that anybody wants it on,
and it is not a reason to route the aggregate an operator reads through an SDK
that may have no provider behind it.

So the registry is a dict, and the OTel bridge below is a second, parallel
emission rather than the implementation. An operator who runs a collector gets
the observations there too; one who does not gets a snapshot that works with no
configuration at all.

## What it does not record

Never fact text, entity names, or query text. `infra/logging.py` exists in that
shape for a stated reason - logs must be safe to ship off-box without also
shipping user memory - and a metric label is a log field with a longer life,
because it reaches a collector and is retained there. Label keys that name
content are rejected at the call, the value replaced with REJECTED so the
snapshot SHOWS the rejection rather than quietly omitting it. Rejected rather
than raised: telemetry must not be able to fail a write.

## Why aggregate instead of a line per observation

A log line per increment makes a six fact write cost dozens of stderr writes
and ties write latency to whatever is reading the pipe. So the hot path touches
a dict under a lock, and `emit()` writes one line when an operator or a
scheduled call asks for one.

## Why no percentiles

count, sum, min and max are exact and need no samples. A p95 would need a
reservoir, and a p95 computed from a bounded reservoir over a process that has
been up for a week is not a p95 of anything anyone wants - it is a p95 of
whatever survived the sampling. If percentiles are needed, they are needed in a
collector that has the raw observations, which is what the OTel bridge is for.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager

from echo_memory.infra.logging import get_logger

_logger = get_logger("metrics")

#: Substituted for a label value whose key could carry user memory. Visible in
#: the snapshot on purpose: a dropped label reads as a label nobody passed.
REJECTED = "<rejected>"

#: Label keys that would carry fact, entity or query text. Checked by key
#: rather than by sniffing the value, because a value check cannot tell a
#: group_id from a one word fact and would reject either both or neither.
_CONTENT_KEYS = frozenset(
    {
        "fact", "facts", "fact_text", "text", "content", "query", "query_text",
        "q", "entity", "entities", "name", "names", "mention", "source",
        "target", "summary", "answer", "prompt", "message", "detail",
    }
)

#: The longest a label value may be. 64 is generous for every legitimate label
#: this package has (a group_id, a scope, an outcome word, a version) and short
#: enough that a fact smuggled through an unlisted key is truncated rather than
#: retained in full.
MAX_LABEL_LENGTH = 64

#: How many distinct label combinations one metric name may hold. A label whose
#: value varies per call - a session_id, an edge_id - turns a counter into an
#: unbounded dict, and the process that notices is the one that gets OOM killed.
#: Past the cap the series is counted under the overflow label instead, so the
#: total stays correct and the snapshot says what happened.
MAX_SERIES_PER_NAME = 1000

#: Where observations past MAX_SERIES_PER_NAME go.
OVERFLOW = "<overflow>"

_lock = threading.Lock()
_counters: dict[str, dict[tuple, int]] = {}
_timers: dict[str, dict[tuple, dict]] = {}
_warned_overflow: set[str] = set()


def _labels(name: str, labels: dict) -> tuple:
    """A hashable, order independent, content free key for one series."""
    cleaned = []
    for key in sorted(labels):
        value = labels[key]
        if key.lower() in _CONTENT_KEYS:
            cleaned.append((key, REJECTED))
            continue
        text = "null" if value is None else str(value)
        cleaned.append((key, text[:MAX_LABEL_LENGTH]))
    return tuple(cleaned)


def _resolve(by_labels: dict, name: str, key: tuple) -> tuple:
    """The key this observation lands on: its own, or the overflow key once
    this metric has as many distinct label combinations as it may hold."""
    if key in by_labels or len(by_labels) < MAX_SERIES_PER_NAME:
        return key
    if name not in _warned_overflow:
        _warned_overflow.add(name)
        _logger.warning(
            "metric_label_cardinality_capped",
            extra={"metric": name, "cap": MAX_SERIES_PER_NAME},
        )
    return ((OVERFLOW, "true"),)


def counter(name: str, n: int = 1, /, **labels) -> None:
    """Add to a monotonic counter. Thread safe, because the write path this
    measures is the one the concurrency tests run eight threads through.

    `name` and `n` are positional only, and that is not tidiness. Labels arrive
    as kwargs, `name` is in the content denylist precisely because somebody
    will reach for it, and without the `/` that call is a TypeError raised from
    inside the telemetry of whatever it was measuring."""
    key = _labels(name, labels)
    with _lock:
        by_labels = _counters.setdefault(name, {})
        resolved = _resolve(by_labels, name, key)
        by_labels[resolved] = by_labels.get(resolved, 0) + n
    _bridge_add(name, n, resolved)


@contextmanager
def timer(name: str, /, **labels):
    """Time the block in milliseconds. Records on the way out whether or not
    the block raised: a timing that only covers successes hides exactly the
    slow path somebody is looking for, and the `outcome` label says which it
    was.

    A caller's own `outcome` label is overwritten rather than colliding. This
    one is observed - the block raised or it did not - and a passed one is a
    guess made before the call."""
    started = time.perf_counter()
    outcome = "ok"
    try:
        yield
    except BaseException:
        outcome = "error"
        raise
    finally:
        elapsed = (time.perf_counter() - started) * 1000
        observe(name, elapsed, **{**labels, "outcome": outcome})


def observe(name: str, value_ms: float, /, **labels) -> None:
    """Record one duration without a `with` block, for a caller that already
    has the number."""
    key = _labels(name, labels)
    with _lock:
        by_labels = _timers.setdefault(name, {})
        resolved = _resolve(by_labels, name, key)
        slot = by_labels.setdefault(
            resolved, {"count": 0, "sum_ms": 0.0, "min_ms": None, "max_ms": None}
        )
        slot["count"] += 1
        slot["sum_ms"] += value_ms
        slot["min_ms"] = value_ms if slot["min_ms"] is None else min(slot["min_ms"], value_ms)
        slot["max_ms"] = value_ms if slot["max_ms"] is None else max(slot["max_ms"], value_ms)
    _bridge_observe(name, value_ms, resolved)


def snapshot() -> dict:
    """Everything recorded so far, as plain JSON serialisable data.

    Labels come back as a dict per series rather than as the tuple key, so the
    result survives `json.dumps` without a custom encoder: the one reader this
    is for is a `/metrics` style handler or a log line, and both want JSON.
    mean_ms is derived here rather than stored, so it cannot drift from the sum
    and count it is derived from.
    """
    with _lock:
        counters = [
            {"name": name, "labels": dict(key), "value": value}
            for name, by_labels in sorted(_counters.items())
            for key, value in by_labels.items()
        ]
        timers = [
            {
                "name": name,
                "labels": dict(key),
                **slot,
                "mean_ms": round(slot["sum_ms"] / slot["count"], 3) if slot["count"] else None,
            }
            for name, by_labels in sorted(_timers.items())
            for key, slot in by_labels.items()
        ]
    return {"counters": counters, "timers": timers}


def emit(message: str = "metrics") -> None:
    """One JSON line with the whole snapshot, through the existing logger."""
    _logger.info(message, extra={"fields": snapshot()})


def reset() -> None:
    """Zero everything. For tests, and for a process that has just emitted and
    wants deltas rather than totals from here.

    Also un-latches the OTel bridge, so a test that changes ECHO_MEMORY_OTEL
    gets a fresh decision rather than the one taken by whichever test ran
    first. Costs one cached import in a real process."""
    global _otel
    with _lock:
        _counters.clear()
        _timers.clear()
        _warned_overflow.clear()
        _otel = None


# --- the OpenTelemetry bridge -------------------------------------------
#
# On by default, which is the one decision in this module worth defending.
#
# Nothing leaves the process unless an application has configured an SDK. With
# no provider, `get_meter` hands back a `_ProxyMeterProvider`'s instrument and
# a record costs 0.044 us, measured over 200,000 calls against a loop floor of
# 0.013 us: about 30 ns of real work, against a write_episode that takes tens
# of milliseconds. So the bridge cannot put anything in anybody's collector
# that they did not configure a collector for, and it cannot be the reason a
# write is slow.
#
# That is also what OTel asks of a library: instrument through the API
# unconditionally and let the application own the SDK. An operator who wants it
# off anyway sets ECHO_MEMORY_OTEL=0.

OTEL_ENV = "ECHO_MEMORY_OTEL"

#: The meter name observations arrive under in a collector.
OTEL_METER = "echo_memory"

#: The instruments, or False once the bridge has been turned off for good.
#: None means "not looked yet". Latched rather than retried, so a broken or
#: absent OTel costs one import attempt rather than one per observation.
_otel: dict | bool | None = None


def _otel_enabled() -> bool:
    return os.environ.get(OTEL_ENV, "1").strip().lower() not in {"0", "false", "no", "off"}


def _otel_instruments():
    global _otel
    if _otel is not None:
        return _otel or None
    if not _otel_enabled():
        _otel = False
        return None
    try:
        from opentelemetry import metrics as otel_metrics

        _otel = {
            "meter": otel_metrics.get_meter(OTEL_METER),
            "counters": {},
            "histograms": {},
        }
    except Exception:  # noqa: BLE001 - any failure here means "no bridge"
        _otel = False
        return None
    return _otel


def _disable_bridge(name: str) -> None:
    global _otel
    _otel = False
    _logger.warning("otel_bridge_disabled", extra={"metric": name})


def _bridge_add(name: str, n: int, key: tuple) -> None:
    instruments = _otel_instruments()
    if not instruments:
        return
    try:
        instrument = instruments["counters"].get(name)
        if instrument is None:
            instrument = instruments["meter"].create_counter(
                name, description="echo-memory count"
            )
            instruments["counters"][name] = instrument
        instrument.add(n, attributes=dict(key))
    except Exception:  # noqa: BLE001 - telemetry must not fail the operation
        _disable_bridge(name)


def _bridge_observe(name: str, value_ms: float, key: tuple) -> None:
    instruments = _otel_instruments()
    if not instruments:
        return
    try:
        instrument = instruments["histograms"].get(name)
        if instrument is None:
            instrument = instruments["meter"].create_histogram(
                name, unit="ms", description="echo-memory duration"
            )
            instruments["histograms"][name] = instrument
        instrument.record(value_ms, attributes=dict(key))
    except Exception:  # noqa: BLE001 - telemetry must not fail the operation
        _disable_bridge(name)
