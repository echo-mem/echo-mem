# Benchmarks

Every number here was taken on a stated date with the command that reproduces
it. Where a number is unflattering it is still here, because a benchmark you
only publish when it wins is marketing.

## What is measured, and what is not

LoCoMo and LongMemEval are published as **QA accuracy**: a model reads what
memory returned, writes an answer, and a second model judges it against a gold
answer. Those are the numbers people quote, including the state of the art
claims made by hosted memory products.

Echo Memory's harness measures **retrieval**: whether the turn holding the
answer came back at all. No model is called anywhere in it, and the machine
readable report it writes says so in the field where accuracy would go.

Retrieval is a **ceiling on** QA accuracy, not a substitute for it. A system
that never surfaces the evidence cannot answer the question, so a low retrieval
number is decisive and a high one is necessary rather than sufficient. These
results are not comparable to a published QA accuracy figure and should never
be quoted as though they were.

There is a second, sharper caveat specific to this design. The harness feeds
**raw, unfiltered dialogue turns**, one fact per turn. That deliberately skips
the step Echo Memory pushes to the calling agent, which is deciding what in a
conversation was worth remembering at all. So these numbers describe the store
with its extraction step removed, which is this architecture's worst case. See
[WRITE-COST.md](WRITE-COST.md) for why that step lives where it does.

## LoCoMo, 2026-09-17

Ten conversations, 5,882 dialogue turns, 1,982 questions whose gold answers
cite the exact turns supporting them.

```bash
curl -sLO https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json
ECHO_MEMORY_DATABASE_URL=postgresql://.../locomo_bench \
    echo-memory eval-external locomo locomo10.json
```

| Category | n | recall@1 | recall@10 | recall@30 | hit@10 | MRR |
|---|---:|---:|---:|---:|---:|---:|
| **overall** | **1,982** | **0.324** | **0.601** | **0.708** | **0.658** | **0.460** |
| temporal | 321 | 0.439 | 0.691 | 0.794 | 0.717 | 0.556 |
| single hop | 841 | 0.383 | 0.680 | 0.773 | 0.697 | 0.498 |
| adversarial | 446 | 0.308 | 0.584 | 0.697 | 0.594 | 0.401 |
| multi hop | 282 | 0.099 | 0.386 | 0.530 | 0.649 | 0.391 |
| open domain | 92 | 0.139 | 0.310 | 0.415 | 0.435 | 0.263 |

`recall@k` is the share of a question's cited turns returned in the top k.

**Multi hop is the worst row and it is the expected one.** recall@1 of 0.099
says that when an answer needs two turns joined, ranking the single best fact
is nearly useless. This is the case v1b's multi hop retrieval exists for, and
the number to beat now exists before the feature does, which is the same
posture as the 187 question MRR 0.212 figure in the README.

**Open domain is low and mostly should be.** Those questions need world
knowledge that is not in the transcript, so no retrieval over the transcript
can supply it. It is reported rather than excluded because excluding a category
because it is hard is how benchmark tables become useless.

**Temporal being the strongest row is a consequence of a design decision, not a
surprise.** Each fact records the date the turn was spoken, in the fact text, so
"when did she say that" has something to match against.

## LongMemEval S, 2026-09-18, stratified sample of 90

500 questions, each with its own haystack of roughly 50 chat sessions, 246,930
turns in total. What was run is **15 questions of each of the six types, 90 in
all, 33,261 turns**, and that is what these numbers describe.

```bash
curl -sLo longmemeval_s.json \
  https://huggingface.co/datasets/xiaowu0162/longmemeval/resolve/main/longmemeval_s
ECHO_MEMORY_DATABASE_URL=postgresql://.../lme_bench \
    echo-memory eval-external longmemeval longmemeval_s.json --per-type 15
```

Not a prefix, and the distinction is not pedantry. The file is ordered by
question type: the first 70 instances are all single-session-user, which is one
of the easiest categories. A `--limit 70` run would have reported turn@10 near
0.95 and been a sample of one category rather than of the benchmark.

