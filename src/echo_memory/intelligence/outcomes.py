"""Turning a validated event into graph writes, through the one write path.

Everything here goes through `write_episode`. Not because of layering taste:
`write_episode` owns dedup, supersession, embedding, attestation, the audit
entry, the per-scope lock and the refusal to store a fact with no author.
Raw Cypher from here would get none of those, and the ones that would be
missed first are the quiet ones. A fact written around the write path carries
no attestation, so nothing afterwards can tell that its text was changed; and
it is not restatement-checked, so a CI system reporting the same deploy every
night would accumulate a version of one sentence per night, which this store
has already done to a customer edge forty-seven times.

## Nothing here reads a sentence

The payload names its entities. There is no extraction, no model, and no
inference about what a report "means" - the reporter knows what it deployed
and says so in fields. That is the same contract `write_episode` has with an
agent, applied to a machine, and it is what keeps the server free of a model
on the write path.

The consequence is that a missing field is an error rather than something to
guess at, and the error names the field. A reporter is a program: it will
keep sending exactly what it sent until somebody reads a message saying what
was wrong with it.

## An unknown event type raises

There is no default branch and there must not be. A default that recorded an
unrecognised event "as a fact" would turn this from an outcome channel into an
arbitrary write endpoint: anything a reporter could name would become a node
and an edge, which is precisely the authority the envelope exists to withhold.
`envelope.parse` already refuses a type outside EVENT_TYPES, so reaching the
raise here means a type was added to that set and not to this dispatch - and
failing loudly is how that gets noticed in the pull request rather than in a
customer's graph.
"""

from __future__ import annotations

import importlib
from typing import Any

from echo_memory.events import envelope as envelope_module
from echo_memory.infra.db import GRAPH_NAME as GRAPH
from echo_memory.infra.logging import get_logger
from echo_memory.infra.project import UNKNOWN as PROJECT_UNKNOWN
from echo_memory.ingestion.write_episode import write_episode
from echo_memory.intelligence import vocabulary
from echo_memory.intelligence.naming import outcome_node_name
from echo_memory.trust import states as trust_states

_logger = get_logger("outcomes")

#: Confidence on every fact written here. `extracted` rather than `inferred`,
#: and the distinction is the one write_episode already draws: confidence says
#: how the writer came by the claim, not whether the claim is true. A deploy
#: reporter read "the deploy finished" off its own exit code - that is as
#: extracted as a claim gets. Whether the deploy was a good idea is the
#: outcome's SIGN, which is a separate axis, and whether the decision behind it
#: was right is a lesson, which is derived at read time and never written.
CONFIDENCE = "extracted"

#: The event types that produce an outcome node. Three names for one shape,
#: because a reporter should be able to say what it actually is: a CI system
#: sending `test.completed` does not have to pretend it is making a general
#: observation, and the three stay distinguishable in the inbox afterwards.
OUTCOME_EVENTS = frozenset({
    envelope_module.OUTCOME_OBSERVED,
    envelope_module.DEPLOYMENT_COMPLETED,
    envelope_module.TEST_COMPLETED,
})


class OutcomeError(Exception):
    """An event was well formed and cannot be applied: a field the handler
    needs is missing, a sign is not a sign, or a capability it requires was not
    granted. Distinct from EnvelopeError, which is about the door - this is
    about the write, and it is what the drain records as the rejection reason.
    """


def _required(payload: dict, event_type: str, field: str) -> str:
    value = payload.get(field)
    text = str(value).strip() if value is not None else ""
    if not text:
        raise OutcomeError(
            f"{event_type} needs payload.{field}, which was "
            f"{value!r}. This path extracts nothing: the reporter names its "
            f"entities, because the server does not read sentences."
        )
    return text


def _session_id(envelope: envelope_module.Envelope) -> str:
    """The session every fact from this event is attributed to.

    Built from the event id rather than from the run, so `echo-memory why` on a
    fact leads back to the exact row in the inbox that produced it - including
    the envelope, including the reporter, including whether it was retried.
    A run id would group several events and lose which one wrote this fact.
    """
    return f"event:{envelope.event_id}"


