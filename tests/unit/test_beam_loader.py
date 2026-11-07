"""BEAM, the third corpus, and the three things its format does differently.

BEAM is 100 conversations and 2,000 human-validated questions at 128K, 500K,
1M and 10M tokens, over ten memory abilities. Two things make it worth the
adapter rather than more LoCoMo runs: its questions were written by people
instead of derived from their own answers, and its largest scale is where this
store's lack of consolidation stops being a caveat and becomes the measurement.

The fixture is a real row from the published dataset, trimmed to three
sessions of four messages and one question per ability, with the question
block left as the Python repr string the dataset actually ships.
"""

from __future__ import annotations

import json
from pathlib import Path

from echo_memory.eval.external import (
    DATASETS,
    PREFIXES,
    Question,
    load_beam,
    score_question,
)

FIXTURE = str(Path(__file__).parent.parent / "fixtures" / "beam_fixture.json")


def only() -> object:
    instances = list(load_beam(FIXTURE))
    assert len(instances) == 1
    return instances[0]


def test_beam_is_registered_like_the_other_two():
    assert DATASETS["beam"] is load_beam
    assert PREFIXES["beam"] == "beam"


def test_the_question_block_is_a_python_repr_not_json():
    """Single quoted, so `json.loads` raises on every row of the real
    dataset. Asserted on the fixture rather than in prose, because the fixture
    is the published shape and a future rewrite to `json.loads` would pass
    every other test here."""
    raw = json.loads(Path(FIXTURE).read_text())[0]["probing_questions"]

    assert isinstance(raw, str)
    assert raw.lstrip().startswith("{'"), "the fixture is no longer the published shape"
    try:
        json.loads(raw)
    except json.JSONDecodeError:
        pass
    else:
        raise AssertionError("this would not have needed ast.literal_eval")

    assert len(only().questions) == 10


def test_a_time_anchor_carries_to_every_message_in_its_session():
    """BEAM puts the anchor on the first message of a session and nowhere
    else. A tenth of the benchmark is temporal reasoning, and `when` rides
    into every fact's text, so without carrying it forward only the first
    message of each session would be dated and the ability it measures most
    directly would fail for a reason that is ours."""
    turns = only().turns
    first = turns[0]

    assert first.when, "the first message lost its own anchor"
    dated = [t for t in turns if t.session == first.session and t.when == first.when]
    assert len(dated) > 1, "the anchor did not carry past the first message"
    assert first.when in first.fact, "the date is not in the stored text"


def test_a_session_does_not_inherit_another_sessions_anchor():
    """Sticky within a session, not across one. A date from session 1 pasted
    onto session 2's messages would be a wrong answer rather than a missing
    one, which is worse."""
    turns = only().turns
    sessions = {t.session for t in turns}

    assert len(sessions) == 3
    for session in sessions:
        anchors = {t.when for t in turns if t.session == session}
        assert len(anchors) <= 2, f"{session} mixes anchors from elsewhere: {anchors}"


def test_the_generators_index_marker_is_not_stored_as_speech():
    """Every session's first message ends with `->-> 1,1`, which is BEAM's own
    bookkeeping. Left in, it becomes part of a stored fact and part of what a
    judge reads back."""
    for turn in only().turns:
        assert "->->" not in turn.text, turn.text[-40:]


def test_gold_ids_are_read_from_all_three_spellings():
    """Flat list, mapping of lists, and strings like "chat_id: 58". Three
    shapes in one dataset, and a loader that handled only the first would
    silently score five abilities as retrieving nothing."""
    with_gold = [q for q in only().questions if q.gold_turns]
    abilities = {q.category for q in with_gold}

    # A flat list of ints.
    assert "information_extraction" in abilities
    # A mapping whose values are lists of ints.
    assert "knowledge_update" in abilities or "temporal_reasoning" in abilities
    # Strings carrying the id.
    assert "contradiction_resolution" in abilities


def test_an_abstention_question_is_kept_rather_than_dropped():
    """LoCoMo's loader drops a question citing no turn, because there it means
    nobody wrote the evidence down. Here it means abstention, which BEAM
    measures on purpose and which a memory store should be good at: the right
    answer is that the transcript does not say."""
    abstention = [q for q in only().questions if q.category == "abstention"]

    assert abstention, "the abstention ability was dropped"
    assert not abstention[0].gold_turns
    assert abstention[0].answer, "it has no gold response to judge against"


def test_an_unanswerable_question_is_not_scored_as_a_retrieval_miss():
    """The scoring half of the one above. A question with nothing to retrieve
    cannot be scored on turn-keyed metrics, and recording 0.0 would be a
    number about the question rather than about retrieval - and would deflate
    every headline by a tenth on this corpus."""
    unanswerable = Question(
        question_id="q", question="what did they say about X?",
        category="abstention", gold_turns=frozenset(), gold_sessions=frozenset(),
        answer="the chat does not say",
    )

    row = score_question(unanswerable, [{"fact": "something", "provenance": {}}], {})

    assert row["recall@10"] is None, "an abstention question was scored as a miss"
    assert row["rr"] is None
    assert row["attributed"] is False


