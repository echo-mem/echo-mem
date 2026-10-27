"""The judged QA pass, tested without calling a model.

Every provider call goes through `echo_memory.cli.llm.complete`, so a stub of
that is the whole seam. What matters here is not the prompts but the arithmetic
and the three places a null is meant to survive: a judge that answered neither
word, a provider that failed, and an answer the facts could not support.
"""

from __future__ import annotations

import json

import pytest

from echo_memory.cli.llm import ModelUnavailable
from echo_memory.eval import qa


class Stub:
    """Answers scripted by which system prompt it is given."""

    def __init__(self, answer="a three legged tortoise", judgements=None):
        self._answer = answer
        self._judgements = list(judgements or ["CORRECT"])
        self.prompts = []

    def __call__(self, provider, system, prompt, max_tokens=2000):
        self.prompts.append((provider, system, prompt))
        if system is qa.ANSWER_SYSTEM or "ONLY the numbered facts" in system:
            if isinstance(self._answer, Exception):
                raise self._answer
            return self._answer
        return self._judgements.pop(0) if self._judgements else "CORRECT"


@pytest.fixture
def stub(monkeypatch):
    s = Stub()
    monkeypatch.setattr(qa, "complete", s)
    return s


def _results(tmp_path, rows):
    path = tmp_path / "results.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return str(path)


ROW = {
    "question_id": "q1", "category": "single hop",
    "question": "what did Ada adopt?", "gold_answer": "a three legged tortoise",
    "facts": ["Ada adopted a three legged tortoise", "unrelated"],
}


# --- what the answerer is shown ------------------------------------------


def test_the_answerer_is_never_shown_the_gold_answer(tmp_path, monkeypatch):
    """Leakage would make every accuracy number meaningless."""
    s = Stub()
    monkeypatch.setattr(qa, "complete", s)

    qa.judge_run(_results(tmp_path, [ROW]), "p", {"a": "p"})

    answer_prompt = s.prompts[0][2]
    assert "Ada adopted a three legged tortoise" in answer_prompt, "the facts are shown"
    assert "Reference answer" not in answer_prompt
    assert answer_prompt.count("a three legged tortoise") == 1, (
        "the gold answer must not appear as a reference, only inside the fact"
    )


def test_the_judge_is_never_told_which_system_produced_the_answer(tmp_path, monkeypatch):
    s = Stub()
    monkeypatch.setattr(qa, "complete", s)

    qa.judge_run(_results(tmp_path, [ROW]), "p", {"a": "p"})

    judge_prompt = s.prompts[1][2]
    for word in ("echo-mem", "mem0", "zep", "backend"):
        assert word not in judge_prompt.lower()


def test_facts_are_numbered_in_rank_order_and_truncated_to_k():
    rendered = qa.render_facts(["first", "second", "third"], limit=2)

    assert rendered == "1. first\n2. second"


def test_no_facts_says_so_rather_than_sending_an_empty_list():
    assert "returned nothing" in qa.render_facts([])


# --- nulls that have to survive ------------------------------------------


def test_a_judge_that_says_neither_word_records_none_not_wrong(monkeypatch):
    """Counting it as wrong would blame the system for the judge's failure."""
    monkeypatch.setattr(qa, "complete", lambda *a, **k: "it depends, somewhat")

    assert qa.verdict("provider", "q", "gold", "candidate") is None


def test_a_judge_is_read_leniently_on_case_and_padding(monkeypatch):
    monkeypatch.setattr(qa, "complete", lambda *a, **k: "  correct.  ")
    assert qa.verdict("p", "q", "g", "c") is True

    monkeypatch.setattr(qa, "complete", lambda *a, **k: "INCORRECT - too vague")
    assert qa.verdict("p", "q", "g", "c") is False


