"""Whether a candidate summary still says what its sources said.

This is the gate on consolidation, built before consolidation, because the
design doc asks for exactly that: "the invariant check is a gate on shipping
consolidation at all, not a metric to add afterwards". A gate written after the
thing it gates is a rationalisation of whatever that thing already does.

## Why retrieval metrics cannot do this job

Hit rate and recall ask whether the right node came back, and after a bad
consolidation the right node does come back. It just no longer means what the
facts underneath it meant. The acceptance criteria in
`tests/fixtures/consolidation_invariants.json` were contributed against that
paragraph of the design doc, by email, before any of this existed, and they are
the specification this module implements rather than a test written to match it.

## What it checks, and the honest limit of each

Four of the five fixtures are decidable without a model. That was not obvious
and is worth stating plainly, because the fixture file predicted otherwise for
one of them.

**supersession_respected** is exact. A fact's `valid_from` and `valid_until`
are structured, so "this summary asserts something that had already been
superseded when it was written" is arithmetic. Nothing lexical about it.

**no_unconditionalization** and **no_unsupported_strengthening** are lexical
and therefore one-directional. A source whose prose carries `unless` or `may`,
summarised into prose carrying neither, has lost something the marker was
holding. That direction is reliable. The reverse, deciding whether a condition
that IS still present is still the RIGHT condition, is not something a word
list can do, and this module does not pretend to.

So these two reject confidently and accept weakly, which is the correct
asymmetry for a gate: a summary that keeps the hedge has only cleared the bar
of not having obviously dropped it.

The fixture file says of the hedging case that "a deterministic check has
nothing structured to compare and this fixture currently belongs to the
independent evaluator". That is right about `confidence`, which records how a
fact was obtained rather than the modality of its claim, and it does not follow
that nothing can be done. The hedge is in the prose, and prose is where a
marker check looks. This is weaker than a semantic evaluator and it is not
nothing, and it is available today without a model or a schema change.

**causal_direction_preserved** compares the candidate's own cause and effect,
read off its connective, against the direction stored on the edge. "A caused B"
and "B was caused by A" are the same claim written from opposite ends, which is
the same equivalence `trace_cause` already relies on, so both forms are
accepted and only a genuine reversal is refused.

## What would make this stronger, in the order it would help

The open design question the fixtures raise is whether the write path should
carry conditions, exceptions and modality as FIELDS rather than leaving them
inside prose to be reconstructed on every consolidation. If it did,
`no_unconditionalization` and `no_unsupported_strengthening` stop being word
lists and become comparisons, and they start being able to accept confidently
as well as reject. That is a change to the tool contract agents follow, which
is the expensive kind, and it is a decision rather than an implementation
detail. This module is built so that it is a replacement of two functions when
that decision is made, not a rewrite.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Words that carry a condition or an exception.
#:
#: Deliberately short and deliberately common. A long list of rare connectives
#: would fire more often on a summary than on its sources and invert the check,
#: because the check is "the source had one and the candidate does not".
CONDITION_MARKERS = (
    "unless", "except", "if", "when", "whenever", "provided",
    "otherwise", "but not", "as long as", "in which case", "depending on",
)

#: Words that hedge a claim: the speaker is not asserting it outright.
#:
#: These are the modality the `confidence` enum cannot carry, because
#: `extracted`, `inferred` and `ambiguous` record how an agent OBTAINED a fact
#: and say nothing about how strongly the fact itself is stated.
HEDGE_MARKERS = (
    "may", "might", "could", "possibly", "probably", "likely", "perhaps",
    "appears", "appeared", "seems", "seemed", "suspected", "potentially",
    "apparently", "believed", "thought to", "unclear", "unconfirmed",
)

#: Connectives that put the cause FIRST: "A caused B".
CAUSAL_ACTIVE = (
    "caused", "causes", "led to", "leads to", "resulted in", "results in",
    "triggered", "triggers", "brought about", "gave rise to",
)

#: Connectives that put the cause SECOND: "B was caused by A".
#:
#: Checked before the active list, because every one of these contains an
#: active connective as a prefix and matching "caused" inside "caused by" would
#: read every passive sentence backwards. That is the bug this ordering exists
#: to prevent and it is the one a reader should check first.
CAUSAL_PASSIVE = (
    "caused by", "due to", "because of", "resulted from", "results from",
    "owing to", "attributable to", "stems from", "stemmed from",
)

#: Relation names that mean the EDGE's target is the cause rather than its
#: source. `causal_hint` spells this `caused_by`; a relation_type may spell it
#: any number of ways, and all of them end in `_by`.
TARGET_IS_CAUSE = ("caused_by", "_by")

#: Relation names that mean the edge's source is the cause.
SOURCE_IS_CAUSE = ("caused", "led_to", "leads_to", "resulted_in", "causes",
                   "enabled_by_source", "triggered")


@dataclass(frozen=True)
class Verdict:
    """Accepted, or refused with the single invariant responsible.

    One invariant, not a list, because a summary that breaks three of them
    needs fixing once and a reader needs the first reason rather than a report.
    `why` names the evidence rather than the rule, so an operator reading it
    can check the call rather than trust it.
    """

    accepted: bool
    invariant: str | None = None
    why: str = ""

    def __bool__(self) -> bool:
        return self.accepted


def _words(text: str | None) -> str:
    # None is a real input, not a defensive guard: every caller reads this out
    # of a fact dict with .get, and an absent causal_hint or target is the
    # normal case rather than an error. The body already collapsed it to the
    # empty string; the annotation said otherwise, which is the half that was
    # wrong.
    return " ".join(str(text or "").lower().split())


def _has(text: str, markers) -> str | None:
    """The first marker present as a whole word, or None.

    Word boundaries matter more here than anywhere else in this module: `may`
    is inside `maybe` and `dismay`, and `if` is inside `verify`, `notify` and
    `specific`. A substring test would report a hedge in almost every sentence
    and the check would accept everything.
    """
    for marker in markers:
        if re.search(rf"(?<!\w){re.escape(marker)}(?!\w)", text):
            return marker
    return None


def _as_number(value):
    """Order `valid_from` and `valid_until` whether they are ints or labels.

    Real facts carry unix seconds. The fixtures carry `t1` and `t2`, which are
    the same ordering written for a human. Supporting both costs four lines and
    means the acceptance criteria can be read by the person who wrote them.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if re.fullmatch(r"-?\d+(\.\d+)?", text):
        return float(text)
    digits = re.search(r"\d+", text)
    return float(digits.group()) if digits else text


