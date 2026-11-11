"""The six states a claim can be in, and which of them retrieval must obey.

A fact in this store is believed because somebody wrote it. `confidence` says
how the writer came by it - extracted, inferred, ambiguous - and that is a
statement about provenance, not about truth. Nothing until now recorded whether
a claim had been *checked*.

This is that record. It is deliberately not a number. A score invites
arithmetic across claims that were established in different ways, and the one
calibration this project has ever measured came out AUC 0.766 with a 95%
interval of [0.437, 0.968] over five positives - an interval that includes
chance. A six value state that says who checked it and how is honest about the
same evidence.

## Authoritative and advisory, and why the line matters

    authoritative   changes WHICH facts retrieval returns
    advisory        changes only the ORDER they come back in

`query_memory` already records the governing principle for this, in the comment
on COSINE_FLOOR: hiding a real memory is worse than including a mediocre one.
Every state computed from a heuristic is therefore advisory, and the only state
that can remove a fact from an answer is one an authorised verifier set on
purpose.

That split is also what stops this becoming a denial of service on your own
memory. A rule that could mark things `stale` in bulk can reorder a thousand
facts and cannot hide one.
"""

from __future__ import annotations

#: Recorded, nothing checked. Every fact written before this existed, and every
#: fact written since by a caller that did not attach evidence.
UNVERIFIED = "unverified"

#: At least one `evidence` row points at it. Evidence is not proof: a
#: deployment log saying the deploy happened does not establish that the
#: decision behind it was right.
EVIDENCE_BACKED = "evidence_backed"

#: An authorised verifier asserted it, naming a method and a policy. The only
#: state that makes a fact eligible to support a lesson.
VERIFIED = "verified"

#: Contradicting evidence or a contradicting fact exists. Returned WITH the
#: contradiction rather than suppressed - a disputed claim the reader can see
#: is worth more than a gap they cannot.
DISPUTED = "disputed"

#: A verification that has expired, or whose scope has passed. Advisory,
#: because a lapsed check is not a refutation.
STALE = "stale"

#: An authorised verifier refused it. The only state that removes a fact from
#: an answer.
REJECTED = "rejected"

ALL_STATES = (UNVERIFIED, EVIDENCE_BACKED, VERIFIED, DISPUTED, STALE, REJECTED)

#: States an authorised verifier sets deliberately, and which therefore decide
#: what retrieval returns at all.
AUTHORITATIVE = frozenset({VERIFIED, DISPUTED, REJECTED})

#: States that may be computed, and which therefore may only reweight.
ADVISORY = frozenset({UNVERIFIED, EVIDENCE_BACKED, STALE})

#: Never returned by a read. Exactly one member, and it is the one a human or an
#: approved rule had to choose.
SUPPRESSED = frozenset({REJECTED})

#: Who performed a verification. Not an enum in the database either; see the
#: CHECK constraint in migration 0032 and the note there about audit_mutation_type.
HUMAN, SERVICE, RULE = "human", "service", "rule"
VERIFIER_KINDS = (HUMAN, SERVICE, RULE)

#: How much a state is worth as a ranked list, relative to the content channels.
#:
#: These are weights for reciprocal rank fusion, not probabilities, and they are
#: starting values rather than measured ones. The honest position: nothing has
#: measured them yet, the eval harness is the thing that decides them, and
#: shipping them at 0.0 would be the same as not shipping the feature. They are
#: here so the ablation has something to move.
#:
#: VERIFIED above 1.0 and STALE below it, because a checked claim should outrank
#: an unchecked one about the same thing, and a lapsed check should not.
TRUST_WEIGHTS = {
    VERIFIED: 1.0,
    EVIDENCE_BACKED: 0.6,
    UNVERIFIED: 0.0,
    DISPUTED: 0.3,
    STALE: -0.3,
    REJECTED: 0.0,
}


def is_authoritative(state: str) -> bool:
    return state in AUTHORITATIVE


def suppresses(state: str) -> bool:
    """Whether this state removes a fact from an answer entirely."""
    return state in SUPPRESSED


def weight(state: str) -> float:
    """The RRF weight for a trust state. Unknown states weigh nothing rather
    than defaulting to something: a state this version does not recognise was
    written by a newer one, and guessing what it meant is how a future
    `rejected` becomes a boost."""
    return TRUST_WEIGHTS.get(state, 0.0)