Two recalls, because the benchmark supports both and they answer different
questions. `session@k` asks whether the right conversation surfaced, which is
what a reader would then have to read. `turn@k` asks whether the specific line
flagged `has_answer` surfaced, which is what retrieval working should mean for
a store that returns facts rather than documents.

| Question type | n | session@5 | session@10 | turn@10 | turn@30 |
|---|---:|---:|---:|---:|---:|
| **overall** | **90** | **0.882** | **0.940** | **0.727** | **0.834** |
| single session assistant | 15 | 1.000 | 1.000 | 1.000 | 1.000 |
| single session user | 15 | 0.933 | 1.000 | 0.933 | 0.933 |
| knowledge update | 15 | 0.867 | 1.000 | 0.911 | 0.978 |
| temporal reasoning | 15 | 0.839 | 0.933 | 0.756 | 0.924 |
| single session preference | 15 | 0.933 | 0.933 | 0.467 | 0.578 |
| multi session | 15 | 0.722 | 0.772 | 0.297 | 0.590 |

**Fifteen per cell is few, so here are the intervals rather than only the
means.** Overall, 95% confidence: session@10 [0.905, 0.975], turn@10 [0.645,
0.809]. Per type the intervals are wide enough that the middle of the table
should not be ranked: single session preference is 0.467 plus or minus 0.245.
Only the two ends survive that, and they are the interesting parts anyway.

**Multi session is the floor, at turn@10 0.297 plus or minus 0.150.** When an
answer is spread across conversations, ranking the individual lines that carry
it is where this fails, exactly as LoCoMo's multi hop row says. The same
feature, v1b's multi hop retrieval, is the answer to both, and both numbers now
exist before it does.

**Session recall stays high while turn recall falls**, 0.940 against 0.727. The
right conversation is usually found; the specific line inside it often is not.
For a store that returns facts rather than documents that gap is the honest
statement of what is still missing, and it is invisible if only one of the two
is reported.

**Single session preference is the surprise**, session@10 0.933 but turn@10
0.467: the conversation is found almost always and the line inside it less than
half the time. A stated preference tends to be a short aside inside a long
exchange about something else, which is the shape hybrid retrieval handles
worst. Not chased down, and recorded here so it is not quietly forgotten.

## Both of them through `eval-external`, 2026-09-26

The two standalone scripts are now one module behind `echo-memory eval-external`.
The first thing to establish about a rewritten harness is that it did not move
the numbers, so both benchmarks were run again on the same scratch databases the
runs above filled.

LoCoMo, all 1,982 questions, 5,882 facts already present so nothing was
rewritten, 53 seconds:

| Category | n | recall@1 | recall@10 | recall@30 | hit@10 | session@10 | MRR |
|---|---:|---:|---:|---:|---:|---:|---:|
| **overall** | **1,982** | **0.324** | **0.601** | **0.708** | **0.658** | **0.850** | **0.460** |
| temporal | 321 | 0.439 | 0.691 | 0.794 | 0.717 | 0.860 | 0.556 |
| single hop | 841 | 0.383 | 0.680 | 0.773 | 0.697 | 0.925 | 0.498 |
| adversarial | 446 | 0.308 | 0.584 | 0.697 | 0.594 | 0.915 | 0.401 |
| multi hop | 282 | 0.099 | 0.386 | 0.530 | 0.649 | 0.616 | 0.391 |
| open domain | 92 | 0.139 | 0.310 | 0.415 | 0.435 | 0.538 | 0.263 |

Every column the script printed is unchanged to three decimals. `session@10` is
new, and LoCoMo supports it because a `dia_id` names the session it belongs to:
0.850 against turn recall of 0.601 is the same gap LongMemEval showed, on a
second corpus. The right conversation comes back far more often than the right
line in it.

**A resumed run scores facts the old code wrote, so ingest was checked on its
own.** conv-26 written into an empty database wrote 419 facts, the same count
the 2026-09-17 run left in its scope, in 20 seconds, and scored its 197
questions identically: recall@1 0.302, recall@10 0.567, recall@30 0.663, hit@10
0.614, session@10 0.848, MRR 0.415, the same six figures from both databases.

LongMemEval S, the same stratified 15 of each of the six types, 90 questions,
170 seconds:

