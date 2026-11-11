"""The acceptance policy at the machine door, which is the whole security
boundary of the event channel.

No database here, and that is the point of `parse` being pure: everything a
reporter may send, and everything it may not, is decided by one function with
no clock of its own. These tests are the specification of that function.

What each case is actually protecting:

    event_id      idempotency. A shape that cannot be made deterministic from
                  the reporter's own run gives a retry no way to be free.
    event_type    a closed set, because the drain branches on it. An unknown
                  type has no handler, and defaulting one to "record it as a
                  fact" is how an event channel becomes an arbitrary write
                  endpoint.
    schema_version  refused rather than read with the fields this version
                  happens to know.
    occurred_at   the replay window. Naive instants are refused because a
                  reporter in another zone would land hours out and quietly
                  widen it.
    payload size  a hostile caller must not be able to fill the inbox with one
                  request.
    group_id      which graph a call reaches is derived from the credential.
                  A caller that could name a group id could name somebody
                  else's.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from echo_memory.events.envelope import (
    CLAIM_VERIFIED,
    DEPLOYMENT_COMPLETED,
    MAX_AGE,
    MAX_CLOCK_SKEW,
    MAX_PAYLOAD_BYTES,
    REQUIRES_VERIFY,
    SCHEMA_VERSION,
    EnvelopeError,
    parse,
)

NOW = datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC)


def _raw(**overrides) -> dict:
    """A valid event. Every test below changes exactly one thing about it, so
    a failure names the rule that fired rather than the fixture."""
    raw = {
        "schema_version": SCHEMA_VERSION,
        "event_id": "deploy-ab12cd34-prod",
        "event_type": DEPLOYMENT_COMPLETED,
        "occurred_at": NOW.isoformat(),
        "actor": {"agent": "ci.github", "run_id": "4812"},
        "scope": {"memory": "default", "scope": "shared"},
        "payload": {"decision": "migrate-payments", "outcome": "checkout-errors",
                    "sign": "success"},
    }
    raw.update(overrides)
    return raw


def test_a_valid_envelope_round_trips_through_as_dict():
    envelope = parse(_raw(), now=NOW)
    assert envelope.event_id == "deploy-ab12cd34-prod"
    assert envelope.event_type == DEPLOYMENT_COMPLETED
    assert envelope.actor.agent == "ci.github"
    assert envelope.scope.memory == "default"

    # Re-parsing what as_dict emitted has to give the same envelope back. The
    # inbox stores as_dict and the drain parses it again, so if those two
    # disagree an event changes meaning between being accepted and being
    # applied - and nothing would say so.
    again = parse(envelope.as_dict(), now=NOW)
    assert again == envelope


def test_an_event_must_be_an_object():
    with pytest.raises(EnvelopeError, match="must be an object"):
        parse(["not", "an", "event"], now=NOW)


@pytest.mark.parametrize(
    "event_id",
    [
        "",             # absent
        "short",        # under 8 characters
        "-leading",     # must start alphanumeric, so a flag cannot be an id
        "has spaces",
        "has/slash",    # excluded so an id is safe in a URL path
        "x" * 129,      # over 128
    ],
)
def test_a_bad_event_id_shape_is_refused(event_id):
    with pytest.raises(EnvelopeError, match="event_id"):
        parse(_raw(event_id=event_id), now=NOW)


def test_the_error_for_a_bad_event_id_says_to_make_it_deterministic():
    """The message is the whole of what a reporter's author gets. An id that
    is merely well formed and random passes this check and then silently
    defeats the only reason the field exists, so the error has to say so
    while somebody is reading it."""
    with pytest.raises(EnvelopeError) as e:
        parse(_raw(event_id="bad"), now=NOW)
    assert "idempotency" in str(e.value) and "deterministic" in str(e.value)


def test_an_unknown_event_type_is_refused():
    with pytest.raises(EnvelopeError, match="event_type"):
        parse(_raw(event_type="database.dropped"), now=NOW)


def test_an_unknown_schema_version_is_refused_rather_than_read():
    with pytest.raises(EnvelopeError) as e:
        parse(_raw(schema_version=99), now=NOW)
    assert "99" in str(e.value)


def test_a_naive_occurred_at_is_refused():
    with pytest.raises(EnvelopeError, match="timezone offset"):
        parse(_raw(occurred_at="2026-11-11T12:00:00"), now=NOW)


def test_occurred_at_must_parse_as_an_instant():
    with pytest.raises(EnvelopeError, match="ISO 8601"):
        parse(_raw(occurred_at="last tuesday"), now=NOW)


def test_a_future_occurred_at_within_the_skew_is_accepted():
    """A CI runner with a drifting clock is normal, and refusing it would make
    the channel unusable from the machines it exists for."""
    ahead = NOW + MAX_CLOCK_SKEW - timedelta(seconds=1)
    assert parse(_raw(occurred_at=ahead.isoformat()), now=NOW).occurred_at == ahead


def test_a_future_occurred_at_beyond_the_skew_is_refused():
    ahead = NOW + MAX_CLOCK_SKEW + timedelta(minutes=1)
    with pytest.raises(EnvelopeError, match="future"):
        parse(_raw(occurred_at=ahead.isoformat()), now=NOW)


def test_an_event_older_than_the_replay_window_is_refused():
    stale = NOW - MAX_AGE - timedelta(hours=1)
    with pytest.raises(EnvelopeError, match="replay window"):
        parse(_raw(occurred_at=stale.isoformat()), now=NOW)


def test_the_replay_refusal_names_the_operator_path():
    """A genuine backfill is a real need, and a reporter told only "too old"
    will work around it by lying about occurred_at - which is the one value in
    the envelope nothing else can check."""
    stale = NOW - MAX_AGE - timedelta(hours=1)
    with pytest.raises(EnvelopeError) as e:
        parse(_raw(occurred_at=stale.isoformat()), now=NOW)
    assert "backfill" in str(e.value)


def test_an_event_with_no_actor_is_refused():
    with pytest.raises(EnvelopeError, match="actor.agent"):
        parse(_raw(actor={}), now=NOW)


def test_a_blank_actor_agent_is_refused_like_a_missing_one():
    with pytest.raises(EnvelopeError, match="actor.agent"):
        parse(_raw(actor={"agent": "   "}), now=NOW)


def test_an_oversized_payload_is_refused():
    with pytest.raises(EnvelopeError, match="over the"):
        parse(_raw(payload={"log": "x" * (MAX_PAYLOAD_BYTES + 1)}), now=NOW)


def test_a_payload_that_is_not_an_object_is_refused():
    with pytest.raises(EnvelopeError, match="payload must be an object"):
        parse(_raw(payload=["a", "b"]), now=NOW)


def test_a_group_id_in_scope_is_refused_rather_than_stripped():
    """Stripping it would accept the request. A caller that sent a group id
    believes it may name one, and the honest answer is that it may not -
    silently ignoring the field leaves it believing that until the day the
    field starts working."""
    with pytest.raises(EnvelopeError, match="group_id"):
        parse(_raw(scope={"memory": "default", "group_id": "someone-else"}), now=NOW)


def test_an_unknown_scope_kind_is_refused():
    with pytest.raises(EnvelopeError, match="solo"):
        parse(_raw(scope={"scope": "everyone"}), now=NOW)


def test_scope_defaults_to_shared_when_unnamed():
    assert parse(_raw(scope={}), now=NOW).scope.scope == "shared"


def test_claim_verified_is_the_only_type_needing_verify():
    """If this set ever grows by accident, a reporter credential gets a path to
    assert its own evidence was sufficient. The membership is asserted here so
    that widening it has to be deliberate."""
    assert REQUIRES_VERIFY == {CLAIM_VERIFIED}
    assert DEPLOYMENT_COMPLETED not in REQUIRES_VERIFY
