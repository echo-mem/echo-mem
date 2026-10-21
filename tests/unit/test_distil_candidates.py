"""What stops an extraction pass from poisoning the graph.

An LLM reading a chat transcript will invent a decision, promote an idea the
person rejected two turns later, and read a question as a statement. The graph
is the thing this product keeps clean, and supersession would spread a bad
fact rather than contain it, so the guards matter more than the extraction.

The strongest one is not a prompt instruction. It is that a candidate must
quote the words the person typed, checked against the input by string
comparison, which a model cannot talk its way past.
"""

from __future__ import annotations

import json

from echo_memory.distil import candidates as c

TURNS = [
    "never push straight to main, open a pull request every time",
    "actually use Postgres not SQLite, we need the graph extension",
]


def _reply(*items):
    return lambda provider, system, prompt: json.dumps({"facts": list(items)})


def _item(**over):
    base = {
        "subject": "main branch",
        "object": "pull request",
        "relation": "requires",
        "fact": "Never push straight to main; open a pull request every time.",
        "quote": "never push straight to main",
    }
    base.update(over)
    return base


def test_a_candidate_the_model_cannot_quote_is_dropped():
    """The guard that cannot be argued with. A model that invents a decision
    cannot also produce the words the person typed."""
    extraction = c.extract(
        TURNS, None, _reply(_item(), _item(quote="nobody ever typed this sentence"))
    )

    assert len(extraction.candidates) == 1
    assert extraction.candidates[0].subject == "main branch"


def test_a_quote_is_checked_against_the_input_not_merely_present():
    """A non-empty quote is not evidence. It has to be IN the turns."""
    extraction = c.extract(TURNS, None, _reply(_item(quote="x")))

    assert extraction.candidates == []


def test_an_incomplete_item_is_skipped():
    for missing in ("subject", "object", "relation", "fact"):
        extraction = c.extract(TURNS, None, _reply(_item(**{missing: ""})))
        assert extraction.candidates == [], missing


def test_a_reply_that_is_not_json_is_recorded_rather_than_raised():
    extraction = c.extract(TURNS, None, lambda p, s, pr: "I think the answer is no.")

    assert extraction.candidates == []
    assert not extraction.ok
    assert "not JSON" in extraction.failures[0]


def test_json_in_a_fence_is_tolerated():
    """Models fence JSON despite being told not to, and discarding a whole
    batch over punctuation would be the command losing work it already paid
    for."""
    fenced = "```json\n" + json.dumps({"facts": [_item()]}) + "\n```"

    extraction = c.extract(TURNS, None, lambda p, s, pr: fenced)

    assert len(extraction.candidates) == 1


def test_a_provider_failure_does_not_abort_the_other_batches():
    """Thirty batches where one times out should yield twenty-nine batches of
    candidates and one line saying so, not nothing."""
    turns = [f"we decided thing number {i}, never push straight to main" for i in range(90)]
    calls = {"n": 0}

    def flaky(provider, system, prompt):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("timeout")
        return json.dumps({"facts": [_item()]})

    extraction = c.extract(turns, None, flaky)

    assert extraction.batches == 3
    assert len(extraction.candidates) == 2
    assert not extraction.ok
    assert "batch 2" in extraction.failures[0]


def test_more_than_the_cap_is_trimmed():
    """A cap rather than a target. A model asked for as many as possible pads
    to the number."""
    extraction = c.extract(TURNS, None, _reply(*[_item() for _ in range(20)]))

    assert len(extraction.candidates) == c.MAX_PER_BATCH


def test_nothing_worth_reporting_is_a_clean_empty_result():
    extraction = c.extract(TURNS, None, lambda p, s, pr: json.dumps({"facts": []}))

    assert extraction.candidates == []
    assert extraction.ok, "an honest empty answer is not a failure"


def test_a_candidate_becomes_the_arguments_write_episode_expects():
    extraction = c.extract(TURNS, None, _reply(_item()))

    entities, facts = extraction.candidates[0].as_episode()

    assert [e["name"] for e in entities] == ["main branch", "pull request"]
    assert facts[0]["source"] == "main branch"
    assert facts[0]["target"] == "pull request"
    assert facts[0]["relation_type"] == "requires"
    assert facts[0]["confidence"] == "extracted"


def test_a_self_referential_candidate_does_not_declare_one_entity_twice():
    """write_episode requires names to be unique within a call, so a fact
    whose subject and object are the same thing must not list it twice."""
    same = _item(subject="deploys", object="deploys")

    extraction = c.extract(TURNS, None, _reply(same))
    entities, facts = extraction.candidates[0].as_episode()

    assert len(entities) == 1
    assert facts[0]["source"] == facts[0]["target"] == "deploys"


def test_batching_covers_every_turn_exactly_once():
    turns = [f"turn {i}" for i in range(95)]

    batches = list(c.batched(turns, size=40))

    assert [len(b) for b in batches] == [40, 40, 15]
    assert [t for b in batches for t in b] == turns