| Question type | n | recall@1 | recall@10 | recall@30 | hit@10 | session@10 | MRR |
|---|---:|---:|---:|---:|---:|---:|---:|
| **overall** | **90** | **0.181** | **0.727** | **0.834** | **0.833** | **0.937** | **0.428** |
| single session assistant | 15 | 0.467 | 1.000 | 1.000 | 1.000 | 1.000 | 0.617 |
| single session user | 15 | 0.300 | 0.933 | 0.933 | 0.933 | 1.000 | 0.501 |
| knowledge update | 15 | 0.100 | 0.911 | 0.978 | 1.000 | 1.000 | 0.504 |
| temporal reasoning | 15 | 0.156 | 0.756 | 0.924 | 0.867 | 0.933 | 0.515 |
| single session preference | 15 | 0.000 | 0.467 | 0.578 | 0.533 | 0.933 | 0.119 |
| multi session | 15 | 0.067 | 0.297 | 0.590 | 0.667 | 0.756 | 0.310 |

`recall@k` here is what the 2026-09-18 table called `turn@k`, and it is identical
in every cell. **One cell moved**: multi session `session@10`, 0.772 to 0.756.
Two things changed between the runs and neither can be ruled out from here. 89 of
the 90 scopes were already present and one was not, so 527 turns were written
fresh into that scope. And 0.5.1 made vector search use the HNSW index, which is
approximate: 98% of the exact top fifty rather than all of it. That every other
cell of both tables is identical to three decimals is the useful thing this run
says about that change, and it is a stronger statement than the 0.980 recall
figure on its own.

recall@1 and MRR are printed for LongMemEval for the first time, and
`single-session-preference` at recall@1 0.000 and MRR 0.119 is the sharpest
statement of the gap in the table above: the conversation is found 93% of the
time and the line inside it is never ranked first.

### The closest this gets to accuracy without a model

`answer_words@10` is **0.629** on LoCoMo, over the 1,242 of 1,982 questions whose
gold answer has two or more content words, and **0.775** on the LongMemEval
sample, over 62 of 90.

It is the share of the gold answer's own content words that appear in the ten
facts that came back. It is not accuracy and must not be quoted as though it
were: it undercounts every answer that paraphrases the transcript, which in
LoCoMo is most of them, so it is a floor under what a reader could have written.
The excluded questions either supply no gold answer at all, which is true of
LoCoMo's 446 adversarial ones, or supply one that ten facts would match by
chance, which is what "yes" and "2022" would do to this metric.

The `accuracy` field in the JSON report is `null`, with the reason beside it, for
as long as no model is called anywhere in this harness.

### Getting the two files

```bash
curl -sLO https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json
curl -sLo longmemeval_s.json \
  https://huggingface.co/datasets/xiaowu0162/longmemeval/resolve/main/longmemeval_s
```

Neither needs a credential or an account as of 2026-09-26. What was measured
above, and what the JSON report records for every run, is:

| File | Bytes | sha256 |
|---|---:|---|
| locomo10.json | 2,805,274 | `79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4` |
| longmemeval_s | 278,025,796 | `08d8dad4be43ee2049a22ff5674eb86725d0ce5ff434cde2627e5e8e7e117894` |

The LongMemEval hash is also what the Hub serves as that file's `x-linked-etag`,
which is what makes it a revision identifier rather than only a checksum.

A full LongMemEval run is 500 scopes and 246,930 facts, which is hours and not
something to start on a machine you need. `--per-type N` is the honest subset and
`--limit N` is a smoke test, for the reason in the section above.

## Context cost as the store grows

`eval --context` reports one number, and a number without its denominator is
easy to read wrongly in both directions. `eval --context --sweep` measures the
same thing at several corpus sizes, by building real sub scopes from a prefix
of the store's own facts and querying each one.

On this author's store, 2026-09-17:

```
    facts    inject   recall   hit@10   saving
  --------------------------------------------
       32     4,531    1,108    0.900    75.5%
       65     9,371    1,168    0.887    87.5%
      130    21,288    1,431    0.911    93.3%
      261    42,633    1,522    0.946    96.4%
```

