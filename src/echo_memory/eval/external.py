"""Published benchmarks, run through the real write and query path.

`eval` measures this store against itself, which is useful for A/B and
worthless for comparison: 219 cases generated from one person's own facts is
not a number anybody else can stand beside their own. Hosted memory products
publish LongMemEval and LoCoMo figures. This is the harness that puts Echo
Memory on the same corpora, so a claim about it can be checked by someone who
has never seen this store.

Two datasets, both obtained by the person running it rather than vendored here:

    locomo        ten very long two-person conversations, 5,882 dialogue turns,
                  1,986 questions whose gold answers cite the exact turns that
                  support them.
                  https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json

    longmemeval   500 questions, each with its own haystack of about 50 chat
                  sessions, 246,930 turns in total. The sessions holding the
                  answer are named in `answer_session_ids` and the individual
                  turns carrying it are flagged `has_answer`.
                  https://huggingface.co/datasets/xiaowu0162/longmemeval (file
                  `longmemeval_s`, 278MB)

The path is an argument for two reasons. Neither file is redistributable under
this repository's licence, and both are large enough that vendoring them would
be a hostile thing to do to a clone. `tests/fixtures/` holds a small synthetic
file in each schema so the harness itself can be tested without either
download.

**What is measured, and what is not.** Both benchmarks are published as QA
accuracy: a model reads what memory returned, writes an answer, and a second
model judges it. That is the number people quote. This harness calls no model
anywhere, so it does not produce that number and `accuracy` in its JSON is
null, with the reason beside it. What it produces is retrieval: whether the
turn holding the answer came back at all. Retrieval is a **ceiling on** QA
accuracy - a system that never surfaces the evidence cannot answer the
question - and it is not comparable to a published QA figure. Quoting it as one
would be dishonest in the specific way that is easy to get away with.

`answer_words@k` is the closest this can honestly get to accuracy without a
model, and its definition is doing all the work: the share of the gold answer's
own content words that appear anywhere in the returned facts. It undercounts
every answer that paraphrases the transcript, which in LoCoMo is most of them,
so it is a floor under what a reader could have written and not an accuracy
score. Questions whose gold answer has fewer than two content words are
excluded and counted, because a gold answer of "yes" or "2022" matches
somewhere in ten facts by chance.

**The input is raw dialogue, one fact per turn.** That deliberately skips the
step this product pushes to the calling agent, which is deciding what in a
conversation was worth remembering. So these numbers describe the store with
its extraction step removed, which is this architecture's worst case. See
docs/WRITE-COST.md for why that step lives where it does.

**Point it at a scratch database.** It writes real facts through the real code
path, so whatever it touches is indistinguishable from ordinary memory
afterwards. A full LongMemEval run writes 246,930 facts across 500 scopes and
takes hours; write throughput decays as the database grows, so budget
generously.

**It is safe to rerun.** A scope already holding exactly its expected number of
facts is scored without being rewritten, so a run killed at hour three resumes
rather than starting over.
"""

from __future__ import annotations

import hashlib
import json
import re
import statistics
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

from echo_memory.ingestion.write_episode import write_episode
from echo_memory.retrieval.query_memory import query_memory

KS = (1, 5, 10, 20, 30)

# write_episode refuses a fact longer than MAX_STRING_LEN (4,000 characters) by
# RETURNING {"error": ...} rather than raising, so a caller that does not read
# the return value counts the refusal as a write. LongMemEval has long
# assistant turns and they are exactly the ones most likely to carry an answer,
# so they are split rather than dropped. The prefix ("the assistant said on
# <date>: ") is added after chunking, which is what the headroom is for.
MAX_FACT = 3500


def chunks(text: str, size: int = MAX_FACT) -> list[str]:
    """Split on a paragraph or sentence boundary where there is one nearby, so
    a chunk is still something a reader could act on."""
    if len(text) <= size:
        return [text]
    out = []
    while len(text) > size:
        window = text[:size]
        # The END of the boundary, not its start. Cutting at the index of ". "
        # drops the full stop into the gap between two chunks, which is how the
        # first version of this silently ate a character per split.
        candidates = [
            found + len(marker)
            for marker in ("\n\n", ". ")
            if (found := window.rfind(marker)) != -1
        ]
        cut = max(candidates, default=0)
        if cut < size // 2:
            cut = size
        out.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        out.append(text)
    return out