def test_every_ability_has_a_gold_answer_to_judge_against():
    """Four different field names across ten abilities, and two abilities with
    none at all - instruction_following and preference_following are scored on
    whether a response complies rather than on what it says, so the rubric is
    the gold. An ability with an empty answer is one the judged pass cannot
    score."""
    for question in only().questions:
        assert question.answer, f"{question.category} has no answer and no rubric"


def test_the_questions_carry_their_ability_as_the_category():
    """The whole reason for running this corpus: BEAM reports per ability, and
    contradiction resolution, knowledge update and temporal reasoning are what
    a bi-temporal store is for."""
    categories = {q.category for q in only().questions}

    assert {"contradiction_resolution", "knowledge_update", "temporal_reasoning"} <= categories
    assert len(categories) == 10


def test_a_long_message_splits_into_several_facts_under_one_key():
    """write_episode refuses a fact over MAX_FACT characters and BEAM's
    assistant turns go well past it, so a message becomes several facts.

    Every piece keeps the message id as its `key`, which is what gold cites,
    and takes its own `node`, because a fact is an edge keyed by (source,
    target, relation_type) and two pieces sharing a target would supersede one
    another rather than coexist. That failure is silent: the scope ends up
    with fewer facts than turns and scores as a retrieval miss.
    """
    turns = only().turns
    nodes = [t.node for t in turns]

    assert len(nodes) == len(set(nodes)), "two pieces share a node and will supersede"
    assert len(turns) > len({t.key for t in turns}), (
        "no message in the fixture split, so this no longer tests anything"
    )
    for key in {t.key for t in turns}:
        pieces = [t for t in turns if t.key == key]
        assert len({t.session for t in pieces}) == 1, "a split crossed sessions"


def test_gold_cites_only_turns_the_loader_produced():
    """If the key spelling and the gold spelling ever drift apart, every
    question scores a clean zero and nothing says why."""
    instance = only()
    cited = set().union(*(q.gold_turns for q in instance.questions))

    assert cited, "no question in the fixture cites a turn"
    assert cited <= {t.key for t in instance.turns}


# --- the 10M split, which is a different dataset with a different shape ---

TEN_M = str(Path(__file__).parent.parent / "fixtures" / "beam_10m_fixture.json")


def ten_m() -> object:
    instances = list(load_beam(TEN_M))
    assert len(instances) == 1
    return instances[0]


def test_the_10m_split_nests_two_levels_deeper():
    """BEAM-10M is published as a separate dataset and does not share the
    other splits' layout. A session is a mapping of `plan-N` to a list of
    batches, a batch is `{batch_number, time_anchor, turns}`, and `turns` is a
    list of LISTS of messages.

    A loader that assumed the flat shape does not raise on this: `chat[0][0]`
    on a dict raises KeyError, and anything more forgiving yields nothing and
    reports a conversation with no turns, which scores as a clean zero.
    """
    raw = json.loads(Path(TEN_M).read_text())[0]["chat"]

    assert isinstance(raw[0], dict), "the fixture is no longer the 10M shape"
    assert all(k.startswith("plan-") for k in raw[0])
    batch = next(b for v in raw[0].values() if v for b in v)
    assert {"batch_number", "time_anchor", "turns"} <= set(batch)
    assert isinstance(batch["turns"][0], list), "turns is a list of lists"

    assert ten_m().turns, "the 10M shape produced no turns"


def test_both_shapes_produce_the_same_kind_of_instance():
    """The point of normalising in one place: everything after it - chunking,
    keys, anchors, the gold intersection - is identical, and a second loader
    would be a second thing to get wrong."""
    for instance in (only(), ten_m()):
        assert instance.prefix == "beam"
        assert len(instance.questions) == 10
        assert all(t.key and t.node and t.session for t in instance.turns)
        assert all(q.answer for q in instance.questions)


def test_a_batch_anchor_reaches_the_messages_under_it():
    """At 10M the date is on the batch rather than on a message, so without
    pushing it down the temporal ability would be unanswerable for a reason
    that is ours rather than the benchmark's."""
    assert all(t.when for t in ten_m().turns), "a 10M turn has no date"


def test_plans_are_ordered_by_number_not_lexically():
    """`plan-10` sorts before `plan-2` lexically. Order decides the sequence
    facts are written in, and a conversation read out of order is a different
    conversation to anything measuring event ordering."""
    from echo_memory.eval.external import _plan_order

    names = ["plan-1", "plan-10", "plan-2", "plan-9"]

    assert sorted(names, key=_plan_order) == ["plan-1", "plan-2", "plan-9", "plan-10"]