def _node_id(conn, group_id: str, name: str) -> str | None:
    """The node of this name in this scope, or None.

    Read back rather than assumed. `write_episode` reports edges and says
    nothing about nodes, and a registration-only episode reports `stored:
    false` by design - so for the one event type whose whole content is a node,
    the only way to know it exists is to look. This codebase has had a rollback
    not undo a commit and an exception not undo a delete; reading back after a
    step is the habit that came out of both.
    """
    row = conn.execute(
        f"""SELECT id::text FROM {GRAPH}."Node"
             WHERE (properties ->> '"group_id"'::agtype) = %s
               AND (properties ->> '"name"'::agtype) = %s
             LIMIT 1""",
        (group_id, name),
    ).fetchone()
    return str(row[0]) if row else None


def _store(
    conn,
    group_id: str,
    envelope: envelope_module.Envelope,
    entities: list[dict],
    facts: list[dict],
    embedder,
) -> dict:
    """One write_episode call, with its answer actually checked.

    `assume_new=True`, and that needs justifying because the deferral it opts
    out of exists for a good reason. An ambiguous mention is held so a caller
    can say which existing node it meant. A CI runner cannot answer that
    question - there is nobody to ask and no second call coming - so a
    deferral on this path is not a question, it is a silent drop, and the
    reporter would get `stored: false` forever with no way to make progress.

    Saying "new" is safe because it is not actually an override. Resolution
    checks for an exact name match first and lets the match win, with an audit
    entry saying so, precisely because name equality is this system's own
    definition of entity identity. So an action reported against a decision
    that already exists attaches to that decision; one reported against a
    decision nobody recorded creates it.

    Then the result is checked, because a write that returns is not a write
    that stored. Three paths end with no edges and a well formed response -
    refused, deferred, unchanged - and a customer loading 26,633 facts read
    all three as success and stored 1,650. Here:

        stored        applied
        unchanged     applied. The reporter re-sent an event the graph already
                      holds word for word, which is what idempotency looks
                      like from this end, and retrying will not change it.
        no_facts      applied, and ONLY for an episode that deliberately
                      asserted none. decision.recorded is that episode.
        anything else raises, so the drain marks the event rejected with the
                      reason instead of recording that it applied.
    """
    result = write_episode(
        conn,
        group_id,
        _session_id(envelope),
        entities,
        facts,
        None,
        embedder,
        project=str(envelope.payload.get("project") or PROJECT_UNKNOWN),
        # The reporter, bound to the credential by the caller that parsed the
        # envelope and never read out of the payload. write_episode refuses a
        # fact with no author outright; an outcome with no reporter cannot be
        # weighed, which is the same rule one layer up.
        agent_id=envelope.actor.agent,
        assume_new=True,
    )
    if result.get("stored"):
        return result
    reason = result.get("not_stored")
    if reason == "unchanged" or (reason == "no_facts" and not facts):
        return result
    raise OutcomeError(
        f"{envelope.event_type} stored nothing ({reason}): "
        f"{result.get('not_stored_detail') or result.get('error')}"
    )


def _decision(conn, group_id, envelope, embedder) -> dict:
    """A decision node, and nothing else.

    No fact, because a fact needs two entities and a decision on its own is
    one. The decision's prose arrives later on the edges that hang off it - the
    action taken under it, the outcome observed after it - and those are also
    the only things a lesson is ever about. A node asserting a decision exists
    is the whole content of this event, and `write_episode` honours an episode
    with no facts as exactly that act.
    """
    name = _required(envelope.payload, envelope.event_type, "decision")
    result = _store(
        conn, group_id, envelope,
        [{"name": name, "type": vocabulary.DECISION}],
        [],
        embedder,
    )
    node_id = _node_id(conn, group_id, name)
    if node_id is None:
        raise OutcomeError(
            f"decision {name!r} is not in the graph after writing it. The "
            f"episode reported {result.get('not_stored')!r}; nothing was stored."
        )
    return {"edge_ids": [], "node_ids": [node_id], "decision": name}