@dataclass(frozen=True)
class Turn:
    """One dialogue turn, as both a fact to write and a thing to score against.

    `key` is the turn's identity in the benchmark's own terms - LoCoMo's
    `dia_id`, LongMemEval's `session_id#index` - and it is what scoring
    compares a returned fact against, so it rides in the fact's `project`
    field.

    `node` is the entity the fact points at, and it is separate from `key`
    because it has to be unique within the scope and `key` is not: a
    LongMemEval haystack can list the same session id twice, and a long turn
    becomes several facts. Two facts sharing a target would supersede one
    another rather than coexist, because a fact is an edge keyed by (source,
    target, relation_type).

    `when` rides into the fact text on purpose. A sixth of LoCoMo and a third
    of LongMemEval is temporal reasoning, and a store that never recorded when
    something was said cannot answer those at any k.
    """

    key: str
    node: str
    session: str
    speaker: str
    when: str
    text: str

    @property
    def fact(self) -> str:
        return (
            f"{self.speaker} said on {self.when}: {self.text}" if self.when
            else f"{self.speaker} said: {self.text}"
        )


@dataclass(frozen=True)
class Question:
    """One scoreable question. `answer` is "" when the dataset supplies none."""

    question_id: str
    question: str
    category: str
    gold_turns: frozenset[str]
    gold_sessions: frozenset[str]
    answer: str = ""


@dataclass(frozen=True)
class Instance:
    """A haystack and the questions asked of it, which together are one scope.

    One scope per instance rather than one for the whole dataset, because that
    is what the benchmarks describe: a question is asked of its own history,
    not of every other question's history. It also makes a killed run
    resumable, since a finished scope can be recognised by its fact count.
    """

    instance_id: str
    prefix: str
    turns: tuple[Turn, ...]
    questions: tuple[Question, ...]

    @property
    def group_id(self) -> str:
        return f"{self.prefix}:{self.instance_id}"

    @property
    def sessions(self) -> dict[str, str]:
        """Which session each turn key belongs to.

        Built here from the turns rather than re-derived from the key at
        scoring time. The two datasets spell the relationship differently
        (`D1:3` against `session_id#index`) and a second copy of that
        derivation is a second thing that can go quietly wrong.
        """
        return {turn.key: turn.session for turn in self.turns}


def instances_in(path: str, limit: int = 0) -> Iterator[dict]:
    """Yield one top-level JSON object at a time, so only one is ever resident.

    Worth a modest amount and no more, and the measurement is here so nobody
    credits it with more: on longmemeval_s, `json.load` peaks at 0.92GB and
    this peaks at 0.56GB. The first full attempt was killed for memory at
    27,394 of 246,930 turns and this is NOT why. Python RSS during ingest is
    flat at 1.3GB across thousands of turns, so the pressure was elsewhere on
    the machine. What protects a long run is the resume check, not this.
    """
    with open(path) as handle:
        text = handle.read()
    decoder = json.JSONDecoder()
    at = text.index("[") + 1
    yielded = 0
    while True:
        while at < len(text) and text[at] in " \t\r\n,":
            at += 1
        if at >= len(text) or text[at] == "]":
            return
        instance, at = decoder.raw_decode(text, at)
        yield instance
        yielded += 1
        if limit and yielded >= limit:
            return


# The scope prefix each dataset writes under. `lme` rather than
# `longmemeval` so a scratch database half-filled by the standalone script this
# replaced still resumes instead of being ingested a second time under a new
# name.
PREFIXES = {"locomo": "locomo", "longmemeval": "lme"}

# Where the file comes from, so a missing path can say so instead of raising
# FileNotFoundError at whoever typed it. Recorded here rather than only in the
# docs because the CLI is where somebody finds out they do not have the corpus.
DATASET_URLS = {
    "locomo": "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json",
    "longmemeval": (
        "https://huggingface.co/datasets/xiaowu0162/longmemeval/resolve/main/longmemeval_s"
    ),
}


LOCOMO_CATEGORIES = {
    1: "multi hop", 2: "temporal", 3: "open domain",
    4: "single hop", 5: "adversarial",
}


def _locomo_session_keys(conversation: dict) -> list[str]:
    return sorted(
        (k for k in conversation if k.startswith("session_") and not k.endswith("date_time")),
        key=lambda k: int(k.split("_")[1]),
    )


