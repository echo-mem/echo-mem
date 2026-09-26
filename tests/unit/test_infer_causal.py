"""The guards that keep a late backfill on the extraction side of the line.

The standing refusal is statistical causal discovery: never infer causation from
co-occurrence, structure or correlation. A pass that re-reads fact text is only
extraction if it can be shown to read, so the reading is gated three times and
these tests hold each gate.

Offline by construction. Nothing here calls a model; the model's job is one
judgement in the middle and everything around it is testable without one, which
is also why the same guards catch a model that answers badly.
"""

from __future__ import annotations

import json

import pytest

from echo_memory.cli.infer_causal import (
    Candidate,
    InferenceError,
    Proposal,
    parse_response,
    quotes_the_text,
    render_clear,
    render_dry_run,
    render_prompt,
    render_scan,
    states_a_cue,
)
from echo_memory.ingestion.write_episode import VALID_CAUSAL_HINTS

POOL = Candidate("1", "the pool was sized 5 so checkout returned 502s", "pool", "checkout")
WEEK = Candidate("2", "the deploy and the outage happened the same week", "deploy", "outage")


# --- gate 1: the sentence must contain a connective that asserts a relation ---

@pytest.mark.parametrize("text", [
    "the pool was sized 5 so checkout returned 502s",
    "checkout returned 502s because the pool was sized 5",
    "the migration led to 30 authorless facts",
    "the org move blocked owner auth",
    "AGE's graphid is not a bigint, which is why the DELETE took everything",
    "the index was missing, therefore every read was a sequential scan",
])
def test_a_stated_relation_is_a_candidate(text):
    assert states_a_cue(text)


@pytest.mark.parametrize("text", [
    "the deploy and the outage happened the same week",
    "the pool rewrite, then the Friday outage",
    "checkout returned 502s while the pool was sized 5",
    "after the migration there were 30 authorless facts",
    "the store holds 38,479 edges and 2 causal hints",
    "",
])
def test_sequence_and_co_occurrence_are_not_candidates(text):
    """The refusal, at its cheapest. "X, then Y" and "X and Y both happened" are
    exactly the co-occurrence a statistical pass would type, and they never
    reach a model here."""
    assert not states_a_cue(text)


@pytest.mark.parametrize("text", [
    "the sonar readings were fine",
    "the causeway was closed",
    "a reasonable default",
])
def test_a_cue_inside_a_longer_word_is_not_a_cue(text):
    assert not states_a_cue(text)


# --- gate 2: the model must point at the words --------------------------------

def test_a_quote_lifted_from_the_fact_passes():
    assert quotes_the_text("sized 5 so checkout returned 502s", POOL.fact)


def test_whitespace_and_case_are_forgiven():
    """A model re-typing a span gets those wrong without changing what it
    pointed at."""
    assert quotes_the_text("Sized 5   so  checkout", POOL.fact)


@pytest.mark.parametrize("quote", [
    "the pool caused the 502s",            # a paraphrase, not the sentence
    "connection pool exhaustion",          # plausible, and not in the text
    "",
])
def test_a_quote_that_is_not_in_the_fact_fails(quote):
    """This is the guard that stops reasoning from the world getting stored. A
    model that cannot point at the words does not get the hint."""
    assert not quotes_the_text(quote, POOL.fact)


# --- the prompt ---------------------------------------------------------------

def test_the_prompt_carries_both_endpoints_and_the_closed_set():
    prompt = render_prompt([POOL, WEEK])
    for candidate in (POOL, WEEK):
        assert candidate.fact in prompt
        assert candidate.source in prompt and candidate.target in prompt
    # Orientation is the reason the set has to be spelled out: "A led_to B" and
    # "B caused_by A" are the same claim from opposite ends, and the answer is
    # only usable if it is given from the source's end.
    for hint in VALID_CAUSAL_HINTS:
        assert hint in prompt


# --- parsing ------------------------------------------------------------------

def _response(items) -> str:
    return "Here you go:\n```json\n" + json.dumps(items) + "\n```"


def test_a_fenced_response_is_read_rather_than_refused():
    found = parse_response(
        _response([{"n": 1, "hint": "led_to", "quote": "sized 5 so checkout"}]),
        [POOL],
    )
    assert found == {"1": {"hint": "led_to", "quote": "sized 5 so checkout"}}


def test_a_null_hint_is_the_ordinary_answer_and_yields_nothing():
    assert parse_response(_response([{"n": 1, "hint": None, "quote": None}]), [POOL]) == {}


@pytest.mark.parametrize("item", [
    {"n": 1, "hint": "because_of", "quote": "so checkout"},   # not in the closed set
    {"n": 1, "hint": "led_to"},                               # no quote to check
    {"n": 1, "hint": "led_to", "quote": "   "},
    {"n": 7, "hint": "led_to", "quote": "so checkout"},       # not an item we asked about
    {"hint": "led_to", "quote": "so checkout"},               # no item number
    "not an object",
])
def test_an_item_out_of_shape_is_dropped_and_the_batch_survives(item):
    """A hint outside the closed set is unwalkable, so storing it would make a
    fact read as causally typed while answering nothing. Dropping one item beats
    discarding seven good ones."""
    found = parse_response(
        _response([item, {"n": 2, "hint": "caused_by", "quote": "the same week"}]),
        [POOL, WEEK],
    )
    assert found == {"2": {"hint": "caused_by", "quote": "the same week"}}


@pytest.mark.parametrize("text", ["", "I could not do that", "{\"n\": 1}"])
def test_a_response_with_no_array_raises_rather_than_recording_no_cause(text):
    """Recording "this sentence states no cause" for a whole batch because a
    response was malformed would write a permanent wrong verdict from a
    transport-level problem."""
    with pytest.raises(InferenceError):
        parse_response(text, [POOL])


def test_unparseable_json_raises():
    with pytest.raises(InferenceError):
        parse_response("[{'n': 1,}]", [POOL])


# --- what an operator reads ---------------------------------------------------

def _scan_result(proposals):
    return {
        "read": 2, "no_cue": 1, "examined": 1, "proposed": len(proposals),
        "quote_rejected": 0, "proposals": proposals,
        "facts": {POOL.edge_id: POOL},
    }


def test_a_dry_run_shows_the_quote_next_to_every_hint():
    """The quote is the justification. An operator deciding whether to pass
    --write is deciding whether those words really say what the hint claims."""
    out = render_scan(
        "solo", _scan_result([Proposal("1", "led_to", "sized 5 so checkout")]),
        remaining=0, write=False,
    )
    assert "Would set:" in out
    assert "led_to" in out and "pool -> checkout" in out
    assert "sized 5 so checkout" in out


def test_a_dry_run_says_plainly_that_nothing_was_written():
    assert "Nothing was written" in render_dry_run()


def test_a_run_that_found_nothing_says_so_rather_than_printing_an_empty_list():
    out = render_scan("solo", _scan_result([]), remaining=12, write=False)
    assert "Nothing in this window states a cause." in out
    assert "12 fact(s) in this scope still unexamined" in out


def test_clear_with_nothing_to_clear_does_not_imply_it_did_something():
    out = render_clear("solo", [], dry_run=True)
    assert "No hints" in out


def test_clear_lists_what_it_would_remove_before_removing_it():
    out = render_clear("solo", [("1", "led_to")], dry_run=True)
    assert "Would clear 1 hint(s)" in out and "led_to" in out
    assert "Nothing was written" in out