def _action(conn, group_id, envelope, embedder) -> dict:
    """An action node, and the edge to the decision it was taken under.

    Direction: the action is the source and the decision is the target, with
    `causal_hint=enabled_by`. `enabled_by` is in write_episode's
    CAUSE_IS_TARGET, so that puts the decision at the cause end, which is
    where it belongs - the decision enabled the action, not the reverse. Get
    this backwards and `trace_cause` walks the chain the wrong way round and
    reports the action as the reason for the decision.

    An action is kept distinct from its decision because one decision usually
    has several, and because an action can fail without the decision being
    wrong. Collapsing them would make a flaky deploy script read as a bad
    architectural call.
    """
    payload = envelope.payload
    action = _required(payload, envelope.event_type, "action")
    decision = _required(payload, envelope.event_type, "decision")
    detail = str(payload.get("detail") or "").strip()
    text = f"{action} was taken under the decision {decision}."
    if detail:
        text = f"{text} {detail}"
    result = _store(
        conn, group_id, envelope,
        [
            {"name": action, "type": vocabulary.ACTION},
            {"name": decision, "type": vocabulary.DECISION},
        ],
        [{
            "source": action,
            "target": decision,
            "relation_type": vocabulary.TAKEN_UNDER,
            "fact": text,
            "confidence": CONFIDENCE,
            "causal_hint": "enabled_by",
        }],
        embedder,
    )
    return {
        "edge_ids": list(result.get("edges_created") or []),
        "node_ids": [],
        "decision": decision,
        "action": action,
    }


def _outcome(conn, group_id, envelope, embedder) -> dict:
    """An outcome node, and the edge from the decision to it.

    Direction: the decision is the source with `causal_hint=led_to`, which is
    in CAUSE_IS_SOURCE, so the decision is the cause and the outcome is the
    effect. "A led_to B" and "B caused_by A" are the same claim written from
    opposite ends and write_episode folds them together, so this reads
    correctly from either direction of the walk.

    The sign goes in two places and that is deliberate. In the fact text, for
    whoever reads the fact. And in the outcome node's NAME, which is the
    structural copy the lesson layer counts - see naming.py for why: node
    names are unique per scope and edge identity is (source, target,
    relation_type), so a success and a later failure of the same named thing
    would otherwise be one edge, and the second report would supersede the
    first. Two real observations, one survivor, and the supersession would
    look like a correction rather than the loss of a data point.
    """
    payload = envelope.payload
    decision = _required(payload, envelope.event_type, "decision")
    subject = _required(payload, envelope.event_type, "outcome")
    sign = _required(payload, envelope.event_type, "sign")
    if sign not in vocabulary.OUTCOME_SIGNS:
        raise OutcomeError(
            f"payload.sign must be one of {list(vocabulary.OUTCOME_SIGNS)}, got "
            f"{sign!r}. Nothing here infers it: an outcome whose sign was "
            f"guessed would weigh exactly like one that was reported."
        )
    node_name = outcome_node_name(subject, sign)
    detail = str(payload.get("detail") or "").strip()
    text = f"{decision} led to {subject}: {sign}."
    if detail:
        text = f"{text} {detail}"
    result = _store(
        conn, group_id, envelope,
        [
            {"name": decision, "type": vocabulary.DECISION},
            {"name": node_name, "type": vocabulary.OUTCOME},
        ],
        [{
            "source": decision,
            "target": node_name,
            "relation_type": vocabulary.OBSERVED_ON,
            "fact": text,
            "confidence": CONFIDENCE,
            "causal_hint": "led_to",
        }],
        embedder,
    )
    return {
        "edge_ids": list(result.get("edges_created") or []),
        "node_ids": [],
        "decision": decision,
        "outcome": node_name,
        "sign": sign,
    }


def _trust(module: str):
    """Stream 4's trust package, imported at call time.

    Lazily, because trust and learning land separately and an import at module
    scope would make every outcome event fail on a build that has one and not
    the other. The error says the package is absent rather than surfacing an
    ImportError from three frames down, because the answer - upgrade, or stop
    sending this event type - depends on knowing it is missing rather than
    broken.

    The package is present or absent as a unit: `echo_memory/trust/__init__.py`
    imports every submodule, so asking for one of them loads all of them. That
    is why this catches ImportError around the whole import rather than trying
    to report which submodule was unavailable.
    """
    try:
        return importlib.import_module(f"echo_memory.trust.{module}")
    except ImportError as e:
        raise OutcomeError(
            f"this build has no echo_memory.trust.{module}, so evidence and "
            f"verification events cannot be applied. Reporting events "
            f"(decisions, actions, outcomes) are unaffected."
        ) from e