Across 8x of corpus growth the cost of injecting everything rose 9x, while what
a recall returned moved **+37%**. The recall cost is bounded, not fixed, and the
sweep prints the drift rather than assuming it away: `top_k` limits how many
facts come back but not how long they are.

`hit@10` is printed in every row deliberately. A saving is only worth having if
the answer is still in what came back, and a configuration that returned nothing
would score a perfect 100%.

## What a write costs as the store grows

Found while ingesting LongMemEval, by noticing that throughput fell from 28
writes a second to 8 over one run and then checking whether that was the
machine or the store. It was the store: an empty database on the same machine
at the same moment still ran at 28/s.

| Store size | Before | After |
|---|---:|---:|
| 1,057 nodes | 29ms | 22ms |
| 24,054 nodes | 129ms | 45ms |

Two defects, both the same shape. A neighbourhood lookup and an entity lookup
each went through Cypher, where `MATCH ... WHERE id(x) = $id` cannot use an
index, because AGE expands the match and filters afterwards. Each therefore
scanned the whole graph on every write: every scope, and in a hosted deployment
every tenant. Read off the tables directly they are index lookups, and one of
them reaches an index that migration 0016 had already built for it.

The remaining growth, roughly 2x across that range, is the vector index getting
larger as it gains rows. That one is real rather than a defect, and it is the
number to beat next.

## The graph hop, measured after it stopped costing a minute

`query_memory(graph_hops=1)` expands from the facts a query already found, and
has been off by default since it was written. It turned out to take **58,356ms
against 44ms** on a 376 fact scope, because its Cypher left the pattern unbound
and AGE walked every edge in the database per seed. That is now 21.1ms against
21.4ms: the hop is free.

Which made a full ablation worth running, on this author's store, 2026-09-22:

| shape | shipping | + graph hop | ΔMRR | 95% CI |
|---|---:|---:|---:|---|
| entity_pair | 0.641 | 0.499 | −0.142 | [−0.182, −0.104] |
| entity_single | 0.690 | 0.591 | −0.099 | [−0.135, −0.062] |
| prose | 0.974 | 0.849 | −0.125 | [−0.158, −0.094] |
| multihop | 0.196 | 0.200 | +0.003 | [−0.022, +0.028] |

**It stays off.** Three intervals clear of zero in the same direction is not
noise: on a question one fact answers, the neighbours of that fact dilute the
ranking.

The thing it does buy does not show up in MRR at all. On the multihop shape,
recall@10 rises **0.635 to 0.769**. It finds the second fact and ranks it
badly, which is why the code is kept, and why the useful version of this
feature turns the hop on per question rather than for everybody. Nothing can
yet tell a multi hop question from a single hop one, and that is the actual v1b
problem.

## Against Mem0, Zep, Letta and cognee, 2026-10-27

Every number above is about this system with nothing beside it, which is the
limitation issue #147 names. `eval-external --backend` runs another memory
system through the same harness: the same corpus, the same scoring, the same
code path, because a comparison where our side ran one path and theirs ran
another is measuring the harness.

### The baseline this comparison uses

LoCoMo in full, all ten conversations, 5,882 turns into an empty scratch
database in **310 seconds** (18.9 turns/s), 1,982 questions scored. Dataset
sha256 `79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4`.

| Category | n | recall@1 | recall@10 | recall@30 | hit@10 | session@10 | MRR |
|---|---:|---:|---:|---:|---:|---:|---:|
| **overall** | **1,982** | **0.341** | **0.643** | **0.725** | **0.700** | **0.907** | **0.485** |
| temporal | 321 | 0.450 | 0.692 | 0.775 | 0.720 | 0.900 | 0.565 |
| single hop | 841 | 0.385 | 0.712 | 0.784 | 0.729 | 0.976 | 0.514 |
| adversarial | 446 | 0.380 | 0.722 | 0.794 | 0.729 | 0.980 | 0.497 |
| multi hop | 282 | 0.088 | 0.361 | 0.503 | 0.631 | 0.681 | 0.360 |
| open domain | 92 | 0.136 | 0.319 | 0.362 | 0.435 | 0.647 | 0.260 |