def _live_at(source: dict, as_of) -> bool:
    """Whether this fact was believed at `as_of`.

    Half open, [valid_from, valid_until), which is the same convention
    `live_clause` uses on the read path. A fact superseded AT t2 is not a fact
    you may assert as of t2, and getting that boundary wrong is precisely the
    `superseded-fact` fixture.
    """
    if as_of is None:
        return True
    moment = _as_number(as_of)
    start = _as_number(source.get("valid_from"))
    end = _as_number(source.get("valid_until"))
    try:
        if start is not None and start > moment:
            return False
        if end is not None and end <= moment:
            return False
    except TypeError:
        # Mixed labels and numbers: not orderable, so do not claim it is dead.
        return True
    return True


def _names(source: dict) -> list[str]:
    return [str(source.get(k) or "") for k in ("source", "target") if source.get(k)]


def _mentions(candidate: str, name: str) -> bool:
    name = _words(name)
    if not name:
        return False
    return bool(re.search(rf"(?<!\w){re.escape(name)}(?!\w)", candidate))


def _candidate_direction(candidate: str, cause: str, effect: str) -> tuple[bool, str]:
    """(agrees, why) for the causal order the candidate's own wording states.

    Returns agrees=True when the candidate does not state a direction at all,
    because this invariant is about reversal and a summary that omits the
    causal claim has not reversed it. Dropping a causal claim entirely is a
    different failure, and inventing a verdict for it here would be this module
    deciding something nobody specified.
    """
    passive = _has(candidate, CAUSAL_PASSIVE)
    active = None if passive else _has(candidate, CAUSAL_ACTIVE)
    connective = passive or active
    if not connective:
        return True, "the candidate states no causal direction"

    at = candidate.find(connective)
    before, after = candidate[:at], candidate[at + len(connective):]
    # Passive puts the cause after the connective; active puts it before.
    cause_side, effect_side = (after, before) if passive else (before, after)

    cause_ok = _mentions(cause_side, cause)
    effect_ok = _mentions(effect_side, effect)
    if cause_ok and effect_ok:
        return True, ""
    # Only call it a reversal when the candidate actually puts them the other
    # way round. A candidate naming neither, or naming them somewhere else in
    # the sentence, is not evidence of a flip.
    if _mentions(cause_side, effect) and _mentions(effect_side, cause):
        return False, (
            f"the sources say {cause!r} caused {effect!r}; the candidate's "
            f"{connective!r} puts it the other way round"
        )
    return True, "the candidate does not place both entities around the connective"