def _evidence(conn, group_id, envelope, embedder) -> dict:
    """Attach an artefact to an existing fact. Writes no fact of its own.

    Evidence is not proof and this deliberately cannot make it one, in a
    stronger sense than it first appears. Attaching evidence does NOT move a
    fact to `evidence_backed`: that state is recorded by a verifier like any
    other, so a fact with five evidence rows still reads `unverified` until
    somebody says the evidence was sufficient.

    That is the mechanism rather than an oversight, and it is the same
    separation `REQUIRES_VERIFY` draws one layer up. If attaching an artefact
    promoted a fact's trust state, a CI reporter could attach its own deploy
    log to its own outcome and thereby admit that outcome into lesson support -
    which is exactly the elevation a reporter credential must not be able to
    perform. Here it records what it saw, and somebody else weighs it.
    """
    del embedder  # no fact is written here, so nothing is embedded
    payload = envelope.payload
    fact_id = _required(payload, envelope.event_type, "fact_id")
    kind = _required(payload, envelope.event_type, "kind")
    # Required here although Stream 4 has it optional. Evidence with no
    # artefact to point at is an assertion wearing the word evidence, and the
    # whole value of an evidence row is that somebody can go and look.
    uri = _required(payload, envelope.event_type, "uri")
    detail = payload.get("detail")
    if detail is not None and not isinstance(detail, dict):
        raise OutcomeError(
            f"payload.detail must be an object, got {type(detail).__name__}. "
            f"It is stored structured so a reader can query it; a sentence "
            f"belongs in the fact, not in the evidence row."
        )
    evidence = _trust("evidence")
    if kind not in evidence.KINDS:
        raise OutcomeError(
            f"payload.kind must be one of {sorted(evidence.KINDS)}, got "
            f"{kind!r}. A kind nothing recognises cannot be weighed."
        )
    # Returns the id as a plain string, not a result envelope.
    evidence_id = evidence.record(
        conn,
        group_id,
        kind,
        uri=uri,
        detail=detail,
        reporter_agent=envelope.actor.agent,
        reporter_run=envelope.actor.run_id,
        # When the artefact was produced, from the envelope rather than from
        # now(). An evidence row stamped at drain time would date a CI log to
        # whenever the queue got round to it.
        occurred_at=envelope.occurred_at,
        content_digest=payload.get("content_digest"),
    )
    evidence.link(conn, group_id, fact_id, evidence_id)
    return {"edge_ids": [], "node_ids": [], "fact_id": fact_id,
            "evidence_id": evidence_id}