`answer_words@10` 0.629 over the 1,242 questions with two or more content
words in their gold answer.

**This is not the same as the 2026-09-26 row above.** MRR 0.460 to 0.485,
recall@10 0.601 to 0.643, on the identical corpus, the identical question set
and the same scoring code.

The delta is **not attributed to any one change here, because it has not been
ablated.** Several retrieval changes landed in between, and the most likely
contributor is BM25 replacing `ts_rank` on the lexical channel: that went in at
15:24 on 2026-09-26 and the run above it was recorded at 14:37 the same day, so
it is genuinely after it rather than merely later in the file. Saying which
change earned the 0.025 would need each one run on its own, which is what
`eval` and its ablation flags are for and is not what this section measured.

Both rows are kept rather than the older one being overwritten. A benchmark
file that only ever shows the current number cannot be used to check whether a
change helped.

Multi hop at MRR 0.360 and open domain at 0.260 remain the weak shapes, and
`session@10` 0.907 against `recall@10` 0.643 is the same gap both corpora have
always shown. The right conversation comes back far more often than the right
line in it.

### The attribution problem, which decides whether any of this is honest

Scoring matches what came back against the gold turns by reading a turn key out
of each result. That works here because one turn becomes one fact, so the key
survives exactly. It does not generalise, and the ways it fails all run in our
favour unless they are handled:

| system | what a result is | attribution |
|---|---|---|
| echo-mem | one stored turn | exact |
| Zep | a fact derived from several episodes | **every** source episode, via `episode_uuids` |
| Mem0 | a memory derived from one or more turns | one `turn_key`, even when several turns fed it |
| Letta | a stored passage | exact, via `tags` |
| cognee | a chunk of the graph | unverified; two routes attempted |

Two consequences are built into the scoring rather than mentioned afterwards.

**A result may name several source turns.** Crediting a Zep fact derived from
three turns to one of them scores the other two as misses on a system that
genuinely surfaced them. Measured on the fixture: recall@1 1.000 reading all
sources against 0.500 reading one. Half the recall, against a competitor.

**A system that cannot attribute a result scores null, not zero.** A zero
reports a competitor that retrieved the right content and merely could not name
its source turn as having retrieved nothing. `means` drops nulls rather than
averaging them in, and every report says which metrics were not computable and
why. `attribution_rate` sits beside the turn-keyed numbers so a low recall can
be told from a recall that was never measurable.

**And attribution can be present and wrong, which is worse than absent.** Mem0
on the fixture returned `attribution_rate` 1.000 with `recall@k` 0.000 and
`session@10` 0.750: right sessions, wrong turns, because a memory synthesised
from several turns carries one of their keys. Nothing outside Mem0 can tell
that case from a genuine miss, so its turn-keyed metrics understate it by an
unknown amount. For Mem0, `session@k`, `answer_words@k` and the judged answer
are the comparisons that carry weight.

### Write cost, which is what this project is actually about

Measured, not projected, for ours. Measured on 12 turns and extrapolated for
Zep, which is flagged as coarse until the full run replaces it:

| | turns/s | LoCoMo, 5,882 turns |
|---|---:|---|
| echo-mem | 18.9 | 310 seconds |
| Zep | 0.17 | ~9.8 hours, extrapolated |

Zep's own documentation says every episode triggers multiple LLM calls and that
write cost scales with volume, so a gap of this shape is expected; its size is
the thing worth having a number for. LongMemEval S at 246,930 turns projects to
roughly 17 days per hosted backend, which is why the full comparison is run on
LoCoMo and LongMemEval is not claimed.

Letta should look like us rather than like Zep, because inserting a passage
embeds it without running an extraction model. cognee splits the two: `add` is
cheap and `cognify` is where the model cost lands, so its `ingest` does both
and reports both. Those two predictions are written here before the runs so
they can be checked rather than adjusted afterwards.

### Retrieval is a ceiling, and the judged pass is the comparable number

Everything above is retrieval: did the turn holding the answer come back. Mem0
and Zep publish model-judged QA accuracy, which is a different measurement, and
quoting one as the other would be dishonest in the specific way that is easy to
get away with. `judge-external` produces the comparable number from a saved
results file:

- one model answers each question from **only** what that backend returned,
  never the gold answer and never the transcript, and may reply `CANNOT ANSWER`
- that outcome is counted apart from a wrong answer, because for a memory
  system "did not retrieve it" and "retrieved and misled" are different
  failures
- **two judges from different vendors** grade the identical answer string, so
  their disagreement is judge variance and nothing else
- reported as raw agreement **and** Cohen's kappa, because two judges who both
  say CORRECT 90% of the time agree 82% of the time by chance

Both prompts are constants in `src/echo_memory/eval/qa.py` and are published
with any number taken from them. A judged figure whose prompt is not published
cannot be argued with, and one that cannot be argued with is not evidence.

### Running it

The backend SDKs are **not** in this project's requirements: they pull heavy,
mutually incompatible dependency trees. Install them into a separate
environment.

```bash
python3 -m venv .venv-bench
.venv-bench/bin/pip install -e . mem0ai zep-cloud letta-client cognee
```

Credentials are read from the environment and there is deliberately no flag for
any of them, because a benchmark command ends up in a shell history and beside
a results file.

```bash
export MEM0_API_KEY=...                  # mem0
export ZEP_API_KEY=...                   # zep
export LETTA_BASE_URL=http://localhost:8283   # letta, plus a server
export LETTA_MODEL=... LETTA_EMBEDDING=...
export COGNEE_LLM_API_KEY=... COGNEE_LLM_MODEL=...

echo-memory eval-external locomo locomo10.json --backend zep \
  --json zep.json --results zep.jsonl
```

Every backend is checked **before** anything is ingested, because a four system
run that dies on the fourth for a missing key has wasted the first three, and
with Mem0 and Zep billing per write it has wasted them expensively.

Then the judged pass over the saved file, which needs two providers from
different vendors:

```bash
export ECHO_MEMORY_LLM_API_KEY=...   ECHO_MEMORY_LLM_MODEL=...
export ECHO_MEMORY_JUDGE2_API_KEY=... ECHO_MEMORY_JUDGE2_MODEL=...
export ECHO_MEMORY_JUDGE2_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai

echo-memory judge-external zep.jsonl --json zep-judged.json
```

### What is verified and what is not

Verified by a live run: the echo-mem baseline above; Mem0 and Zep end to end on
the fixture, including ingest, settle, score and render.

**Not yet run:** Mem0 and Zep on full LoCoMo; Letta and cognee against
anything, because both need a model provider and Letta also needs a server.
Their adapters are written to API shapes read off the installed SDKs, not
guessed, and are marked unverified in their own module docstrings. That
distinction is worth keeping: for both hosted backends, every assumption that
had not actually been run turned out to be wrong - Mem0's writes are
asynchronous, its `search` refuses the argument its `add` accepts, Zep creates
a duplicate graph for a repeated name, and `metadata` survives extraction in
both when the design had assumed it would not.

## Reproducing any of it

Point the two external benchmarks at a scratch database. They write real facts
through the real code path, so anything they touch is indistinguishable from
ordinary memory afterwards, and `eval-external` refuses to start when the
target database holds facts outside its own scopes.

```bash
echo-memory eval                   # retrieval quality against your own store
echo-memory eval --context         # what a recall costs against injecting everything
echo-memory eval --context --sweep # the same, as a curve across corpus size
echo-memory calibrate              # is entity resolution trustworthy on your data
echo-memory benchmark              # write, query and digest latency
echo-memory eval-external locomo      locomo10.json      # published corpus, yours to fetch
echo-memory eval-external longmemeval longmemeval_s.json --per-type 15
```

`--json PATH` writes the machine-readable report, `--results PATH` appends each
question as it is scored so a killed run keeps what it measured, and a rerun
skips any scope already holding its full complement of facts. Neither dataset is
in this repository: both are somebody else's to license, and longmemeval_s is
278MB. `tests/fixtures/locomo_fixture.json` and
`tests/fixtures/longmemeval_fixture.json` are synthetic files in the two
schemas, which is what the test suite runs against and what to point the command
at first.

The most useful contribution to this repository is a measurement that disagrees
with one of these.