def check_flags(dataset: str, per_type: int) -> None:
    """Reject a combination that cannot mean anything, before anything costs
    time. The CLI calls this before loading the embedding model, which is 6
    seconds, and the loader calls it too so no caller can skip it.

    `per_type` on LoCoMo is rejected rather than ignored: every conversation
    carries questions of all five categories, so there is no per-type prefix to
    take, and a flag that silently did nothing would be read as one that worked.
    """
    if per_type and dataset == "locomo":
        raise ValueError(
            "--per-type is a LongMemEval flag: each LoCoMo conversation already "
            "carries all five question categories, so stratifying by type would "
            "mean dropping questions rather than choosing instances"
        )


def load_locomo(path: str, limit: int = 0, per_type: int = 0) -> Iterator[Instance]:
    """One instance per conversation, one question per `qa` entry."""
    check_flags("locomo", per_type)
    for sample in instances_in(path, limit):
        conversation = sample["conversation"]
        turns: list[Turn] = []
        for key in _locomo_session_keys(conversation):
            when = conversation.get(f"{key}_date_time", "")
            for turn in conversation[key]:
                text = (turn.get("text") or "").strip()
                dia = turn.get("dia_id") or ""
                if not text or not dia:
                    continue
                # "D1:3" is turn 3 of session 1, which is how a returned turn
                # is mapped back to a session for session-level recall.
                turns.append(Turn(
                    key=dia, node=dia, session=dia.split(":")[0],
                    speaker=turn.get("speaker") or "someone", when=when, text=text,
                ))
        questions = []
        for index, question in enumerate(sample.get("qa", [])):
            gold = {str(e) for e in (question.get("evidence") or [])}
            if not gold:
                # Four of LoCoMo's questions cite no turn. A question with no
                # gold cannot be scored either way, and counting it as a miss
                # would quietly deflate every number here.
                continue
            # Adversarial questions carry `adversarial_answer` instead of
            # `answer`: the right response is that the transcript does not say.
            # That is a QA property, not a retrieval one, so the cited turn is
            # still scored and the answer field is left empty.
            answer = question.get("answer")
            questions.append(Question(
                question_id=f"{sample['sample_id']}#{index}",
                question=question["question"],
                category=LOCOMO_CATEGORIES.get(question.get("category"),
                                               str(question.get("category"))),
                gold_turns=frozenset(gold),
                gold_sessions=frozenset(g.split(":")[0] for g in gold),
                answer="" if answer is None else str(answer),
            ))
        yield Instance(
            instance_id=sample["sample_id"], prefix=PREFIXES["locomo"],
            turns=tuple(turns), questions=tuple(questions),
        )


def load_longmemeval(path: str, limit: int = 0, per_type: int = 0) -> Iterator[Instance]:
    """One instance per question, each with its own haystack.

    **Never report a prefix.** The file is ordered by question type: the first
    70 instances are all single-session-user, the easiest category, and the
    last are not. `limit` exists for smoke tests only. `per_type` takes the
    first N of each of the six types, which is what a partial run has to be if
    its numbers are going to mean anything.
    """
    taken: dict[str, int] = {}
    for instance in instances_in(path, 0 if per_type else limit):
        kind = instance.get("question_type") or "unknown"
        if per_type:
            if taken.get(kind, 0) >= per_type:
                continue
            taken[kind] = taken.get(kind, 0) + 1
        dates = instance.get("haystack_dates") or []
        ids = instance["haystack_session_ids"]
        turns: list[Turn] = []
        gold_turns: set[str] = set()
        for position, (session_id, session) in enumerate(
            zip(ids, instance["haystack_sessions"])
        ):
            when = dates[position] if position < len(dates) else ""
            for index, turn in enumerate(session):
                content = (turn.get("content") or "").strip()
                if not content:
                    continue
                key = f"{session_id}#{index}"
                if turn.get("has_answer"):
                    gold_turns.add(key)
                role = turn.get("role") or "speaker"
                for part, piece in enumerate(chunks(content)):
                    turns.append(Turn(
                        key=key, node=f"turn {position}.{index}.{part}",
                        session=str(session_id), speaker=role, when=when, text=piece,
                    ))
        answer = instance.get("answer")
        question = Question(
            question_id=str(instance["question_id"]),
            question=instance["question"],
            category=kind,
            gold_turns=frozenset(gold_turns),
            gold_sessions=frozenset(str(s) for s in instance.get("answer_session_ids") or []),
            answer="" if answer is None else str(answer),
        )
        yield Instance(
            instance_id=str(instance["question_id"]), prefix=PREFIXES["longmemeval"],
            turns=tuple(turns), questions=(question,),
        )


DATASETS: dict[str, Callable[..., Iterator[Instance]]] = {
    "locomo": load_locomo,
    "longmemeval": load_longmemeval,
}