def test_a_judge_failure_is_recorded_and_the_other_judge_still_counts(
    tmp_path, monkeypatch
):
    calls = {"n": 0}

    def flaky(provider, system, prompt, max_tokens=2000):
        if "ONLY the numbered facts" in system:
            return "a three legged tortoise"
        calls["n"] += 1
        if provider == "bad":
            raise ModelUnavailable("judge two is down")
        return "CORRECT"

    monkeypatch.setattr(qa, "complete", flaky)

    records = qa.judge_run(
        _results(tmp_path, [ROW]), "good", {"good": "good", "bad": "bad"}
    )

    assert records[0]["verdicts"]["good"] is True
    assert records[0]["verdicts"]["bad"] is None
    assert qa.accuracy(records, "good")["accuracy"] == 1.0
    assert qa.accuracy(records, "bad")["accuracy"] is None


def test_an_answerer_failure_costs_one_question_not_the_run(tmp_path, monkeypatch):
    monkeypatch.setattr(qa, "complete", Stub(answer=ModelUnavailable("rate limited")))

    records = qa.judge_run(_results(tmp_path, [ROW, dict(ROW, question_id="q2")]), "p",
                           {"a": "p"})

    assert len(records) == 2, "both questions are recorded"
    assert all(r["verdicts"]["a"] is None for r in records)
    assert "rate limited" in records[0]["error"]


def test_cannot_answer_is_counted_apart_from_a_wrong_answer(tmp_path, monkeypatch):
    """For a memory system these are different failures: one did not retrieve,
    the other retrieved and misled."""
    monkeypatch.setattr(qa, "complete", Stub(answer="CANNOT ANSWER",
                                             judgements=["INCORRECT"]))

    records = qa.judge_run(_results(tmp_path, [ROW]), "p", {"a": "p"})
    stats = qa.accuracy(records, "a")

    assert records[0]["cannot_answer"] is True
    assert stats["accuracy"] == 0.0
    assert stats["cannot_answer"] == 1
    assert stats["cannot_answer_rate"] == 1.0


# --- the file contract ----------------------------------------------------


def test_a_results_file_without_retrieved_text_says_so(tmp_path):
    """Rather than judging nothing and reporting a confident zero."""
    path = _results(tmp_path, [{k: v for k, v in ROW.items() if k != "facts"}])

    with pytest.raises(ValueError, match="without retrieved text"):
        list(qa.rows_from(path))


def test_rows_with_no_gold_answer_are_skipped_not_judged(tmp_path):
    rows = [ROW, dict(ROW, question_id="q2", gold_answer="")]

    assert [r["question_id"] for r in qa.rows_from(_results(tmp_path, rows))] == ["q1"]


# --- the agreement arithmetic --------------------------------------------


def _rec(a, b):
    return {"question_id": "x", "category": "c", "answer": "x",
            "cannot_answer": False, "verdicts": {"a": a, "b": b}}


def test_perfect_agreement_is_kappa_one():
    records = [_rec(True, True), _rec(False, False), _rec(True, True)]

    out = qa.agreement(records, "a", "b")

    assert out["raw"] == 1.0
    assert out["kappa"] == 1.0


def test_two_judges_who_always_say_correct_agree_but_score_no_kappa():
    """The number raw agreement alone would hide. Both judges say CORRECT every
    time, so they agree perfectly and have demonstrated nothing: there is no
    variance for them to have agreed about."""
    records = [_rec(True, True) for _ in range(20)]

    out = qa.agreement(records, "a", "b")

    assert out["raw"] == 1.0
    assert out["chance"] == 1.0
    assert out["kappa"] is None, "undefined, not 1.0"


def test_chance_agreement_is_removed():
    """Eight agree, two disagree. Raw 0.8; kappa lower, because two judges this
    lopsided agree often by luck."""
    records = (
        [_rec(True, True)] * 8 + [_rec(True, False), _rec(False, True)]
    )

    out = qa.agreement(records, "a", "b")

    assert out["raw"] == 0.8
    assert out["kappa"] is not None
    assert out["kappa"] < out["raw"], "kappa must be the stricter number"


def test_agreement_only_counts_answers_both_judges_graded():
    records = [_rec(True, True), _rec(True, None), _rec(None, False)]

    out = qa.agreement(records, "a", "b")

    assert out["n"] == 1
