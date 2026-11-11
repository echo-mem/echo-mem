"""Which claims may leave this deployment, and what is stripped from the ones
that do.

An export hands data to another organisation and cannot be undone. So the
question "what is actually in this file" has to be answerable before the file
is handed over, not after, and it has to be answerable by a person rather than
by reading the exporter's source. That is what `audit` is for: it reports what
a policy removed, counted by the rule that removed it, over the same facts
`build` is about to serialise.

## Redaction runs before signing, always

The signature covers the redacted content. A recipient who verifies it is
therefore verifying the bundle that was actually sent.

Signing first and redacting afterwards would invert that. The signature would
attest to a bundle nobody received, every recipient's verification would fail,
and the only way to make them pass again would be to re-sign the redacted
form - at which point the first signature proved nothing. Worse, a middle box
could strip claims from a signed bundle and the recipient could not tell
whether the exporter or the carrier had done it. `bundle.build` enforces the
order by construction: it redacts, then digests, and `sign` is handed the
manifest that already carries that digest.

## What a policy is not

A policy is a rule over structure and text, so it catches what it is told to
catch and nothing else. Two limits worth stating because an exporter will
otherwise assume more:

**`drop_entity_types` matches a string the writer chose.** An entity's `type`
is free text supplied by whoever wrote the fact, not a category this store
validates. Dropping type `person` catches a convention, not a class of data. A
store whose writers used `human`, `employee` or `x` keeps every one of them.

**`drop_matching` is a pattern list, not a classifier.** It catches the shapes
it names - an address, a key with a recognisable prefix - and a person's name
in ordinary prose matches nothing. Anyone reading a clean `audit` as "no
personal data is leaving" has read it wrong; what it says is "none of these
rules fired".

The control that actually bounds an export is the scope it is taken from. A
policy narrows what leaves a scope the operator already chose to send.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: The audit keys for each drop rule, so a caller reading a count does not
#: depend on a string this module spells one way and a test another.
BY_RELATION = "relation"
BY_ENTITY_TYPE = "entity_type"
BY_MATCHING = "matching"
DROP_RULES = (BY_RELATION, BY_ENTITY_TYPE, BY_MATCHING)

#: The audit keys for the two fields a policy may strip from a fact it keeps.
STRIP_AGENT_ID = "agent_id"
STRIP_PROVENANCE = "provenance"
STRIP_RULES = (STRIP_AGENT_ID, STRIP_PROVENANCE)

#: How many dropped claims `audit` keeps the text of, for the operator to read.
PREVIEW = 10


@dataclass(frozen=True)
class Policy:
    """One named set of rules. Frozen, because a policy id is recorded in the
    manifest and in `bundle_registry`, and a policy that could be mutated after
    an export would make that record a lie about what left."""

    policy_id: str

    #: Relation types that never leave, whatever they say. The bluntest rule
    #: and the one an operator reaches for first: a store where `pays` or
    #: `credentials_for` is a relation type can exclude the whole class
    #: without reading a single fact.
    drop_relations: frozenset[str]

    #: Entity types whose facts never leave, at either endpoint. See the
    #: module note: this matches a string the writer chose.
    drop_entity_types: frozenset[str]

    #: Compiled patterns. A fact whose claim matches any of them is dropped
    #: entirely rather than having the match removed, because a claim with a
    #: hole in it is still the same claim and the hole says where to look.
    drop_matching: tuple[re.Pattern[str], ...]

    #: Whether the author leaves with the claim. Off by default everywhere:
    #: `agent_id` identifies a process inside the exporter's organisation and
    #: means nothing to the recipient except as an internal detail.
    keep_agent_id: bool

    #: Whether `provenance` leaves with the claim. It carries the session and
    #: episode the fact was written in, which are internal identifiers, so
    #: this is off unless an operator has a reason.
    keep_provenance: bool


#: Shapes that should not cross an organisational boundary inside a claim.
#:
#: Deliberately short and readable rather than an attempt at coverage, for the
#: reason `retrieval/envelope.py` gives about its own pattern list: a long
#: regex list buys a false sense of completeness. These are the shapes whose
#: presence in an exported fact is almost certainly a mistake.
_SECRETS_AND_CONTACTS = (
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b(sk|rzp|ghp|gho|xoxb|xoxp)[-_][A-Za-z0-9_-]{8,}", re.IGNORECASE),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:Bearer|Authorization:)\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE),
)


POLICIES: dict[str, Policy] = {
    # The claim and nothing about who recorded it. What an operator should
    # reach for when a partner needs the knowledge and has no business
    # knowing how this store is organised internally.
    #
    # `person` is in drop_entity_types because a policy named "minimal" that
    # exports facts about named individuals would be misnamed - and because
    # the names of people are the part of a memory that is personal data
    # under GDPR Article 17 and the DPDP Act, which `erasure.py` exists for.
    # It catches the convention, not the category; see the module note.
    "minimal": Policy(
        policy_id="minimal",
        drop_relations=frozenset(),
        drop_entity_types=frozenset({"person", "people", "individual"}),
        drop_matching=_SECRETS_AND_CONTACTS,
        keep_agent_id=False,
        keep_provenance=False,
    ),
    # Everything the format permits. Not "everything in the scope": the
    # format never carries `group_id` whatever the policy says, because a
    # group id is the exporter's tenancy boundary and means nothing on the
    # other side. That is a rule of the format rather than a choice a policy
    # gets to make, and it lives in `bundle.build`.
    #
    # The secret shapes stay. Nobody intends to export an AWS key, and a
    # policy that permits it because it is named "full" is a policy that
    # exports one.
    "full": Policy(
        policy_id="full",
        drop_relations=frozenset(),
        drop_entity_types=frozenset(),
        drop_matching=_SECRETS_AND_CONTACTS,
        keep_agent_id=True,
        keep_provenance=True,
    ),
}

#: What `build` applies when a caller names no policy. The restrictive one, so
#: that forgetting to choose cannot export more than choosing would.
DEFAULT_POLICY = "minimal"


def get(policy_id: str | None) -> Policy:
    """A policy by id, or the default. An unknown id is an error rather than a
    fallback: silently exporting under a different policy than the operator
    named is the one failure this module must not have."""
    if policy_id is None:
        return POLICIES[DEFAULT_POLICY]
    try:
        return POLICIES[policy_id]
    except KeyError:
        raise KeyError(
            f"no redaction policy {policy_id!r}; known policies are "
            f"{', '.join(sorted(POLICIES))}"
        ) from None


def _scanned_text(fact: dict) -> str:
    """What `drop_matching` is tested against: the claim as it would be sent.

    The endpoint names are included, not just the fact text. An address or a
    key is as likely to be an entity name as to be inside a sentence - this
    store's entities are frequently identifiers - and a pattern list that read
    only `fact` would pass a bundle whose source node is an email address.
    """
    return "\n".join(
        str(fact.get(key) or "")
        for key in ("fact", "source", "target")
    )


def _decide(policy: Policy, fact: dict) -> tuple[dict | None, str | None, list[str]]:
    """The redacted fact, the rule that dropped it, and the fields stripped.

    One function behind both `apply` and `audit` so that the audit an operator
    reads cannot disagree with what the export does. Two implementations of
    this decision would drift, and the direction they would drift is an audit
    that under-reports.
    """
    if fact.get("relation_type") in policy.drop_relations:
        return None, BY_RELATION, []

    endpoints = {fact.get("source_type"), fact.get("target_type")}
    if endpoints & policy.drop_entity_types:
        return None, BY_ENTITY_TYPE, []

    text = _scanned_text(fact)
    if any(pattern.search(text) for pattern in policy.drop_matching):
        return None, BY_MATCHING, []

    kept = dict(fact)
    stripped = []
    if not policy.keep_agent_id and "agent_id" in kept:
        del kept["agent_id"]
        stripped.append(STRIP_AGENT_ID)
    if not policy.keep_provenance and "provenance" in kept:
        del kept["provenance"]
        stripped.append(STRIP_PROVENANCE)
    return kept, None, stripped


def apply(policy: Policy, fact: dict) -> dict | None:
    """This fact as it would be sent, or None when the policy drops it."""
    kept, _rule, _stripped = _decide(policy, fact)
    return kept


def audit(policy: Policy, facts: list[dict]) -> dict:
    """What this policy removes from these facts, counted by the rule that
    removed it.

    The surface an exporter reads before anything leaves. Counts per rule
    rather than one total, because "11 dropped" and "11 dropped, all of them
    because a pattern matched an address" lead to different decisions - the
    second says the scope has a problem that is not about this export.

    `preview` carries the text of the first few dropped claims. It stays on
    the exporter's side by construction, since nothing in the bundle format
    has a field for it: this is the operator's last look at what they are
    choosing not to send.
    """
    dropped = dict.fromkeys(DROP_RULES, 0)
    stripped = dict.fromkeys(STRIP_RULES, 0)
    relations: set[str] = set()
    entity_types: set[str] = set()
    preview: list[str] = []
    kept_n = 0

    for fact in facts:
        kept, rule, fields = _decide(policy, fact)
        if kept is not None:
            kept_n += 1
            for field in fields:
                stripped[field] += 1
            continue

        dropped[rule or BY_MATCHING] += 1
        if rule == BY_RELATION:
            relations.add(str(fact.get("relation_type")))
        elif rule == BY_ENTITY_TYPE:
            entity_types |= {
                str(t) for t in (fact.get("source_type"), fact.get("target_type"))
                if t in policy.drop_entity_types
            }
        if len(preview) < PREVIEW:
            preview.append(str(fact.get("fact") or ""))

    return {
        "policy_id": policy.policy_id,
        "n_in": len(facts),
        "n_out": kept_n,
        "dropped": dropped,
        "n_dropped": sum(dropped.values()),
        "stripped": stripped,
        "relations_dropped": sorted(relations),
        "entity_types_dropped": sorted(entity_types),
        "preview": preview,
    }
