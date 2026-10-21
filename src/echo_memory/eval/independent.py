"""An evaluation whose questions were not written from its answers.

`eval/retrieval.py` builds a query out of the fact it wants back. That makes
labels free, and it is why every number it produces describes the generated
task rather than retrieval. Two review rounds established the stronger version
of that limitation: a biased task can change WHICH CONFIGURATION WINS, not only
the level of both, so the harness cannot settle what should ship. The recorded
sign reversal - dropping the lexical channel measured +0.112 MRR under one
query shape and -0.082 under another - is an instance.

This is the other half. The independence that matters is from the gold-answer
construction and from the tuning, and it is enforced by order and by shape:

  1. A question is written and stored BEFORE anything is retrieved for it. It
     cannot have been fitted to a result that did not exist yet.
  2. The retriever is frozen at a commit, recorded on the question and again on
     every run. A question and a score from different code are two
     measurements.
  3. Every configuration runs, and their results are POOLED and shuffled. The
     judge sees a question and a list of facts, never a configuration.
  4. Relevance is judged for a (question, fact) pair. One label serves every
     configuration, and any number of facts may be relevant - which is what
     the old harness could not express, since it marked exactly one fact
     correct for a name that may appear in thirty.

**Pooling bias is not solved by any of this.** A fact no configuration
returned is never judged, so recall here is recall over the pooled relevant
set, never over the store. Reported as such, everywhere it appears.

The author may write the questions. Independence from the author is not what
was ever at stake - a person who knows the subject matter is the only person
who can ask a real question about it - and demanding it would make the
evaluation impossible rather than sound. What must not happen is a question
shaped by an answer, a score compared across code versions, or a judge who
knows which system produced a candidate. Those are the three this prevents.
"""

from __future__ import annotations

import random
import subprocess

# Judging order is shuffled so position carries no information about which
# configuration found a fact. Seeded so a judging session can be reproduced
# and audited.
SHUFFLE_SEED = 20260913