def _causal_pair(source: dict) -> tuple[str, str] | None:
    """(cause, effect) for a causal edge, or None if this edge is not causal."""
    relation = _words(source.get("causal_hint") or source.get("relation_type") or "")
    if not relation:
        return None
    src, tgt = _words(source.get("source")), _words(source.get("target"))
    if not src or not tgt:
        return None
    if any(relation.endswith(suffix) or suffix in relation for suffix in TARGET_IS_CAUSE):
        return tgt, src
    if any(token in relation for token in SOURCE_IS_CAUSE):
        return src, tgt
    return None


def check(sources: list[dict], candidate: str, as_of=None) -> Verdict:
    """Whether `candidate` may stand in for `sources`.

    Rejects on the first invariant broken, in a fixed order, so the verdict is
    reproducible and names one thing to fix. The order is from the check that
    is exact to the checks that are lexical, which also means the strongest
    evidence wins when a summary breaks more than one.
    """
    text = _words(candidate)
    if not text:
        return Verdict(False, "faithful_compression", "the candidate is empty")

    live = [s for s in sources if _live_at(s, as_of)]
    dead = [s for s in sources if not _live_at(s, as_of)]

    # 1. Supersession. Exact, because the dates are structured.
    live_names = {_words(n) for s in live for n in _names(s)}
    for s in dead:
        for name in _names(s):
            if _words(name) in live_names:
                continue
            if _mentions(text, name):
                return Verdict(
                    False, "supersession_respected",
                    f"the candidate asserts {name!r}, which appears only in a "
                    f"fact that was already superseded at as_of={as_of!r}",
                )

    # 2. Conditions and exceptions. Lexical, rejects confidently only.
    if not _has(text, CONDITION_MARKERS):
        for s in live:
            marker = _has(_words(s.get("fact")), CONDITION_MARKERS)
            if marker:
                return Verdict(
                    False, "no_unconditionalization",
                    f"a source fact is conditional on {marker!r} and the "
                    f"candidate carries no condition at all",
                )

    # 3. Modality. Same shape, same asymmetry.
    if not _has(text, HEDGE_MARKERS):
        for s in live:
            marker = _has(_words(s.get("fact")), HEDGE_MARKERS)
            if marker:
                return Verdict(
                    False, "no_unsupported_strengthening",
                    f"a source fact hedges with {marker!r} and the candidate "
                    f"states it outright",
                )

    # 4. Causal direction.
    for s in live:
        pair = _causal_pair(s)
        if not pair:
            continue
        agrees, why = _candidate_direction(text, *pair)
        if not agrees:
            return Verdict(False, "causal_direction_preserved", why)

    return Verdict(True, None, "no invariant was broken by this candidate")