# Everything a benchmark question is likely to contain that says nothing about
# whether the answer came back. Deliberately short: a longer list would start
# removing words that carry the answer ("first", "no").
STOPWORDS = frozenset([
    'a', 'an', 'the', 'and', 'or', 'but', 'if', 'of', 'in', 'on', 'at', 'to', 'for', 'from',
    'by', 'with', 'about', 'as', 'is', 'are', 'was', 'were', 'be', 'been', 'being', 'do',
    'does', 'did', 'have', 'has', 'had', 'will', 'would', 'can', 'could', 'should', 'it',
    'its', 'this', 'that', 'these', 'those', 'he', 'she', 'they', 'them', 'his', 'her',
    'their', 'there', 'here', 'what', 'when', 'where', 'who', 'whom', 'which', 'how', 'why',
])

ANSWER_MIN_WORDS = 2


def content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOPWORDS}


def score_question(question: Question, returned: list[dict], sessions: dict[str, str]) -> dict:
    """One row of results for one question, from what retrieval returned.

    `returned` is query_memory's fact list in rank order. The turn a fact came
    from is read out of its provenance, which is where ingest put it.
    """
    keys = [(f.get("provenance") or {}).get("project") or "" for f in returned]
    text = " ".join(f.get("fact") or "" for f in returned)
    wanted = content_words(question.answer) if question.answer else set()
    first = next((i + 1 for i, key in enumerate(keys) if key in question.gold_turns), None)
    row: dict = {
        "question_id": question.question_id,
        "category": question.category,
        "rr": 1 / first if first else 0.0,
        "answer_scoreable": len(wanted) >= ANSWER_MIN_WORDS,
    }
    for k in KS:
        top = keys[:k]
        found = set(top) & question.gold_turns
        row[f"recall@{k}"] = len(found) / len(question.gold_turns) if question.gold_turns else 0.0
        row[f"hit@{k}"] = 1.0 if found else 0.0
        # Looked up, never re-derived from the key: the datasets spell the
        # turn-to-session relationship differently and one copy of that is
        # enough. A key the map has never heard of matches no gold session,
        # which is the right answer rather than a guess.
        in_sessions = {sessions.get(key, "") for key in top}
        row[f"session@{k}"] = (
            len(in_sessions & question.gold_sessions) / len(question.gold_sessions)
            if question.gold_sessions else 0.0
        )
        if row["answer_scoreable"]:
            words = content_words(" ".join(f.get("fact") or "" for f in returned[:k]))
            row[f"answer_words@{k}"] = len(wanted & words) / len(wanted)
    row["returned"] = len(returned)
    row["chars"] = len(text)
    return row


def already_ingested(conn, group_id: str, expected: int) -> bool:
    """Has this scope been fully written by an earlier attempt?

    A full LongMemEval run is several hours and the first one was killed near
    its end, losing everything. Each instance is a self-contained scope, so a
    rerun can skip the ones that finished. The count has to match exactly: a
    scope interrupted midway is rewritten rather than trusted, because a
    partial haystack would score as a retrieval failure and look like a result.
    """
    row = conn.execute(
        "SELECT count(*) FROM public.fact_embedding WHERE group_id = %s", (group_id,)
    ).fetchone()
    return bool(row) and row[0] == expected


def foreign_facts(conn, prefix: str) -> int:
    """How many facts this database holds outside the benchmark's own scopes.

    The guard exists because the mistake is unrecoverable in practice. A full
    LongMemEval run writes 246,930 facts through the ordinary write path, so
    pointing it at a real store leaves 246,930 pieces of somebody's chat
    history indistinguishable from their memory, and no undo.
    """
    row = conn.execute(
        "SELECT count(*) FROM public.fact_embedding WHERE group_id NOT LIKE %s",
        (f"{prefix}:%",),
    ).fetchone()
    return row[0] if row else 0


class WriteRefused(RuntimeError):
    """A turn the store would not take. Never swallowed: a benchmark that
    quietly drops the turns it finds awkward is measuring a corpus nobody
    has, and in LongMemEval the refused ones are the long turns, which are
    disproportionately the ones carrying an answer."""


