"""Trust and verification: whether a claim was ever checked, and by whom.

Four modules, in the order they depend on each other:

    states      the six states, which of them retrieval must obey, and what
                each is worth as a ranked list
    evidence    the artefact behind a claim, recorded apart from the claim
    verify      the append-only record of who checked what, and the read path
                that turns it back into one state per fact
    vattest     a digest per verification, chained, so no edit to one is silent

The names are re-exported because callers want `trust.state_of` and
`trust.record_verification`, not a tour of the layout. `record` is the one
name both `evidence` and `verify` wanted, so it is exported twice, spelled
out: a single `record` here would make the two most consequential writes in
this package indistinguishable at the call site, and attaching a log to a
fact is not the same act as declaring the fact verified.
"""

from echo_memory.trust.evidence import EvidenceError
from echo_memory.trust.evidence import count_for as evidence_count_for
from echo_memory.trust.evidence import for_fact as evidence_for_fact
from echo_memory.trust.evidence import link as link_evidence
from echo_memory.trust.evidence import record as record_evidence
from echo_memory.trust.states import (
    ADVISORY,
    ALL_STATES,
    AUTHORITATIVE,
    DISPUTED,
    EVIDENCE_BACKED,
    REJECTED,
    STALE,
    SUPPRESSED,
    TRUST_WEIGHTS,
    UNVERIFIED,
    VERIFIED,
    VERIFIER_KINDS,
    is_authoritative,
    suppresses,
    weight,
)
from echo_memory.trust.vattest import VAttestError
from echo_memory.trust.vattest import head as chain_head
from echo_memory.trust.vattest import report as chain_report
from echo_memory.trust.verify import (
    VerificationError,
    ranked,
    state_of,
    states_of,
    summary,
    suppressed,
)
from echo_memory.trust.verify import record as record_verification

__all__ = [
    "ADVISORY",
    "ALL_STATES",
    "AUTHORITATIVE",
    "DISPUTED",
    "EVIDENCE_BACKED",
    "REJECTED",
    "STALE",
    "SUPPRESSED",
    "TRUST_WEIGHTS",
    "UNVERIFIED",
    "VERIFIED",
    "VERIFIER_KINDS",
    "EvidenceError",
    "VAttestError",
    "VerificationError",
    "chain_head",
    "chain_report",
    "evidence_count_for",
    "evidence_for_fact",
    "is_authoritative",
    "link_evidence",
    "ranked",
    "record_evidence",
    "record_verification",
    "state_of",
    "states_of",
    "summary",
    "suppressed",
    "suppresses",
    "weight",
]