def _verified(conn, group_id, envelope, embedder) -> dict:
    """Record that an authorised verifier checked a claim.

    The only state that can remove a fact from an answer is one a verifier set
    on purpose, so this is the one handler that can change what retrieval
    returns rather than only its order. That is why it is gated twice over in
    `apply` and why the gate is a required keyword rather than a default.
    """
    del embedder
    payload = envelope.payload
    fact_id = _required(payload, envelope.event_type, "fact_id")
    state = _required(payload, envelope.event_type, "state")
    method = _required(payload, envelope.event_type, "method")
    # Only the three states a verifier sets deliberately. The advisory ones -
    # unverified, evidence_backed, stale - are computed from what the store can
    # see, and an event channel able to assert them would be a way to mark a
    # thousand facts stale, which is a denial of service on your own memory
    # dressed as a verification.
    if state not in trust_states.AUTHORITATIVE:
        raise OutcomeError(
            f"payload.state must be one of {sorted(trust_states.AUTHORITATIVE)}, "
            f"got {state!r}. The advisory states are computed, not asserted."
        )
    kind = str(payload.get("verifier_kind") or trust_states.SERVICE)
    if kind not in trust_states.VERIFIER_KINDS:
        raise OutcomeError(
            f"payload.verifier_kind must be one of "
            f"{list(trust_states.VERIFIER_KINDS)}, got {kind!r}"
        )
    evidence_ids = payload.get("evidence_ids") or ()
    if not isinstance(evidence_ids, (list, tuple)):
        raise OutcomeError(
            f"payload.evidence_ids must be a list, got {type(evidence_ids).__name__}"
        )
    verify = _trust("verify")
    # Returns the fact_verification row id as an int, not a result envelope.
    # `verifier_id` is the credential, which is the reporter's agent key: a
    # verification nobody can be identified by is a verification nobody can be
    # held to, and the same rule already applies to a fact's author.
    row_id = verify.record(
        conn,
        group_id,
        fact_id,
        state,
        verifier_kind=kind,
        verifier_id=envelope.actor.agent,
        method=method,
        policy_id=payload.get("policy_id"),
        evidence_ids=tuple(str(e) for e in evidence_ids),
        # Passed through, because a check that cannot lapse is a check that
        # stays true after it stops being true. An expired `verified` reads
        # back as `stale`, which drops the outcome out of lesson support with
        # no write having happened - the behaviour a lapsing check should have.
        expires_at=payload.get("expires_at"),
    )
    return {"edge_ids": [], "node_ids": [], "fact_id": fact_id, "state": state,
            "verification_id": row_id}


#: One handler per event type, and no default. Built as a mapping rather than
#: an if-chain so that a type in EVENT_TYPES with no handler is visible as a
#: missing key here instead of silently falling through to a last branch.
_HANDLERS: dict[str, Any] = {
    envelope_module.DECISION_RECORDED: _decision,
    envelope_module.ACTION_EXECUTED: _action,
    envelope_module.EVIDENCE_ATTACHED: _evidence,
    envelope_module.CLAIM_VERIFIED: _verified,
    **{event: _outcome for event in OUTCOME_EVENTS},
}


def apply(
    conn,
    group_id: str,
    envelope: envelope_module.Envelope,
    embedder,
    *,
    allow_verify: bool = False,
) -> dict:
    """Apply one validated event to the graph, and say what it wrote.

    `allow_verify` is the capability gate for `claim.verified`, and it is a
    required keyword defaulting to False so that a caller has to grant it
    explicitly. Two checks, not one: the event type must be in
    `events.envelope.REQUIRES_VERIFY`, and the caller must have passed the
    flag. Either alone would be a weaker thing than it looks - the type set on
    its own says which events need the capability without anybody holding it,
    and a flag on its own would let a future event type inherit verification
    authority by being added to the wrong set.

    What this protects is the separation that makes a reporter credential safe
    to hand out. A reporter can attach evidence all day; it cannot say the
    evidence was sufficient. Collapse that and a CI token can mark its own
    deploys verified, and `verified` is the only state the lesson layer counts
    as causal.

    Returns `edge_ids` for what the drain should record as applied. Raises
    OutcomeError for anything it cannot do, which the drain turns into a
    `rejected` row carrying the reason.
    """
    handler = _HANDLERS.get(envelope.event_type)
    if handler is None:
        raise OutcomeError(
            f"no handler for event_type {envelope.event_type!r}. This is not "
            f"defaulted to recording it as a fact: a default branch would make "
            f"any name a reporter can invent into a node and an edge, which is "
            f"the authority the envelope exists to withhold."
        )
    if envelope.event_type in envelope_module.REQUIRES_VERIFY and not allow_verify:
        raise OutcomeError(
            f"{envelope.event_type} needs verification authority and this "
            f"caller was not granted it (allow_verify=False). A reporter may "
            f"attach evidence; saying the evidence was sufficient is a "
            f"different credential."
        )

    result = handler(conn, group_id, envelope, embedder)
    result.setdefault("edge_ids", [])
    result["event_id"] = envelope.event_id
    result["event_type"] = envelope.event_type
    _logger.info(
        "event_applied",
        extra={
            "group_id": group_id,
            "event_id": envelope.event_id,
            "event_type": envelope.event_type,
            "edges": len(result["edge_ids"]),
        },
    )
    return result
