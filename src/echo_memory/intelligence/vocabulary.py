"""The reserved words the intelligence layer reads structurally.

Capability A needs to connect a decision to what actually happened. The
temptation is a decision table, an outcome table and a join. That would be a
second data model beside the graph, with its own temporal rules, its own
tenancy column and its own supersession logic - all of which this store already
has and has tested.

A decision IS a node. An outcome IS a node. "This decision led to that outcome"
IS a FACT edge with a causal_hint. So the whole of Capability A's storage is a
vocabulary agreement plus two indexes:

    Node(type=decision, name="migrate-payments-to-razorpay")
      -[FACT causal_hint=led_to, relation_type=observed_on]->
    Node(type=outcome, name="checkout-error-rate-2026-11")

What that buys, concretely: `trace_cause` already walks these chains today, and
already normalises "A led_to B" against "B caused_by A" through CAUSE_IS_SOURCE,
so a decision and an outcome written by two different sessions assemble in the
right order with no new traversal code. Supersession already applies, so an
outcome that turns out wrong gets a t_invalid rather than a delete. Erasure
already reaches it. Attestation already covers it.

## Why `type` is only a convention here

`Node.type` is free text and stays free text. Nothing refuses a node whose type
is `decision` but which is not one, and nothing could: the server does not call
a model and cannot read the sentence. What migration 0034 adds is a partial
index so that asking for this scope's decisions does not scan it.

The vocabulary is reserved in the weaker sense that matters: these strings mean
this to the intelligence layer, so a caller using `decision` for something else
will find it showing up in lesson candidates. Documented rather than enforced,
because the alternative is a validation that rejects a customer's existing nodes.
"""

from __future__ import annotations

# ---------------------------------------------------------------- node types

#: Something an agent or a person chose to do, and the thing a lesson is about.
DECISION = "decision"

#: A step taken in service of a decision. Distinct from the decision because
#: one decision usually has several, and because an action can fail without the
#: decision being wrong.
ACTION = "action"

#: What was observed afterwards. The half that was missing: this store could
#: always record that a decision was made and never that it worked.
OUTCOME = "outcome"

RESERVED_NODE_TYPES = frozenset({DECISION, ACTION, OUTCOME})

# ------------------------------------------------------------ relation types

#: An outcome node to the evidence that establishes it.
EVIDENCED_BY = "evidenced_by"

#: A decision to an outcome observed after it. Carries causal_hint=led_to.
OBSERVED_ON = "observed_on"

#: A decision that replaces an earlier one. Separate from supersession, which
#: is about one claim being restated; this is about changing your mind.
SUPERSEDES_DECISION = "supersedes_decision"

#: An action taken under a decision. Carries causal_hint=enabled_by.
TAKEN_UNDER = "taken_under"

RESERVED_RELATION_TYPES = frozenset(
    {EVIDENCED_BY, OBSERVED_ON, SUPERSEDES_DECISION, TAKEN_UNDER}
)

# ------------------------------------------------------------- outcome signs

#: Did the thing work. A SEPARATE axis from `confidence`, and the distinction is
#: load bearing: confidence says how the claim was obtained, sign says what it
#: reports. A fact can be `extracted` with perfect fidelity and report a failure.
SUCCESS = "success"
FAILURE = "failure"
MIXED = "mixed"
UNKNOWN = "unknown"

OUTCOME_SIGNS = (SUCCESS, FAILURE, MIXED, UNKNOWN)

#: How a sign counts toward a lesson. MIXED and UNKNOWN deliberately count as
#: evidence that the decision was TAKEN without counting for or against it, so
#: a decision with ten mixed outcomes produces no lesson rather than a weak one.
SIGN_VALUE = {SUCCESS: 1, FAILURE: -1, MIXED: 0, UNKNOWN: 0}

# ------------------------------------------------------------------- lessons

#: How a conclusion was reached. Decision 1 of the approved architecture
#: requires that observations, correlations and verified causal conclusions stay
#: distinguishable, and that nothing silently promotes one to another.
#:
#: OBSERVED    this is what the evidence says, no generalisation
#: CORRELATIONAL   outcomes co-occur with this decision. NOT a causal claim
#: VERIFIED_CAUSAL a verifier asserted the causal link, naming a method
BASIS_OBSERVED = "observed"
BASIS_CORRELATIONAL = "correlational"
BASIS_VERIFIED_CAUSAL = "verified_causal"
LESSON_BASES = (BASIS_OBSERVED, BASIS_CORRELATIONAL, BASIS_VERIFIED_CAUSAL)

#: A derived conclusion is always `inferred`, whatever its basis, and this
#: string is what keeps it out of the verified vocabulary. A lesson is never
#: written as a FACT edge; see intelligence/lessons.py, and the test that
#: asserts that module does not import write_episode.
KIND_INFERRED = "inferred"

#: How many agreeing, verified-or-evidence-backed outcomes a decision needs
#: before it yields a lesson.
#:
#: Two, not one. One outcome is an anecdote and this store already returns it -
#: asking query_memory about the decision finds it. A lesson claims a pattern,
#: and the smallest number of points that can show one is two. Not higher,
#: because at three a real repeated failure stays invisible through the two
#: occurrences where it would have been cheapest to catch.
MIN_SUPPORT = 2

#: When outcomes disagree, how lopsided they must be to still yield a lesson.
#: 1.0 means unanimity. Anything less invites "mostly worked" conclusions, and
#: a decision whose outcomes disagree is exactly the case a reader should see
#: as disputed rather than resolved.
REQUIRED_AGREEMENT = 1.0
