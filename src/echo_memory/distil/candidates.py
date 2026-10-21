"""Turning human turns into candidate facts, with the operator's own model.

The server still makes no model call, and this does not change that. Same
arrangement as `infer-causal-hints`: a command a person types, reading a key
from the environment, over material that was stored without a model in the
loop. The claim the product rests on is not "no model anywhere", it is that
storing a memory never depends on one.

What this produces is a CANDIDATE, never a fact. Nothing here writes. An
extraction pass over a chat transcript will hallucinate, will promote an idea
somebody rejected two turns later, and will mistake a question for a decision.
Writing that straight into the graph would poison the thing the product
exists to keep clean, and supersession would then spread it rather than
contain it.

So the output is reviewed before it is written. That is the same step Beacon
has - "Review + approve" - and it is worth being honest that it is also where
their loop can stall, for the same reason echo-mem's does: the last step needs
a human's attention. The difference this makes is narrow and real. Today a
fact is captured only if an agent decides mid-task to call a tool, which
measurably does not happen. After this, capture is automatic and only
PROMOTION needs a person, once per session, over a list.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# How many turns go in one request. Small enough that a single bad batch is
# cheap to discard and re-run, large enough that the model can see a decision
# being revised a few turns later rather than reading each line alone.
BATCH_TURNS = 40

# How much of the person's own words a candidate must carry to count as
# evidence. Without a floor the guard is defeatable by accident: a one
# character quote like "x" is a substring of almost any input, so it passes a
# containment check while supporting nothing. Found by the test that tried it.
MIN_QUOTE_CHARS = 20

# Facts per batch. A cap rather than a target: a batch of forty turns that
# genuinely contains two decisions should return two, and a model asked for
# "as many as possible" will pad to the number.
MAX_PER_BATCH = 8

SYSTEM = """You read a transcript of what a person typed to a coding agent and
report only what they DECIDED, CORRECTED, or STATED AS A PREFERENCE.

You are not summarising. You are looking for sentences that will still matter
in three months to somebody who was not there.

Report:
- a decision ("we use X", "deploy only from main")
- a correction ("no, it is Y not X") - these are the most valuable
- a stated preference about how work should be done
- a constraint or fact about the system that was not obvious

Do NOT report:
- questions, or requests to do something ("fix the tests", "what is next")
- anything the person later reversed in a LATER turn
- restatements of what you can already read in the code
- anything you are inferring rather than reading. If the person did not say
  it, it is not there.

Return STRICT JSON, an object with one key "facts", a list. Each item:
  {"subject": "<short entity name>",
   "object": "<short entity name>",
   "relation": "<short_snake_case>",
   "fact": "<one sentence, in the person's own terms, standing alone>",
   "quote": "<the words they actually typed that support it>"}

subject and object are the two things the fact is ABOUT, and must be short
noun phrases, not sentences. `quote` must appear in the input; if you cannot
quote it, do not report it. Return {"facts": []} rather than padding."""


@dataclass(frozen=True)
class Candidate:
    """One proposed fact, not yet written and not yet believed."""

    subject: str
    object: str
    relation: str
    fact: str
    quote: str = ""

    def as_episode(self) -> tuple[list[dict], list[dict]]:
        """The entities and facts arguments `write_episode` expects.

        confidence is "extracted" because the quote is required to be the
        person's own words. A candidate the model could not quote never
        reaches here, which is what keeps that label honest.
        """
        entities = [
            {"name": self.subject, "type": "entity"},
            {"name": self.object, "type": "entity"},
        ]
        if self.subject == self.object:
            entities = entities[:1]
        facts = [{
            "source": self.subject,
            "target": self.object,
            "relation_type": self.relation,
            "confidence": "extracted",
            "fact": self.fact,
        }]
        return entities, facts


@dataclass
class Extraction:
    """What one pass over a session produced, including what it could not do."""

    candidates: list[Candidate] = field(default_factory=list)
    batches: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


def batched(turns: list[str], size: int = BATCH_TURNS):
    for i in range(0, len(turns), size):
        yield turns[i : i + size]


def _parse(raw: str, source_turns: list[str]) -> tuple[list[Candidate], str | None]:
    """Read the model's reply, refusing anything it could not quote.

    The quote check is the one guard that cannot be argued with. A model that
    invents a decision cannot also produce the words the person typed, so
    requiring the quote to appear in the input turns the usual "trust the
    extraction" problem into a string comparison.
    """
    text = raw.strip()
    # Models wrap JSON in fences despite being told not to; tolerate it rather
    # than discard a whole batch over punctuation.
    if text.startswith("```"):
        text = text.split("```")[1]
        text = text.removeprefix("json")
    try:
        payload = json.loads(text)
    except ValueError:
        return [], "reply was not JSON"

    items = payload.get("facts") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return [], "reply had no 'facts' list"

    haystack = "\n".join(source_turns)
    out: list[Candidate] = []
    for item in items[:MAX_PER_BATCH]:
        if not isinstance(item, dict):
            continue
        subject = str(item.get("subject") or "").strip()
        obj = str(item.get("object") or "").strip()
        relation = str(item.get("relation") or "").strip()
        fact = str(item.get("fact") or "").strip()
        quote = str(item.get("quote") or "").strip()
        if not (subject and obj and relation and fact):
            continue
        # Unquotable means unsupported. Dropped silently rather than reported
        # as a failure: one invented item in a good batch is the model being a
        # model, not the batch being broken.
        if len(quote) < MIN_QUOTE_CHARS or quote[:60] not in haystack:
            continue
        out.append(Candidate(subject, obj, relation, fact, quote))
    return out, None


def extract(turns: list[str], provider, complete) -> Extraction:
    """Run every batch, keeping what parsed and recording what did not.

    `complete` is injected rather than imported so this is testable without a
    provider, which is also how the parsing guards above are tested at all.

    A failed batch does not abort the run. Thirty batches where one times out
    should produce twenty-nine batches of candidates and one line saying so,
    not nothing.
    """
    result = Extraction()
    for batch in batched(turns):
        result.batches += 1
        numbered = "\n\n".join(f"[{i + 1}] {t}" for i, t in enumerate(batch))
        try:
            raw = complete(provider, SYSTEM, numbered)
        except Exception as e:  # noqa: BLE001
            result.failures.append(f"batch {result.batches}: {e}")
            continue
        found, problem = _parse(raw, batch)
        if problem:
            result.failures.append(f"batch {result.batches}: {problem}")
        result.candidates.extend(found)
    return result