def ingest(
    conn,
    instance: Instance,
    embedder,
    agent_id: str = "external-benchmark",
    progress: Callable[[str], None] | None = None,
) -> int:
    """One fact per turn, keyed so scoring can find it again.

    Both entity mentions are asserted as new, and both assertions are load
    bearing. Without the utterance one the turns supersede each other: a fact
    is an edge keyed by (source, target, relation_type), so
    `speaker --said--> session` writes one fact per session, and 419 turns
    became 18. Without the speaker one, two names that embed near each other
    are flagged ambiguous and every fact touching the second is DEFERRED rather
    than written, with nothing raised - Tim and John in LoCoMo's conv-43, which
    cost 344 of 680 turns and looked like a successful ingest. An exact name
    match still overrides the assertion, so a speaker's second turn resolves
    onto the node their first one made.
    """
    written = 0
    started = time.time()
    for turn in instance.turns:
        result = write_episode(
            conn, instance.group_id, turn.session,
            [{"name": turn.speaker, "type": "speaker"},
             {"name": turn.node, "type": "utterance"}],
            [{"source": turn.speaker, "target": turn.node, "relation_type": "said",
              "fact": turn.fact, "confidence": "extracted"}],
            {turn.node: {"resolved_to": "new"}, turn.speaker: {"resolved_to": "new"}},
            embedder,
            project=turn.key,
            agent_id=agent_id,
        )
        if not result.get("edges_created"):
            raise WriteRefused(
                f"write refused for {turn.node} in {instance.group_id}: "
                f"{result.get('error') or result}"
            )
        written += 1
        # Reported during ingest rather than only after it, because a long run
        # is hours of silence otherwise, and the rate is the number that says
        # whether it is still worth waiting for: write throughput decays as the
        # database grows (29ms to 129ms across 1,057 to 24,054 nodes, see
        # docs/BENCHMARKS.md), so a run can slow to a stop without erroring.
        if progress and written % 500 == 0:
            rate = written / max(time.time() - started, 1e-9)
            progress(f"  {instance.group_id}: {written:,} turns written, {rate:.0f}/s")
    return written


@dataclass
class Run:
    """Everything a result needs to be checkable by someone who was not there."""

    dataset: str
    path: str
    top_k: int
    rows: list[dict] = field(default_factory=list)
    instances: int = 0
    turns_written: int = 0
    scopes_resumed: int = 0
    seconds: float = 0.0
    dataset_bytes: int = 0
    dataset_sha256: str = ""


def fingerprint(path: str) -> tuple[int, str]:
    """Size and sha256 of the dataset file.

    A benchmark result that does not say which file it ran against cannot be
    reproduced, and both of these datasets have more than one file and more
    than one revision.
    """
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while block := handle.read(1 << 20):
            digest.update(block)
            size += len(block)
    return size, digest.hexdigest()


def run(
    conn,
    dataset: str,
    path: str,
    embedder,
    *,
    limit: int = 0,
    per_type: int = 0,
    top_k: int = max(KS),
    results_path: str = "",
    progress: Callable[[str], None] | None = None,
) -> Run:
    """Ingest and score one instance at a time.

    Not two passes. Each instance is a self-contained haystack, so nothing is
    lost by finishing with it before reading the next, and two things are
    gained: peak memory is one haystack, and a run that dies at hour three has
    scored everything it ingested instead of nothing.
    """
    load = DATASETS[dataset]
    size, digest = fingerprint(path)
    out = Run(dataset=dataset, path=path, top_k=top_k,
              dataset_bytes=size, dataset_sha256=digest)
    started = time.time()
    for instance in load(path, limit=limit, per_type=per_type):
        if already_ingested(conn, instance.group_id, len(instance.turns)):
            out.scopes_resumed += 1
        else:
            out.turns_written += ingest(conn, instance, embedder, progress=progress)
        sessions = instance.sessions
        for question in instance.questions:
            found = query_memory(conn, instance.group_id, question.question, top_k, embedder)
            row = score_question(question, found.get("facts", []), sessions)
            out.rows.append(row)
            if results_path:
                # Appended as it happens, so a killed run keeps what it scored.
                with open(results_path, "a") as handle:
                    handle.write(json.dumps(row) + "\n")
        out.instances += 1
        if progress:
            progress(
                f"  {out.instances} scopes, {len(out.rows):,} questions scored, "
                f"{out.turns_written:,} turns written, {out.scopes_resumed} already present"
            )
    out.seconds = time.time() - started
    return out


ACCURACY_UNAVAILABLE = (
    "both benchmarks define accuracy as model-judged QA over what memory "
    "returned; this harness calls no model, so it reports retrieval only. "
    "Retrieval is a ceiling on accuracy, not a substitute for it."
)