def retriever_sha() -> str | None:
    """The commit the retriever is frozen at, or None outside a checkout.

    Recorded on the question and again on every run. The paper has already
    published one pair of numbers from two different code versions by
    accident; this is the field that makes that visible rather than silent.
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = out.stdout.strip()
    return sha or None


def add_question(conn, group_id: str, text: str, *, subject: str | None = None,
                 author: str | None = None) -> dict:
    """Record a question. Nothing is retrieved here, deliberately.

    Returns {"id": int, "created": bool}; a repeat of the same text in the same
    scope is a no-op rather than a second question, because two identical
    questions would be judged twice and counted twice.
    """
    text = " ".join((text or "").split())
    if not text:
        raise ValueError("a question needs text")

    row = conn.execute(
        """INSERT INTO public.eval_question (group_id, text, subject, retriever_sha, author)
           VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (group_id, text) DO NOTHING
           RETURNING id""",
        (group_id, text, subject, retriever_sha(), author),
    ).fetchone()
    if row is not None:
        return {"id": row[0], "created": True}
    existing = conn.execute(
        "SELECT id FROM public.eval_question WHERE group_id = %s AND text = %s",
        (group_id, text),
    ).fetchone()
    return {"id": existing[0], "created": False}


def questions(conn, group_id: str, *, unopened_only: bool = False) -> list[dict]:
    sql = """SELECT id, text, subject, retriever_sha, opened_at
             FROM public.eval_question WHERE group_id = %s"""
    if unopened_only:
        sql += " AND opened_at IS NULL"
    sql += " ORDER BY id"
    return [
        {"id": i, "text": t, "subject": s, "retriever_sha": sha, "opened_at": o}
        for i, t, s, sha, o in conn.execute(sql, (group_id,)).fetchall()
    ]


def run_configurations(conn, group_id: str, question: dict, embedder,
                       configurations: dict[str, dict]) -> dict[str, list[str]]:
    """Retrieve for one question under every configuration, and store each.

    Called when judging opens, never before. `opened_at` is stamped here, so a
    question that has been retrieved for can be told from one that has not -
    and a question whose text changed after retrieval would be a different
    question, which the unique index already prevents.
    """
    from echo_memory.retrieval.query_memory import DEFAULT_TOP_K, query_memory

    sha = retriever_sha()
    out: dict[str, list[str]] = {}
    for name, kwargs in configurations.items():
        result = query_memory(
            conn, group_id, question["text"], DEFAULT_TOP_K, embedder, **kwargs
        )
        ids = [str(f["fact_id"]) for f in (result.get("facts") or [])]
        out[name] = ids
        conn.execute(
            """INSERT INTO public.eval_run (question_id, configuration, returned, retriever_sha)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (question_id, configuration)
               DO UPDATE SET returned = EXCLUDED.returned,
                             retriever_sha = EXCLUDED.retriever_sha,
                             ran_at = now()""",
            (question["id"], name, ids, sha),
        )
    conn.execute(
        "UPDATE public.eval_question SET opened_at = coalesce(opened_at, now()) WHERE id = %s",
        (question["id"],),
    )
    return out


def pool(conn, question_id: int, *, seed: int = SHUFFLE_SEED,
         judged_by: str | None = None) -> list[str]:
    """Every fact any configuration returned for this question, shuffled.

    The union, not a concatenation: a fact several configurations found is one
    thing to judge, and judging it twice would weight it twice. Shuffled with a
    fixed seed so position says nothing about which configuration ranked it
    first, and so the same pool can be presented twice for an agreement check.

    What `judged_by` already labelled is excluded, which makes one judge's pass
    resumable without hiding the pool from the next judge. Excluding whatever
    ANYBODY had labelled was the earlier behaviour, and it made the second
    judging pass the docstring promises impossible: a second judge asking for
    the pool got an empty one, because the first judge had been through it.
    """
    rows = conn.execute(
        "SELECT returned FROM public.eval_run WHERE question_id = %s ORDER BY configuration",
        (question_id,),
    ).fetchall()
    seen: list[str] = []
    for (returned,) in rows:
        for edge_id in returned or []:
            if edge_id not in seen:
                seen.append(edge_id)

    judged = {
        r[0] for r in conn.execute(
            "SELECT edge_id FROM public.eval_judgement "
            "WHERE question_id = %s AND (%s::text IS NULL OR judged_by = %s::text)",
            (question_id, judged_by, judged_by),
        ).fetchall()
    }
    unjudged = [e for e in seen if e not in judged]
    random.Random(seed + question_id).shuffle(unjudged)
    return unjudged


def judge(conn, question_id: int, edge_id: str, relevant: bool, *,
          note: str | None = None, judged_by: str | None = None) -> None:
    """Record one relevance label for a (question, fact) pair.

    Not a (question, configuration) pair. A fact that answers a question
    answers it whoever surfaced it, so one label serves every configuration and
    the judge never has to be told - or be able to work out - which one did.
    """
    conn.execute(
        """INSERT INTO public.eval_judgement (question_id, edge_id, relevant, note, judged_by)
           VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (question_id, edge_id, judged_by)
           DO UPDATE SET relevant = EXCLUDED.relevant, note = EXCLUDED.note,
                         judged_at = now()""",
        (question_id, edge_id, relevant, note, judged_by or "unknown"),
    )


def coverage(conn, group_id: str, *, judged_by: str | None = None) -> dict:
    """How much of the (question, fact) grid ONE judge has actually covered.

    Pooling bias is a property of how much was left unjudged, so it is a
    measurement rather than a disclaimer. At TREC scale the grid is
    unjudgeable and the caveat is permanent; this store holds a few hundred
    facts, which makes the whole grid reachable and the bias closable.

    Counted per judge for the same reason it is closable at all: the claim is
    that no relevant fact is hiding outside what was labelled, and two judges'
    rows added together cannot support it. Summed across judges this returned
    2942 of 2850 - more than the grid holds - and reported the bias closed on
    the strength of it.
    """
    import json as _json

    from echo_memory.infra.db import GRAPH_NAME as GRAPH

    active = conn.execute(
        f"""SELECT count(*) FROM cypher('{GRAPH}', $$
            MATCH ()-[e:FACT]->() WHERE e.group_id = $g AND e.t_invalid IS NULL
            RETURN id(e) $$, %s) AS (i agtype)""",
        (_json.dumps({"g": group_id}),),
    ).fetchone()[0]
    n_questions = conn.execute(
        "SELECT count(*) FROM public.eval_question WHERE group_id = %s AND opened_at IS NOT NULL",
        (group_id,),
    ).fetchone()[0]
    who = resolve_judge(conn, group_id, judged_by)
    judged = conn.execute(
        """SELECT count(*) FROM public.eval_judgement j
           JOIN public.eval_question q ON q.id = j.question_id
           WHERE q.group_id = %s AND j.judged_by = %s""",
        (group_id, who),
    ).fetchone()[0]
    possible = active * n_questions
    return {
        "active_facts": active,
        "questions": n_questions,
        "judged": judged,
        "possible": possible,
        "judged_by": who,
        # A judge cannot label more pairs than the grid has, so >= is only ever
        # == here; kept as >= so a store that shrank mid-pass still reads as
        # covered rather than silently reopening the caveat.
        "complete": possible > 0 and judged >= possible,
    }


def score(conn, group_id: str, *, judged_by: str | None = None) -> dict:
    """Per-configuration metrics over one judge's labels.

    precision@k  of the first k a configuration returned, how many were judged
                 relevant. The metric the old harness could not express,
                 because it marked exactly one fact correct.
    recall@k     over the POOLED relevant set for that question, never over the
                 store: a fact nothing returned was never judged.
    MRR          reciprocal rank of the first relevant fact.

    Questions with no relevant fact in the pool are excluded from recall and
    MRR and counted separately - they say something about the store, not about
    a configuration's ranking.

    `judged_by` names whose labels these are, and the result carries the name
    back so a number can never be quoted without it. Run it once per judge and
    compare; that difference is a result, and on this data a larger one than
    the difference between the configurations being measured.
    """
    who = resolve_judge(conn, group_id, judged_by)
    labels: dict[int, dict[str, bool]] = {}
    for qid, edge_id, relevant in conn.execute(
        """SELECT j.question_id, j.edge_id, j.relevant
           FROM public.eval_judgement j
           JOIN public.eval_question q ON q.id = j.question_id
           WHERE q.group_id = %s AND j.judged_by = %s""",
        (group_id, who),
    ).fetchall():
        labels.setdefault(qid, {})[edge_id] = relevant

    runs: dict[str, dict[int, list[str]]] = {}
    for qid, configuration, returned in conn.execute(
        """SELECT r.question_id, r.configuration, r.returned
           FROM public.eval_run r
           JOIN public.eval_question q ON q.id = r.question_id
           WHERE q.group_id = %s""",
        (group_id,),
    ).fetchall():
        runs.setdefault(configuration, {})[qid] = returned or []

    out: dict[str, dict] = {}
    for configuration, by_question in sorted(runs.items()):
        scored = 0
        no_relevant = 0
        p_at = {1: 0.0, 3: 0.0, 5: 0.0}
        r_at = {1: 0.0, 3: 0.0, 5: 0.0}
        rr_total = 0.0
        for qid, returned in by_question.items():
            judged = labels.get(qid) or {}
            if not judged:
                continue
            relevant_pool = {e for e, ok in judged.items() if ok}
            if not relevant_pool:
                no_relevant += 1
                continue
            scored += 1
            for k in p_at:
                top = returned[:k]
                hits = sum(1 for e in top if judged.get(e))
                p_at[k] += hits / k
                r_at[k] += hits / len(relevant_pool)
            rank = next(
                (i for i, e in enumerate(returned, start=1) if judged.get(e)), None
            )
            rr_total += (1.0 / rank) if rank else 0.0
        out[configuration] = {
            "questions": scored,
            "questions_with_no_relevant_fact": no_relevant,
            "precision_at": {k: (v / scored if scored else 0.0) for k, v in p_at.items()},
            "recall_at": {k: (v / scored if scored else 0.0) for k, v in r_at.items()},
            "mrr": rr_total / scored if scored else 0.0,
            "judged_by": who,
        }
    return out


def per_question(conn, group_id: str, *, judged_by: str | None = None) -> list[dict]:
    """Each question's reciprocal rank under each configuration, for one judge.

    An aggregate over ten questions hides whether an advantage is consistent or
    carried by two cases, and at this sample size that difference is the whole
    question. A reviewer asked for these before the aggregate could be read as
    anything, which is right.

    The same grid under a second judge is what shows whether an advantage
    survives a change of labeller, which is the question a single grid cannot
    answer however finely it is broken down.
    """
    who = resolve_judge(conn, group_id, judged_by)
    labels: dict[int, set[str]] = {}
    for qid, edge_id in conn.execute(
        """SELECT j.question_id, j.edge_id FROM public.eval_judgement j
           JOIN public.eval_question q ON q.id = j.question_id
           WHERE q.group_id = %s AND j.relevant AND j.judged_by = %s""",
        (group_id, who),
    ).fetchall():
        labels.setdefault(qid, set()).add(edge_id)

    text = {
        i: t for i, t in conn.execute(
            "SELECT id, text FROM public.eval_question WHERE group_id = %s ORDER BY id",
            (group_id,),
        ).fetchall()
    }
    runs: dict[int, dict[str, list[str]]] = {}
    for qid, configuration, returned in conn.execute(
        """SELECT r.question_id, r.configuration, r.returned FROM public.eval_run r
           JOIN public.eval_question q ON q.id = r.question_id
           WHERE q.group_id = %s""",
        (group_id,),
    ).fetchall():
        runs.setdefault(qid, {})[configuration] = returned or []

    out = []
    for qid, question in text.items():
        relevant = labels.get(qid, set())
        row = {"id": qid, "text": question, "relevant": len(relevant),
               "judged_by": who, "rr": {}}
        for configuration, returned in sorted(runs.get(qid, {}).items()):
            rank = next(
                (i for i, e in enumerate(returned, start=1) if e in relevant), None
            )
            row["rr"][configuration] = (1.0 / rank) if rank else 0.0
        out.append(row)
    return out


# Resamples for the bootstrap interval, matching eval/retrieval.py so the two
# evaluations' intervals mean the same thing.
BOOTSTRAP_RESAMPLES = 5000


def compare_configurations(conn, group_id: str, a: str, b: str, *,
                           judged_by: str | None = None,
                           resamples: int = BOOTSTRAP_RESAMPLES,
                           seed: int = 7) -> dict:
    """Delta MRR between two configurations, with a paired bootstrap interval.

    Paired because both configurations answered the same questions: the
    variance that matters is that of the per-question difference. Bootstrap
    rather than a t-test because reciprocal ranks are 1, 1/2, 1/3 ... 0 with a
    heavy spike at 0, so resampling makes no distributional claim the data
    would violate.

    Ten questions is a very small n, and the interval says so rather than
    hiding it. That is the point of computing it here: two judges over this
    pool put `shipping` and `lexical only` in opposite orders on MRR, and
    without an interval that reads as a finding about the configurations. With
    one it reads as what it is - a sample too small to separate them, where
    which judge labelled decides which one happens to lead.
    """
    rows = per_question(conn, group_id, judged_by=judged_by)
    diffs = [
        r["rr"][b] - r["rr"][a]
        for r in rows if a in r["rr"] and b in r["rr"] and r["relevant"]
    ]
    if not diffs:
        return {"questions": 0, "delta": 0.0, "low": 0.0, "high": 0.0,
                "significant": False, "judged_by": judged_by}

    rng = random.Random(seed)
    n = len(diffs)
    means = sorted(
        sum(diffs[rng.randrange(n)] for _ in range(n)) / n for _ in range(resamples)
    )
    low, high = means[int(0.025 * resamples)], means[int(0.975 * resamples)]
    return {
        "questions": n,
        "a": a, "b": b,
        "delta": sum(diffs) / n,
        "low": low,
        "high": high,
        # Excludes zero at 95%. Not "true", only "not obviously nothing".
        "significant": low > 0 or high < 0,
        "judged_by": rows[0].get("judged_by"),
    }


def render_per_question(rows: list[dict]) -> str:
    if not rows:
        return ""
    names = sorted({c for r in rows for c in r["rr"]})
    who = rows[0].get("judged_by")
    lines = [
        "",
        "Per question, reciprocal rank of the first relevant fact"
        + (f", as judged by {who}:" if who else ":"),
        "",
        "  " + f"{'#':<3}{'rel':>4}  " + "".join(f"{n[:12]:>14}" for n in names),
        "  " + "-" * (9 + 14 * len(names)),
    ]
    for r in rows:
        lines.append(
            f"  {r['id']:<3}{r['relevant']:>4}  "
            + "".join(f"{r['rr'].get(n, 0.0):>14.3f}" for n in names)
        )
    lines.append("")
    for r in rows:
        lines.append(f"  {r['id']:<3} {r['text']}")
    return "\n".join(lines)


def render(scores: dict, cover: dict | None = None) -> str:
    if not scores:
        return (
            "no judged questions yet.\n"
            "  echo-memory judge new \"<question>\"   records one, before anything is retrieved\n"
            "  echo-memory judge open                retrieves under every configuration\n"
            "  echo-memory judge pool                presents the shuffled union for labelling\n"
        )
    who = next(iter(scores.values())).get("judged_by")
    lines = [
        "",
        "Independent evaluation - questions written before retrieval, judged per fact",
        # Whose labels these are belongs in the header, not a footnote. Two
        # judges over this pool differ by more than the configurations do.
        f"Relevance as judged by {who}." if who else "",
        "",
        f"  {'configuration':<22}{'n':>5}{'P@1':>8}{'P@3':>8}{'R@3':>8}{'R@5':>8}{'MRR':>8}",
        "  " + "-" * 67,
    ]
    for name, s in scores.items():
        lines.append(
            f"  {name:<22}{s['questions']:>5}"
            f"{s['precision_at'][1]:>8.3f}{s['precision_at'][3]:>8.3f}"
            f"{s['recall_at'][3]:>8.3f}{s['recall_at'][5]:>8.3f}{s['mrr']:>8.3f}"
        )
    any_score = next(iter(scores.values()))
    if any_score["questions_with_no_relevant_fact"]:
        lines.append("")
        lines.append(
            f"  {any_score['questions_with_no_relevant_fact']} question(s) had no relevant "
            "fact anywhere in the pool and are excluded."
        )
    lines.append("")
    if cover and cover["complete"]:
        lines += [
            (f"Every ({cover['questions']} question x {cover['active_facts']} fact) pair "
             f"is judged by {cover['judged_by']} - {cover['judged']} of {cover['possible']}."),
            "  Recall is therefore recall over the store, not over a pool. Nothing relevant",
            "  can be hiding in what no configuration returned, because there is no such",
            "  thing left unjudged.",
        ]
    elif cover:
        missing = cover["possible"] - cover["judged"]
        lines += [
            ("Recall is over the POOLED relevant set: "
             + (f"{cover['judged_by']} left " if cover["judged_by"] else "")
             + f"{missing} of {cover['possible']} (question, fact) pairs unjudged,"),
            "  so a relevant fact no configuration returned cannot count against",
            "  anything. Precision is unaffected and is the number to read first.",
        ]
    else:
        lines += [
            "Recall is over the POOLED relevant set, not the store: a fact no configuration",
            "  returned was never judged, so it cannot count against anything.",
        ]
    lines.append("")
    return "\n".join(lines)


# Characters of a fact shown when judging. Facts here run to 900; a judge
# deciding "does this help answer the question" needs the subject, not the
# whole record, and 119 full-length facts in a terminal is how a judging pass
# gets abandoned or rushed.
EXCERPT = 240


def export_pool(conn, group_id: str, *, only: int | None = None,
                judged_by: str | None = None) -> str:
    """The whole judging pass as one editable file.

    Interactive prompting was the first design and it was the wrong surface:
    it needs a TTY, it cannot be paused and resumed inside a session, it shows
    one fact with no sense of how many remain, and it puts a judge under
    exactly the time pressure that produces labels nobody should build on. A
    file can be worked through in an editor, left half-done, diffed, reviewed
    by somebody else, and committed next to the result it produced.

    Still blind: facts appear in the pooled shuffled order with no
    configuration named and no rank shown.
    """
    import textwrap

    from echo_memory.retrieval.query_memory import _fetch_facts

    lines = [
        "# Relevance judging. Put y or n between the brackets; leave blank to skip.",
        "#",
        "#   y  this fact would help someone answer the question",
        "#   n  it would not - sharing a word with the question is not enough",
        "#",
        "# Several facts may be y for one question. If nothing here answers it,",
        "# mark them all n: that is a finding about the store, not a failed judgement.",
        "#",
        "# Facts are in pooled, shuffled order. Which configuration returned any of",
        "# them, and at what rank, is deliberately not shown.",
        "",
    ]
    total = 0
    for q in questions(conn, group_id):
        if not q["opened_at"] or (only is not None and q["id"] != only):
            continue
        ids = pool(conn, q["id"], judged_by=judged_by)
        if not ids:
            continue
        facts = _fetch_facts(conn, ids)
        lines += ["", "=" * 78, f"Q{q['id']}: {q['text']}", f"{len(ids)} fact(s)", ""]
        for edge_id in ids:
            fact = facts.get(edge_id)
            if not fact:
                continue
            text = (fact["fact"] or "").strip()
            if len(text) > EXCERPT:
                text = text[:EXCERPT].rsplit(" ", 1)[0] + " ..."
            wrapped = textwrap.wrap(text, width=70) or [""]
            lines.append(f"[ ] {edge_id}  {wrapped[0]}")
            lines += [f"    {' ' * len(edge_id)}  {w}" for w in wrapped[1:]]
            lines.append("")
            total += 1
    lines.append(f"# {total} fact(s) to judge.")
    return "\n".join(lines) + "\n"


def import_pool(conn, text: str, *, judged_by: str | None = None) -> dict:
    """Read a marked file back. Unmarked lines are left unjudged, not guessed."""
    import re

    pattern = re.compile(r"^\[\s*([yYnN]?)\s*\]\s+(\d+)\b")
    question_id: int | None = None
    counts = {"y": 0, "n": 0, "skipped": 0}
    for line in text.splitlines():
        heading = re.match(r"^Q(\d+):", line.strip())
        if heading:
            question_id = int(heading.group(1))
            continue
        match = pattern.match(line.strip())
        if not match or question_id is None:
            continue
        verdict, edge_id = match.group(1).lower(), match.group(2)
        if not verdict:
            counts["skipped"] += 1
            continue
        judge(conn, question_id, edge_id, verdict == "y", judged_by=judged_by)
        counts[verdict] += 1
    return counts


def resolve_judge(conn, group_id: str, judged_by: str | None = None) -> str | None:
    """Which judge's labels a score is computed from. Never "all of them".

    Two judges over the same pool are two measurements, and averaging them is
    not a third: the pairs they both labelled would count twice and the ones
    only one labelled would count once, weighting a fact by how many people
    happened to look at it. Blending was what the code did before the primary
    key carried the judge, and it silently reported 2942 of 2850 pairs judged -
    an impossible fraction that read as "complete".

    So one judge is named whenever there is a choice to make. With a single
    judge that is automatic; with more than one, refusing is the only safe
    answer, because there is no default that is not a hidden editorial choice
    about whose labels count.

    With none, the answer is None rather than an error: "nobody has judged this
    yet" is a true and useful state, reported as zero coverage and empty
    scores, and it is what every caller sees before the first judging pass.
    """
    known = [who for who, _, _ in judges(conn, group_id)]
    if judged_by is not None:
        if judged_by not in known:
            raise ValueError(
                f"no labels from {judged_by!r} in this scope"
                + (f"; judges here are {', '.join(known)}" if known else "")
            )
        return judged_by
    if not known:
        return None
    if len(known) > 1:
        raise ValueError(
            f"{len(known)} judges have labelled this scope ({', '.join(known)}); "
            "name one - their labels are separate measurements, not a pool"
        )
    return known[0]


def judges(conn, group_id: str) -> list[tuple[str, int, int]]:
    """Who has labelled this scope, and how much each of them said yes to."""
    return [
        (who, total, yes)
        for who, total, yes in conn.execute(
            """SELECT j.judged_by, count(*), count(*) FILTER (WHERE j.relevant)
               FROM public.eval_judgement j
               JOIN public.eval_question q ON q.id = j.question_id
               WHERE q.group_id = %s GROUP BY 1 ORDER BY 1""",
            (group_id,),
        ).fetchall()
    ]


def agreement(conn, group_id: str, a: str, b: str) -> dict:
    """How far two judges agree on the pairs they both labelled.

    Raw agreement flatters badly here: almost every pair is irrelevant, so two
    judges who both say no to everything agree 99% of the time and have told
    you nothing. Cohen's kappa removes the agreement you would expect from the
    marginals alone, which is the number worth reporting when the positive
    class is 1% of the data.

    Reported alongside the counts rather than instead of them, because kappa is
    unstable when one class is this rare and a reader needs to see why.
    """
    rows = conn.execute(
        """SELECT x.relevant, y.relevant
           FROM public.eval_judgement x
           JOIN public.eval_judgement y
             ON y.question_id = x.question_id AND y.edge_id = x.edge_id
           JOIN public.eval_question q ON q.id = x.question_id
           WHERE q.group_id = %s AND x.judged_by = %s AND y.judged_by = %s""",
        (group_id, a, b),
    ).fetchall()
    if not rows:
        return {"pairs": 0}

    n = len(rows)
    both_yes = sum(1 for p, q in rows if p and q)
    both_no = sum(1 for p, q in rows if not p and not q)
    a_only = sum(1 for p, q in rows if p and not q)
    b_only = sum(1 for p, q in rows if not p and q)

    observed = (both_yes + both_no) / n
    p_a, p_b = (both_yes + a_only) / n, (both_yes + b_only) / n
    expected = p_a * p_b + (1 - p_a) * (1 - p_b)
    kappa = (observed - expected) / (1 - expected) if expected < 1 else 1.0
    return {
        "pairs": n, "both_relevant": both_yes, "both_not": both_no,
        f"{a}_only": a_only, f"{b}_only": b_only,
        "raw_agreement": observed, "kappa": kappa,
    }
