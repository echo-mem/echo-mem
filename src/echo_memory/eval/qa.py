"""The judged QA pass over a finished benchmark run.

`eval/external.py` reports retrieval: whether the turn holding the answer came
back. That is a CEILING on QA accuracy and deliberately not comparable to the
accuracy figures Mem0 and Zep publish, which are model-judged answers. This is
the pass that produces a comparable number, and it is separate from the run for
a reason about money: a LoCoMo run against a hosted backend is hours of billed
ingestion, so changing a prompt or adding a judge must not mean paying for that
again.

## How it is kept honest

**The answerer sees only what was retrieved.** Never the gold answer, never the
transcript. If the facts do not contain the answer it is told to say so, and
that outcome is counted separately from a wrong answer, because for a memory
system they are different failures: one did not retrieve, the other retrieved
and misled. Collapsing them hides the distinction this whole project is about.

**The judge never learns which system produced the answer.** Same rule as
`eval/independent.py`: the judge sees a question, a gold answer and a candidate.
A judge that knows whose answer it is grading is not a judge.

**Two judges, from different vendors, over the identical answer.** We choose the
judge, so the question "how much of this depends on your judge" has to be
answerable with a number rather than a reassurance. Both judges score the same
answer string, so their disagreement is judge variance and nothing else - two
judges scoring two different retrievals would be measuring retrieval and
calling it disagreement. Reported as raw agreement and Cohen's kappa, which
corrects for the agreement two judges reach by chance when the answer is
usually yes.

**Both prompts are in this file and both go in the writeup.** A judged number
whose prompt is not published cannot be argued with, and one that cannot be
argued with is not evidence.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator

from echo_memory.cli.llm import ModelUnavailable, complete

#: What the answerer is told. It may only use the facts it is given, and
#: "I cannot answer" is an allowed and distinct answer.
ANSWER_SYSTEM = (
    "You answer a question using ONLY the numbered facts provided. The facts "
    "came from a memory system; they may be irrelevant, partial or absent.\n\n"
    "Rules:\n"
    "- Use only the facts given. Do not use outside knowledge.\n"
    "- Answer in one short phrase or sentence, as a person would.\n"
    "- If the facts do not contain the answer, reply exactly: CANNOT ANSWER\n"
    "- Do not explain, do not cite, do not hedge."
)

#: What a judge is told. It never learns which system produced the answer.
JUDGE_SYSTEM = (
    "You decide whether a candidate answer matches a reference answer to a "
    "question. You are grading meaning, not wording.\n\n"
    "Rules:\n"
    "- CORRECT if the candidate conveys the same fact as the reference, even "
    "in different words, with extra detail, or less precisely.\n"
    "- INCORRECT if it states something different, contradicts the reference, "
    "or is too vague to count as the same answer.\n"
    "- INCORRECT if the candidate is CANNOT ANSWER.\n"
    "- Reply with exactly one word: CORRECT or INCORRECT."
)

CANNOT = "CANNOT ANSWER"


def render_facts(facts: list[str], limit: int = 30) -> str:
    """The retrieved facts, numbered, in rank order.

    Truncated to `limit` because that is the k the run reported and a judged
    number has to be about the same answer the retrieval number was about.
    """
    kept = [f for f in facts[:limit] if f and f.strip()]
    if not kept:
        return "(the memory system returned nothing)"
    return "\n".join(f"{i}. {f.strip()}" for i, f in enumerate(kept, start=1))


def answer(provider, question: str, facts: list[str], limit: int = 30) -> str:
    prompt = (
        f"Facts:\n{render_facts(facts, limit)}\n\n"
        f"Question: {question}\n\n"
        f"Answer:"
    )
    return complete(provider, ANSWER_SYSTEM, prompt, max_tokens=200).strip()


def verdict(provider, question: str, gold: str, candidate: str) -> bool | None:
    """CORRECT or INCORRECT, or None if the judge said something else.

    None rather than a coerced False. A judge that answered neither word has
    not graded anything, and counting that as wrong would quietly blame the
    system being measured for the judge's failure.
    """
    prompt = (
        f"Question: {question}\n"
        f"Reference answer: {gold}\n"
        f"Candidate answer: {candidate}\n\n"
        f"CORRECT or INCORRECT?"
    )
    said = complete(provider, JUDGE_SYSTEM, prompt, max_tokens=10).strip().upper()
    if said.startswith("CORRECT"):
        return True
    if said.startswith("INCORRECT"):
        return False
    return None


def rows_from(path: str) -> Iterator[dict]:
    """The scored rows a run appended, skipping any without a gold answer.

    A question with no reference answer cannot be judged, and it is the same
    set `answer_scoreable` already excludes from answer_words@k.
    """
    with open(path) as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if not (row.get("gold_answer") or "").strip():
                continue
            if "facts" not in row:
                raise ValueError(
                    f"{path} has rows without retrieved text, so it was written "
                    f"before --results started keeping it. Re-run the benchmark "
                    f"with --results to produce a judgeable file."
                )
            yield row


def judge_run(
    path: str,
    answerer,
    judges: dict,
    *,
    limit: int = 0,
    top_k: int = 30,
    progress: Callable[[str], None] | None = None,
    out_path: str = "",
) -> list[dict]:
    """One answer per question, then every judge's verdict on that answer.

    Appends to `out_path` as it goes, for the same reason the run does: a pass
    over 1,982 questions is thousands of model calls and a crash at question
    1,900 must not throw away the first 1,899.
    """
    done = []
    for n, row in enumerate(rows_from(path), start=1):
        if limit and n > limit:
            break
        question = row["question"]
        gold = row["gold_answer"]
        try:
            said = answer(answerer, question, row["facts"], top_k)
        except ModelUnavailable as exc:
            # Recorded, not raised. One provider hiccup in a run this long
            # should cost one question, and a null is distinguishable from a
            # wrong answer downstream.
            said = ""
            verdicts = dict.fromkeys(judges)
            done.append({
                "question_id": row.get("question_id"), "category": row.get("category"),
                "answer": said, "error": str(exc)[:200], "verdicts": verdicts,
            })
            continue
        verdicts = {}
        for name, provider in judges.items():
            try:
                verdicts[name] = verdict(provider, question, gold, said)
            except ModelUnavailable as exc:
                verdicts[name] = None
                if progress:
                    progress(f"  judge {name} failed on {row.get('question_id')}: {exc}")
        record = {
            "question_id": row.get("question_id"),
            "category": row.get("category"),
            "cannot_answer": said.upper().startswith(CANNOT),
            "answer": said,
            "verdicts": verdicts,
        }
        done.append(record)
        if out_path:
            with open(out_path, "a") as handle:
                handle.write(json.dumps(record) + "\n")
        if progress and n % 50 == 0:
            progress(f"  {n:,} questions answered and judged")
    return done


def accuracy(records: list[dict], judge: str) -> dict:
    """One judge's accuracy, with what it could not grade reported separately."""
    graded = [r for r in records if r["verdicts"].get(judge) is not None]
    correct = sum(1 for r in graded if r["verdicts"][judge])
    cannot = sum(1 for r in records if r.get("cannot_answer"))
    return {
        "judge": judge,
        "n": len(records),
        "graded": len(graded),
        "ungraded": len(records) - len(graded),
        "accuracy": correct / len(graded) if graded else None,
        # Split out because for a memory system "did not retrieve it" and
        # "retrieved and answered wrongly" are different failures.
        "cannot_answer": cannot,
        "cannot_answer_rate": cannot / len(records) if records else None,
    }


def agreement(records: list[dict], a: str, b: str) -> dict:
    """How far the verdict depends on which judge gave it.

    Raw agreement AND Cohen's kappa. Raw agreement alone flatters any pair of
    judges on a task whose answer is usually the same: two judges who both say
    CORRECT 90% of the time agree 82% of the time by chance alone. Kappa is
    what is left after removing that.
    """
    both = [
        r for r in records
        if r["verdicts"].get(a) is not None and r["verdicts"].get(b) is not None
    ]
    if not both:
        return {"n": 0, "raw": None, "kappa": None}
    n = len(both)
    same = sum(1 for r in both if r["verdicts"][a] == r["verdicts"][b])
    raw = same / n
    pa = sum(1 for r in both if r["verdicts"][a]) / n
    pb = sum(1 for r in both if r["verdicts"][b]) / n
    chance = pa * pb + (1 - pa) * (1 - pb)
    kappa = (raw - chance) / (1 - chance) if chance < 1 else None
    return {
        "n": n, "raw": raw, "kappa": kappa,
        "chance": chance,
        f"{a}_says_correct": pa,
        f"{b}_says_correct": pb,
    }