def means(rows: list[dict]) -> dict:
    """Every metric averaged over the rows that could carry it.

    `answer_words@k` averages over the scoreable subset only, and the subset
    size is reported beside it: a question whose gold answer is "yes" has
    fewer than two content words and would match by chance.
    """
    if not rows:
        return {}
    scoreable = [r for r in rows if r["answer_scoreable"]]
    out: dict = {"n": len(rows), "MRR": statistics.mean(r["rr"] for r in rows)}
    for k in KS:
        for metric in ("recall", "hit", "session"):
            out[f"{metric}@{k}"] = statistics.mean(r[f"{metric}@{k}"] for r in rows)
    out["answer_scoreable"] = len(scoreable)
    if scoreable:
        for k in KS:
            out[f"answer_words@{k}"] = statistics.mean(r[f"answer_words@{k}"] for r in scoreable)
    out["mean_facts_returned"] = statistics.mean(r["returned"] for r in rows)
    # What the answer cost, beside whether it was right. A configuration that
    # finds everything by returning everything is not better, and this is the
    # number that says so; `eval` reports the same thing as tokens.
    out["mean_chars_returned"] = statistics.mean(r["chars"] for r in rows)
    return out


def report(out: Run) -> dict:
    """The machine-readable result. Stable keys, so two runs can be diffed."""
    categories = sorted({r["category"] for r in out.rows if r["category"]})
    return {
        "dataset": out.dataset,
        "dataset_path": out.path,
        "dataset_bytes": out.dataset_bytes,
        "dataset_sha256": out.dataset_sha256,
        "generated": datetime.now(UTC).isoformat(timespec="seconds"),
        "top_k": out.top_k,
        "ks": list(KS),
        "instances": out.instances,
        "turns_written": out.turns_written,
        "scopes_resumed": out.scopes_resumed,
        "seconds": round(out.seconds, 1),
        "measures": "retrieval",
        "accuracy": None,
        "accuracy_unavailable_because": ACCURACY_UNAVAILABLE,
        "overall": means(out.rows),
        "by_category": {
            c: means([r for r in out.rows if r["category"] == c]) for c in categories
        },
    }


def render(out: Run) -> str:
    """The readable summary. Same numbers as the JSON, never recomputed
    differently: both read `means`."""
    if not out.rows:
        return "no questions scored: check the dataset path and that it holds gold evidence\n"

    def line(label: str, stats: dict) -> str:
        return (
            f"  {label:<26}{stats['n']:>7,}"
            f"{stats['recall@1']:>11.3f}{stats['recall@10']:>11.3f}{stats['recall@30']:>11.3f}"
            f"{stats['hit@10']:>9.3f}{stats['session@10']:>12.3f}{stats['MRR']:>8.3f}"
        )

    overall = means(out.rows)
    head = (
        f"  {'category':<26}{'n':>7}{'recall@1':>11}{'recall@10':>11}"
        f"{'recall@30':>11}{'hit@10':>9}{'session@10':>12}{'MRR':>8}"
    )
    scopes = f"{out.instances:,} scope" + ("" if out.instances == 1 else "s")
    lines = [
        "",
        f"{out.dataset} via {out.path}",
        (f"{scopes}, {out.turns_written:,} turns written, "
         f"{out.scopes_resumed} already present, {out.seconds:.0f}s"),
        "",
        head,
        "  " + "-" * (len(head) - 2),
        line("overall", overall),
    ]
    by_category = {
        c: [r for r in out.rows if r["category"] == c]
        for c in sorted({r["category"] for r in out.rows if r["category"]})
    }
    for category, rows in by_category.items():
        lines.append(line(category, means(rows)))

    lines += [
        "",
        "recall@k is the share of a question's cited turns returned in the top k;",
        "session@k the share of its answer-bearing sessions represented there.",
        "",
        "This is retrieval, not QA accuracy. Both benchmarks publish model-judged",
        "accuracy and no model is called here, so these numbers are a CEILING on",
        "what any reader could answer from what came back, and are not comparable",
        "to a published LoCoMo or LongMemEval accuracy figure.",
    ]
    if overall.get("answer_scoreable"):
        lines += [
            "",
            (f"answer_words@10 is {overall['answer_words@10']:.3f}, over the "
             f"{overall['answer_scoreable']:,} of {overall['n']:,} questions"),
            "whose gold answer has two or more content words: the share of those words that",
            "appear in the top 10. A floor under what a reader could have written, not an",
            "accuracy score, and it undercounts every answer that paraphrases the transcript.",
            "The excluded questions supply no gold answer, or one that ten facts would match",
            'by chance ("yes", "2022").',
        ]
    return "\n".join(lines) + "\n"
