"""The reader and the scorer, which are the two places a benchmark lies.

A published-benchmark harness is only worth having if its numbers can be
trusted, and the ways it can flatter itself are all here rather than in the
database: dropping questions it cannot answer, counting a near miss as a hit,
averaging a metric over the rows that happened to have it, or reading a prefix
of a type-ordered file and calling it a sample.

The fixtures are synthetic files in the two real schemas. They exist so this
suite needs neither a 2.8MB download nor a 278MB one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from echo_memory.eval.external import (
    DATASET_URLS,
    DATASETS,
    KS,
    MAX_FACT,
    PREFIXES,
    Question,
    Run,
    chunks,
    content_words,
    load_locomo,
    load_longmemeval,
    means,
    report,
    score_question,
)
from echo_memory.ingestion.write_episode import MAX_STRING_LEN

FIXTURES = Path(__file__).parent.parent / "fixtures"
LOCOMO = str(FIXTURES / "locomo_fixture.json")
LONGMEMEVAL = str(FIXTURES / "longmemeval_fixture.json")


def _fact(key: str, text: str = "something was said") -> dict:
    return {"fact": text, "provenance": {"project": key}}


# --- reading the corpora -------------------------------------------------


def test_locomo_turns_carry_their_dia_id_and_session():
    instance = next(load_locomo(LOCOMO))

    assert instance.group_id == "locomo:fixture-conv-1"
    assert len(instance.turns) == 12
    assert instance.turns[0].key == "D1:1"
    assert instance.sessions["D2:3"] == "D2"


def test_locomo_dates_reach_the_fact_text():
    """A third of these benchmarks is temporal. A store that never recorded
    when something was said cannot answer those at any k."""
    turn = next(load_locomo(LOCOMO)).turns[0]

    assert turn.fact.startswith("Ada said on 3 May 2024:")


def test_a_locomo_question_citing_no_turn_is_dropped_not_counted():
    """Four of the real 1,986 cite nothing. Scoring them as misses would
    quietly deflate every number in the table."""
    questions = next(load_locomo(LOCOMO)).questions

    assert len(questions) == 4
    assert all(q.gold_turns for q in questions)


def test_an_adversarial_question_is_still_scored_for_retrieval():
    """It carries `adversarial_answer` instead of `answer`, because the right
    reply is that the transcript does not say. That is a QA property; the cited
    turn still either came back or did not."""
    adversarial = [
        q for q in next(load_locomo(LOCOMO)).questions if q.category == "adversarial"
    ]

    assert len(adversarial) == 1
    assert adversarial[0].gold_turns == {"D1:4"}
    assert adversarial[0].answer == ""


def test_locomo_refuses_per_type_rather_than_ignoring_it():
    """Every LoCoMo conversation carries all five categories, so there is no
    per-type prefix to take. A flag that silently did nothing would be read as
    one that worked."""
    with pytest.raises(ValueError, match="LongMemEval flag"):
        next(load_locomo(LOCOMO, per_type=2))


def test_longmemeval_gold_turns_come_from_has_answer():
    instances = list(load_longmemeval(LONGMEMEVAL))

    first = instances[0]
    assert first.group_id == "lme:fixture-single-session-user"
    assert first.questions[0].gold_turns == {"fixture_answer_1#0"}
    assert first.questions[0].gold_sessions == {"fixture_answer_1"}


def test_longmemeval_per_type_takes_one_of_each_not_a_prefix():
    """The real file is ordered by question type: its first 70 instances are
    all single-session-user, one of the easiest categories. A prefix is a
    sample of one category, which is why `--limit` is for smoke tests and
    `--per-type` is for results."""
    taken = list(load_longmemeval(LONGMEMEVAL, per_type=1))

    assert [i.questions[0].category for i in taken] == [
        "single-session-user", "temporal-reasoning", "knowledge-update",
    ]


def test_a_long_turn_becomes_several_facts_under_one_gold_key():
    """The split is what stops write_episode refusing it. Scoring has to stay
    whole across the pieces, so every piece keeps the turn's key and each gets
    its own node."""
    handover = list(load_longmemeval(LONGMEMEVAL))[2]

    pieces = [t for t in handover.turns if t.key == "fixture_answer_3#0"]
    assert len(pieces) > 1
    assert all(len(p.text) <= MAX_FACT for p in pieces)
    assert len({p.node for p in pieces}) == len(pieces)


def test_every_dataset_has_a_scope_prefix_and_somewhere_to_get_it():
    """The resume check and the scratch-database guard key off the prefix; the
    URL is what a missing path prints instead of a FileNotFoundError."""
    assert set(PREFIXES) == set(DATASETS) == set(DATASET_URLS)


# --- splitting a turn the store would refuse ------------------------------


def test_short_text_is_left_alone():
    assert chunks("a single short turn") == ["a single short turn"]


def test_no_piece_can_be_refused_by_write_episode():
    """The prefix ("the assistant said on <date>: ") is added after chunking,
    so the budget has to leave room for it."""
    pieces = chunks("word " * 4_000)

    assert len(pieces) > 1
    assert all(len(p) <= MAX_FACT for p in pieces)
    assert MAX_FACT < MAX_STRING_LEN


def test_nothing_is_lost_in_the_split():
    text = "\n\n".join(f"Paragraph {i}. " + "filler " * 200 for i in range(12))

    rejoined = " ".join(chunks(text)).split()

    assert rejoined == text.split()


def test_it_prefers_a_paragraph_or_sentence_boundary():
    """A chunk that stops mid word is still retrievable and still unreadable.
    Cutting at a boundary costs nothing when one is nearby."""
    text = ("First part. " * 200) + "\n\n" + ("Second part. " * 200)

    assert chunks(text)[0].endswith(".")


def test_a_boundary_too_far_back_is_ignored():
    """Falling back to a hard cut matters: one 4,000 character line with a full
    stop at character 3 must not produce a 3 character chunk and loop."""
    pieces = chunks("ab. " + "x" * 8_000)

    assert all(len(p) <= MAX_FACT for p in pieces)
    assert len(pieces) == 3


# --- scoring one question -------------------------------------------------


QUESTION = Question(
    question_id="q1", question="what did Ada adopt?", category="single hop",
    gold_turns=frozenset({"D1:3", "D2:2"}), gold_sessions=frozenset({"D1", "D2"}),
    answer="a three legged tortoise",
)
SESSIONS = {"D1:1": "D1", "D1:3": "D1", "D2:2": "D2", "D2:9": "D2"}


def test_a_hit_at_rank_two_is_reciprocal_rank_one_half():
    row = score_question(QUESTION, [_fact("D1:1"), _fact("D1:3")], SESSIONS)

    assert row["rr"] == 0.5
    assert row["recall@5"] == 0.5
    assert row["hit@5"] == 1.0


def test_a_hit_outside_k_is_a_miss_at_that_k():
    returned = [_fact(f"filler-{i}") for i in range(9)] + [_fact("D1:3")]

    row = score_question(QUESTION, returned, SESSIONS)

    assert row["recall@5"] == 0.0
    assert row["hit@5"] == 0.0
    assert row["recall@10"] == 0.5
    assert row["rr"] == 0.1


def test_a_question_nothing_answered_is_scored_zero_not_skipped():
    row = score_question(QUESTION, [_fact("D2:9")], SESSIONS)

    assert row["rr"] == 0.0
    assert all(row[f"recall@{k}"] == 0.0 for k in KS)


def test_session_recall_uses_the_map_rather_than_the_key_shape():
    """A returned key the instance never wrote matches no gold session, rather
    than being guessed at by splitting the string. Two datasets spell the
    turn-to-session relationship differently and one copy of that derivation is
    enough: the last time a reader re-derived an id, it went blind twice."""
    row = score_question(QUESTION, [_fact("D1:3")], SESSIONS)
    unknown = score_question(QUESTION, [_fact("D9:9#0")], SESSIONS)

    assert row["session@5"] == 0.5
    assert unknown["session@5"] == 0.0


def test_the_answer_metric_reads_the_returned_text_not_the_key():
    row = score_question(
        QUESTION,
        [_fact("D1:3", "Ada said: I adopted a three legged tortoise called Mortimer")],
        SESSIONS,
    )

    assert row["answer_scoreable"] is True
    assert row["answer_words@5"] == 1.0


def test_a_one_word_gold_answer_is_excluded_from_the_answer_metric():
    """"yes" or "2022" appears somewhere in ten facts by chance, and a metric
    that counted those would drift upwards with k for no reason."""
    trivial = Question(
        question_id="q2", question="did Ada adopt anything?", category="temporal",
        gold_turns=frozenset({"D1:3"}), gold_sessions=frozenset({"D1"}), answer="yes",
    )

    row = score_question(trivial, [_fact("D1:3", "Ada said: yes")], SESSIONS)

    assert row["answer_scoreable"] is False
    assert "answer_words@5" not in row


def test_stopwords_do_not_count_as_answer_words():
    assert content_words("the of and it") == set()
    assert content_words("Business Administration") == {"business", "administration"}


# --- averaging, and what the report promises ------------------------------


def test_the_answer_metric_averages_over_its_own_subset():
    """Averaging it over every row would divide by questions that supply no
    gold answer, which reads as a failure to answer them."""
    scoreable = score_question(
        QUESTION, [_fact("D1:3", "a three legged tortoise")], SESSIONS
    )
    not_scoreable = score_question(
        Question("q3", "?", "temporal", frozenset({"D1:3"}), frozenset({"D1"}), "yes"),
        [_fact("D1:3", "yes")], SESSIONS,
    )

    stats = means([scoreable, not_scoreable])

    assert stats["n"] == 2
    assert stats["answer_scoreable"] == 1
    assert stats["answer_words@5"] == 1.0


def test_the_report_says_accuracy_is_absent_and_why():
    """Both benchmarks publish model-judged QA accuracy. This harness calls no
    model, so the field is null with its reason beside it rather than filled
    with a retrieval number wearing accuracy's name."""
    out = Run(dataset="locomo", path=LOCOMO, top_k=30)
    out.rows.append(score_question(QUESTION, [_fact("D1:3")], SESSIONS))

    payload = json.loads(json.dumps(report(out)))

    assert payload["accuracy"] is None
    assert "no model" in payload["accuracy_unavailable_because"]
    assert payload["measures"] == "retrieval"
    assert payload["overall"]["n"] == 1
    # One of the question's two cited turns came back, so half of them did.
    assert payload["by_category"]["single hop"]["recall@30"] == 0.5
