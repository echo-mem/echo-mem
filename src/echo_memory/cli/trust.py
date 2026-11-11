"""`echo-memory trust`: the operator surface for verification and evidence.

`trust/verify.py` has been able to record a check since migration 0032, and
until this file there was no way to call it. No CLI command, no HTTP route, no
console page. The one reachable surface named `verify` is `echo-memory verify`,
which checks the ATTESTATION chain and has nothing to do with trust states, so
an operator who went looking found a command with the right name doing a
different job.

The consequence was not cosmetic. `intelligence/lessons.py` counts only
outcomes whose state is `verified` or `evidence_backed`, and neither is ever
derived - `evidence_backed` is recorded by a verifier like any other state, on
purpose, so that a CI reporter attaching its own deploy log cannot promote its
own outcome. With nothing able to record either one, every scope in every real
deployment held exactly zero supporting outcomes, and `lessons.candidates`
returned an empty list that looked like "your decisions disagree with
themselves" and actually meant "nothing in this product can set the state this
query filters on".

## Deliberately not MCP tools

`cli/forget.py` makes the argument and `cli/exchange.py` repeats it; this is
the third instance and the strongest. Verification is the only thing in the
store that decides WHICH facts a read returns rather than the order they come
back in, so a tool that could set a state would let any agent holding a key
mark its own claims `verified` and its inconvenient ones `rejected`. That is
precisely the elevation `events.envelope.REQUIRES_VERIFY` exists to deny, and
denying it on the event channel while publishing a tool for it would be
pointless. `tests/integration/test_erasure.py` reads the live tool registry and
fails if any of these names is ever registered.

## No --confirm, and a required --verifier instead

Nothing here deletes anything. `fact_verification` is append only, so a wrong
verification is corrected by recording the right one and the mistake stays
readable as history - which is the whole reason the table has no UPDATE path.
A dry run in front of an append would be friction in front of the safe
direction, which `exchange.py` already argues against.

What this gate guards is attribution rather than destruction. `--verifier` is
required for the three states an authorised verifier sets on purpose, and an
empty one is refused rather than stored: a verification nobody can be
identified by is a verification nobody can be held to, and it would still
count towards a lesson. The same rule `write_episode` applies to a fact's
author, applied to the stronger claim.

`dispute` requires it too, although it does not suppress. DISPUTED is in
`states.AUTHORITATIVE`, so it is a judgement a person made about a claim, and
"somebody disputed this" with no somebody is not a judgement.

## The current state is never re-derived here

Every state printed comes from `verify.states_of`. This file reads
`fact_verification` only to show the HISTORY of a fact, never to work out what
state that history adds up to. A second copy of that derivation is how a
reader goes blind, which this codebase has paid for twice, and the expiry rule
in particular - a lapsed `verified` reads as `stale` while the row is left
alone - is exactly the kind of thing a second implementation gets wrong
quietly.
"""

from __future__ import annotations

import json
from datetime import datetime

from echo_memory.infra.db import GRAPH_NAME as GRAPH
from echo_memory.intelligence import lessons as lessons_module
from echo_memory.intelligence import vocabulary
from echo_memory.trust import evidence as evidence_module
from echo_memory.trust import states, verify

#: How many facts a listing prints before summarising. Same number and the
#: same reason as `forget.PREVIEW`: enough to see what the scope holds, short
#: enough to read in a terminal.
DEFAULT_LIMIT = 50

#: The states a person records on purpose, mapped to the subcommand that
#: records each. Built from `states.AUTHORITATIVE` rather than typed out, so a
#: seventh state added to that set is a missing subcommand here rather than a
#: state no operator can reach.
_RECORDING = {
    "verify": states.VERIFIED,
    "dispute": states.DISPUTED,
    "reject": states.REJECTED,
}


def _scope(args) -> str:
    """Which scope this invocation is about.

    Two spellings reach the same place. `echo-memory --scope shared trust
    show` is the form every other command takes, because `--scope` is declared
    once on the top level parser. `echo-memory trust show --scope shared` is
    the form an operator actually types, because the scope belongs to the
    thing being inspected rather than to the program.

    The subcommand's copy defaults to None rather than to "solo" so the two
    cannot fight. argparse copies a subparser's defaults over the namespace
    after the top level is parsed, so a subcommand default of "solo" would
    silently discard a `--scope shared` typed before the subcommand, and the
    command would report on the wrong scope while looking like it worked.
    """
    return getattr(args, "trust_scope", None) or args.scope


def _live_facts(conn, group_id: str) -> list[tuple[str, str]]:
    """Every live fact in this scope, as (edge_id, text), oldest first.

    Off the edge table's properties rather than through Cypher, the same way
    `forget._preview` reads fact text. `fact` is a scalar string, so it casts;
    the agtype-map problem that forces `exchange/bundle.live_facts` through a
    per-property Cypher projection does not arise here because nothing in this
    listing needs `provenance`.

    `t_invalid IS NULL` is the same predicate `lessons._observations` uses, and
    for the same reason: a superseded fact is history, and showing an operator
    the trust state of a claim the store has already retired would invite them
    to verify it.
    """
    rows = conn.execute(
        f"""SELECT id::text, (properties ->> '"fact"'::agtype)
              FROM {GRAPH}."FACT"
             WHERE (properties ->> '"group_id"'::agtype) = %s
               AND (properties ->> '"t_invalid"'::agtype) IS NULL
             ORDER BY id""",
        (group_id,),
    ).fetchall()
    return [(str(edge_id), text) for edge_id, text in rows]


def _fact_text(conn, group_id: str, edge_id: str) -> str | None:
    """One fact's text, or None if this scope does not hold it.

    Scoped by group_id and not only by id, which is the whole point of asking.
    An edge id is a graphid and a graphid is global to the graph, so a lookup
    by id alone would happily print another tenant's fact to whoever pasted
    the number.
    """
    row = conn.execute(
        f"""SELECT (properties ->> '"fact"'::agtype)
              FROM {GRAPH}."FACT"
             WHERE id = %s::graphid
               AND (properties ->> '"group_id"'::agtype) = %s""",
        (edge_id, group_id),
    ).fetchone()
    return row[0] if row else None


def _history(conn, group_id: str, edge_id: str) -> list[dict]:
    """Every verification ever recorded for this fact, oldest first.

    HISTORY ONLY. The current state is `verify.state_of`'s answer and is not
    computed from these rows anywhere in this file. What they are for is the
    question that an append-only table exists to answer and a single state
    cannot: "verified on the 3rd, disputed on the 9th, verified again on the
    11th" stays readable, and "who changed their mind" has a name against it.

    Ordered ascending, opposite to how `states_of` reads it, because a person
    reads a chain of events forwards and a derivation wants the newest row
    first.
    """
    rows = conn.execute(
        """SELECT id, state, verifier_kind, verifier_id, method, policy_id,
                  evidence_ids, expires_at, detail, verified_at
             FROM public.fact_verification
            WHERE group_id = %s AND edge_id = %s::graphid
            ORDER BY id""",
        (group_id, edge_id),
    ).fetchall()
    return [
        {
            "id": r[0], "state": r[1], "verifier_kind": r[2],
            "verifier_id": r[3], "method": r[4], "policy_id": r[5],
            "evidence_ids": list(r[6] or ()), "expires_at": r[7],
            "detail": r[8], "verified_at": r[9],
        }
        for r in rows
    ]


def _when(value) -> str:
    return value.isoformat(timespec="seconds") if isinstance(value, datetime) else str(value)


def _parse_expiry(raw: str) -> datetime:
    """`--expires`, with a timezone offset required.

    The same rule `events/envelope._parse_when` applies to a reporter's
    `occurred_at`, for the same reason. An operator in IST typing
    `2027-01-01T00:00` would set an expiry five and a half hours off, and
    `verify.states_of` compares `expires_at` against the DATABASE's clock - so
    the fact would flip between `verified` and `stale` at a moment nobody
    chose. Twenty four milliseconds of container skew has already turned three
    days into four in this project; hours of it would do worse.

    `Z` is accepted, because that is what every machine that writes an
    instant emits. `fromisoformat` has handled it since 3.11, which is this
    package's floor, so there is no translation step here to get wrong.
    """
    try:
        when = datetime.fromisoformat(raw)
    except ValueError as e:
        raise ValueError(f"--expires is not an ISO 8601 instant: {raw!r}") from e
    if when.tzinfo is None:
        raise ValueError(
            f"--expires must carry a timezone offset, e.g. "
            f"{raw}+05:30 or {raw}Z. Expiry is judged against the database's "
            f"clock, so a naive instant expires at a moment nobody chose."
        )
    return when


# --- show ----------------------------------------------------------------


def _show(args, config, conn) -> int:
    group_id = config.group_id(_scope(args))
    text = _fact_text(conn, group_id, args.fact)
    if text is None:
        print(f"No live fact {args.fact} in {group_id}.")
        return 1

    state = verify.state_of(conn, group_id, args.fact)
    print(f"{args.fact}  {text}\n")
    print(f"  state: {state}")
    if state in states.ADVISORY:
        print("         advisory, so it only reorders an answer")
    else:
        print("         authoritative, so it decides what an answer contains")
    if states.suppresses(state):
        # Said plainly, because this is the one state whose effect an operator
        # cannot see by reading anything else: the fact is still in the store
        # and still in its history, and every read silently omits it.
        print("         and this state removes the fact from an answer entirely")

    history = _history(conn, group_id, args.fact)
    if not history:
        print(
            "\nNothing has ever checked this fact. Every fact starts here, "
            "including\nevery fact written before verification existed."
        )
    else:
        print(f"\n{len(history)} verification(s), oldest first:")
        for row in history:
            print(
                f"  {_when(row['verified_at'])}  {row['state']:<15} "
                f"by {row['verifier_id']} ({row['verifier_kind']})"
            )
            print(f"        method: {row['method']}")
            if row["policy_id"]:
                print(f"        policy: {row['policy_id']}")
            if row["evidence_ids"]:
                print(f"        evidence: {', '.join(row['evidence_ids'])}")
            if row["expires_at"]:
                print(f"        expires: {_when(row['expires_at'])}")
            if row["detail"]:
                print(f"        {row['detail']}")
        # Said out loud rather than left to be inferred from the dates. The
        # row still says `verified` and the state says `stale`, and an
        # operator comparing the two without this line reads it as a bug.
        if state == states.STALE:
            print(
                "\nThe newest row says verified and its expiry has passed, so "
                "the fact reads\nas stale. The row is left exactly as it was: "
                "a lapsed check is not a\nrefutation, and rewriting it would "
                "destroy the evidence the check ever passed."
            )

    artefacts = evidence_module.for_fact(conn, group_id, args.fact)
    if not artefacts:
        print("\nNo evidence is linked to this fact.")
        return 0
    print(f"\n{len(artefacts)} evidence row(s), most recently received first:")
    for item in artefacts:
        print(
            f"  {item['evidence_id']}  {item['kind']:<17} "
            f"reported by {item['reporter_agent']}"
        )
        if item["uri"]:
            print(f"        {item['uri']}")
        if item["content_digest"]:
            print(f"        digest at the time: {item['content_digest']}")
        # Said here rather than only in the returned dict, because this is the
        # surface the warning is FOR: the operator reading these rows is the
        # decision maker an injected sentence is aimed at, and a flag nobody
        # prints is a flag nobody sees. Named by field, since a URL and a
        # summary line are read very differently.
        if item["instruction_like"]:
            print(
                f"        WARNING: {', '.join(item['instruction_like'])} "
                f"read(s) like an instruction aimed at a model, not like a "
                f"description of an artefact. Treat it as the reporter's text, "
                f"not as guidance."
            )
    if any(item["instruction_like"] for item in artefacts):
        print(
            "\nAt least one artefact carries text shaped like an instruction. "
            "It is stored\nand shown verbatim on purpose - an attacker's URL "
            "is evidence of the attack,\nand truncating it would destroy what "
            "you need to see. Labelling is the third\nlayer and not the one "
            "that prevents injection; what makes this safe to read is\nthat "
            "you are reading it, rather than a model acting on it."
        )
    if state == states.UNVERIFIED:
        print(
            "\nEvidence does not move a fact out of unverified, and that is "
            "the mechanism\nrather than an oversight: a reporter that could "
            "promote its own claim by\nattaching its own artefact would make "
            "`verified` mean nothing. Somebody has\nto say the evidence was "
            "sufficient."
        )
    return 0


# --- list ----------------------------------------------------------------


def _list(args, config, conn) -> int:
    group_id = config.group_id(_scope(args))
    facts = _live_facts(conn, group_id)
    if not facts:
        print(f"No live facts in {group_id}.")
        return 0

    # One batched call for the whole scope. `states_of` returns a state for
    # every id passed in, so there is no default for this file to remember and
    # get wrong somewhere the tests do not look.
    current = verify.states_of(conn, group_id, [edge_id for edge_id, _ in facts])

    counts = dict.fromkeys(states.ALL_STATES, 0)
    for state in current.values():
        counts[state] = counts.get(state, 0) + 1
    print(f"{len(facts)} live fact(s) in {group_id}:")
    print("  " + "  ".join(f"{state}: {counts[state]}" for state in states.ALL_STATES))

    if args.state:
        wanted = [(e, t) for e, t in facts if current[e] == args.state]
        heading = f"\n{len(wanted)} fact(s) in state {args.state!r}"
    else:
        # Everything that has ever been checked. Unverified is excluded by
        # default because it is almost every fact in any real store, so
        # printing it unasked would bury the handful that were checked under
        # the thousands that were not. `--state unverified` asks for them.
        wanted = [(e, t) for e, t in facts if current[e] != states.UNVERIFIED]
        heading = (
            f"\n{len(wanted)} fact(s) have been checked "
            f"(pass --state unverified for the rest)"
        )
    print(heading + (":" if wanted else "."))
    for edge_id, text in wanted[: args.limit]:
        print(f"  {edge_id}  [{current[edge_id]}]  {text}")
    if len(wanted) > args.limit:
        print(f"  ... and {len(wanted) - args.limit} more (--limit)")
    return 0


# --- verify / dispute / reject -------------------------------------------


def _record(args, config, conn) -> int:
    """Append one verification in the state this subcommand names.

    One function for the three authoritative states rather than three, because
    they take identical arguments and differ only in a constant. Three copies
    would be three places for the `--verifier` check to be forgotten from, and
    the one it would be forgotten from is `dispute`, which is the one the
    brief did not ask for it on.
    """
    state = _RECORDING[args.trust_command]
    group_id = config.group_id(_scope(args))

    # Checked before anything is read, because the refusal is about the
    # request rather than about the store. argparse's required=True is
    # satisfied by an empty string, so --verifier "" reaches here.
    verifier = (args.verifier or "").strip()
    if not verifier:
        print(
            "error: --verifier must name who is making this call. A "
            "verification with\nnobody recorded against it is one nobody can "
            "be held to, and it would still\ncount towards a lesson."
        )
        return 2
    method = (args.method or "").strip()
    if not method:
        print(
            "error: --method must say how this was checked. The method is "
            "what a later\nreader uses to decide whether to re-run the check, "
            "and `verified` with no\nmethod is an assertion wearing the word."
        )
        return 2

    if _fact_text(conn, group_id, args.fact) is None:
        print(f"error: no live fact {args.fact} in {group_id}.")
        return 1

    expires = None
    if args.expires:
        try:
            expires = _parse_expiry(args.expires)
        except ValueError as e:
            print(f"error: {e}")
            return 2
        if state is not states.VERIFIED:
            # Refused rather than ignored. `expires_at` is only ever read for
            # a `verified` row - see verify.states_of - so accepting it here
            # would store a field that does nothing and let an operator
            # believe a refusal lapses on a clock, which is a different
            # decision and not one this command is entitled to make.
            print(
                f"error: --expires applies only to a verification. "
                f"{state} does not lapse:\nexpiry says how long a positive "
                f"check is good for, and applying it to a\nrefusal would make "
                f"suppression quietly stop on a timer."
            )
            return 2

    try:
        verification_id = verify.record(
            conn, group_id, args.fact, state,
            verifier_kind=args.kind,
            verifier_id=verifier,
            method=method,
            policy_id=args.policy,
            evidence_ids=tuple(args.evidence or ()),
            expires_at=expires,
            detail=args.detail,
        )
    except verify.VerificationError as e:
        print(f"error: {e}")
        return 1

    print(
        f"Recorded {state} on {args.fact} in {group_id}.\n"
        f"  by {verifier} ({args.kind})\n"
        f"  method: {method}"
    )
    if args.policy:
        print(f"  policy: {args.policy}")
    if args.evidence:
        print(f"  evidence: {', '.join(args.evidence)}")
    if expires:
        print(f"  expires: {_when(expires)}, after which it reads as {states.STALE}")
    print(f"  verification {verification_id}, attested in the same transaction")

    if state == states.REJECTED:
        print(
            "\nThis is the one state that removes a fact from an answer. It "
            "is still in\nthe store and still in its history; reads will not "
            "return it."
        )
    elif state == states.DISPUTED:
        print(
            "\nDisputed facts are still returned, with the dispute visible. A "
            "claim a\nreader can see is contested is worth more than a gap "
            "they cannot."
        )
    else:
        print(
            "\nThis fact can now support a lesson. Nothing is recomputed: "
            "lessons are\nderived at read time, so `echo-memory trust "
            "lessons` already reflects it."
        )
    # Nothing updates and nothing deletes here, so a wrong call is corrected
    # by making the right one. Said at the point the mistake would be noticed.
    print("To change this, record another verification. Nothing here overwrites.")
    return 0


# --- evidence ------------------------------------------------------------


def _evidence(args, config, conn) -> int:
    group_id = config.group_id(_scope(args))

    detail = None
    if args.detail:
        try:
            detail = json.loads(args.detail)
        except json.JSONDecodeError as e:
            print(f"error: --detail is not valid JSON: {e}")
            return 2
        if not isinstance(detail, dict):
            print(
                f"error: --detail must be a JSON object, got "
                f"{type(detail).__name__}. It is stored structured so a reader "
                f"can query it;\na sentence belongs in the fact."
            )
            return 2

    if _fact_text(conn, group_id, args.fact) is None:
        print(f"error: no live fact {args.fact} in {group_id}.")
        return 1

    try:
        evidence_id = evidence_module.record(
            conn, group_id, args.kind,
            uri=args.uri,
            detail=detail,
            reporter_agent=args.by,
            occurred_at=args.occurred,
            content_digest=args.digest,
        )
        # Recorded and linked in one command, because an artefact nobody
        # pointed at a fact is a row nothing reads. The two are separate CALLS
        # in the library for the reason evidence.py gives - the test run that
        # confirms a decision arrives after it - and an operator attaching one
        # by hand already has the fact in front of them.
        evidence_module.link(conn, group_id, args.fact, evidence_id)
    except evidence_module.EvidenceError as e:
        print(f"error: {e}")
        return 1

    print(
        f"Recorded {args.kind} evidence {evidence_id} and linked it to "
        f"{args.fact}.\n"
        f"  reported by {args.by}"
    )
    if args.uri:
        print(f"  {args.uri}")
    print(
        f"\nThe fact's state is unchanged. Evidence is not proof: a deploy log "
        f"saying\nthe deploy happened does not establish that the decision "
        f"behind it was right.\nRecord that judgement with `trust verify "
        f"--fact {args.fact} --evidence {evidence_id}`."
    )
    return 0


# --- lessons -------------------------------------------------------------


def _lessons(args, config, conn) -> int:
    """What this scope's outcomes currently support, and why if nothing.

    The empty answer is the whole reason this subcommand exists. `candidates`
    returns a list, and an empty list covers two situations with different
    owners: a scope whose outcomes genuinely disagree, which is the feature
    working, and a scope where nobody has recorded a verification, which is a
    configuration problem nothing in the product reported. Until this file
    existed the second was the state of every deployment, and the empty list
    was the only thing anybody saw.

    The counts come from `lessons`' own helpers rather than from queries
    written again here. They are private, and reaching for them is still the
    right trade: a second copy of "which edges are live decision-to-outcome
    observations" would be a second thing to keep in step with
    `_observations`, and a diagnosis that disagrees with the thing it is
    diagnosing is worse than no diagnosis.
    """
    group_id = config.group_id(_scope(args))
    rows = lessons_module.candidates(conn, group_id)

    if rows:
        print(f"{len(rows)} lesson(s) in {group_id}:\n")
        for row in rows:
            print(f"  {row['lesson']}")
            print(
                f"    basis {row['basis']}, support {row['support']}, "
                f"agreement {row['agreement']}"
            )
            for observation in row["outcomes"]:
                backing = observation["n_evidence"]
                counted = "evidence unknown" if backing is None else f"{backing} evidence row(s)"
                print(
                    f"      {observation['fact_id']}  [{observation['trust']}] "
                    f"{observation['outcome']}: {observation['sign']}, {counted}"
                )
            print()
        return 0

    # Nothing. Say which of the three bars was not cleared.
    decisions = lessons_module._decisions(conn, group_id)
    observations = lessons_module._observations(
        conn, group_id, [node_id for node_id, _ in decisions]
    )
    trust = lessons_module._trust_states(
        conn, group_id, [o["fact_id"] for o in observations]
    )
    supporting = [
        o for o in observations
        if trust.get(o["fact_id"], states.UNVERIFIED) in lessons_module.SUPPORTING_STATES
    ]
    directional = [
        o for o in supporting
        if vocabulary.SIGN_VALUE.get(o["sign"], 0) != 0
    ]
    backing = lessons_module._evidence_counts(
        conn, group_id, [o["fact_id"] for o in observations]
    )

    print(f"No lessons in {group_id}.\n")
    print(f"  {len(decisions)} decision node(s)")
    print(f"  {len(observations)} live outcome observation(s) hanging off them")
    print(
        f"  {len(supporting)} in a supporting state "
        f"({' or '.join(sorted(lessons_module.SUPPORTING_STATES))})"
    )
    print(f"  {len(directional)} of those carry a sign that counts for or against")
    if backing is None:
        print("  evidence rows: this build has no evidence module, so nobody can say")
    else:
        print(f"  {sum(backing.values())} evidence row(s) across every observation")
    print(
        f"\nThe bar: at least {vocabulary.MIN_SUPPORT} agreeing observations, "
        f"agreement {vocabulary.REQUIRED_AGREEMENT},\nand every supporting "
        f"observation in one of those two states."
    )

    if not decisions:
        print(
            "\nNothing has recorded a decision in this scope. A lesson is a "
            "claim about a\ndecision, so there is nothing for one to be about "
            "yet. Decisions arrive as\n`decision.recorded` events, or as any "
            "node typed `decision`."
        )
    elif not observations:
        print(
            "\nDecisions exist and nothing has reported how any of them went. "
            "That half is\nwhat the event channel is for: `outcome.observed`, "
            "`deployment.completed` and\n`test.completed` all write one."
        )
    elif not supporting:
        said = (
            "and no evidence has been attached either, so nothing is reporting"
            if not backing or not sum(backing.values())
            else "although evidence IS arriving, so artefacts are being "
                 "attached and nobody is\nadjudicating them"
        )
        print(
            f"\nOutcomes exist and none of them has been checked, {said}.\n"
            f"\nThis is the usual reason a scope has no lessons, and it is not "
            f"a defect:\n`evidence_backed` is never derived from evidence "
            f"existing, because a reporter\nthat could promote its own claim "
            f"by attaching its own artefact would make\n`verified` mean "
            f"nothing. Somebody has to say so:\n"
            f"\n  echo-memory trust verify --scope {_scope(args)} --fact "
            f"<id> \\\n      --verifier <who> --method <how>\n"
            f"\n`trust list --scope {_scope(args)} --state unverified` lists "
            f"the candidates."
        )
    elif not directional:
        print(
            "\nEvery supporting outcome is `mixed` or `unknown`. Those are "
            "evidence that the\ndecision was taken and say nothing about how "
            "it went, so they are excluded\nfrom both counts rather than "
            "making a weak lesson."
        )
    else:
        print(
            f"\nThere are checked, directional outcomes and still no lesson, "
            f"so the bar they\nmissed is support or agreement: fewer than "
            f"{vocabulary.MIN_SUPPORT} agreeing, or at least one pointing the\n"
            f"other way. A decision whose outcomes disagree is one a reader "
            f"should see as\ndisputed rather than resolved, so this is the "
            f"feature working."
        )
    return 0


_SUBCOMMANDS = {
    "show": _show,
    "list": _list,
    "verify": _record,
    "dispute": _record,
    "reject": _record,
    "evidence": _evidence,
    "lessons": _lessons,
}


def run(args, config, conn) -> int:
    return _SUBCOMMANDS[args.trust_command](args, config, conn)


def _add_scope(parser) -> None:
    """`--scope` on the subcommand, alongside the one on the top level parser.

    dest is `trust_scope` and the default is None on purpose. See `_scope`.
    """
    parser.add_argument(
        "--scope", choices=["solo", "shared"], dest="trust_scope", default=None,
        help="which scope to operate on. Also accepted before the subcommand",
    )


def add_parser(sub) -> None:
    """Wire `trust` into the CLI.

    Here rather than in `main.py` for the reason `exchange.add_parser` gives:
    adding a subcommand should not mean editing a file that already holds
    thirty parsers.
    """
    parser = sub.add_parser(
        "trust",
        help="record and read whether a fact has been checked, and by whom",
    )
    commands = parser.add_subparsers(dest="trust_command", required=True)

    show = commands.add_parser(
        "show", help="one fact's current state, every check it has had, and its evidence"
    )
    _add_scope(show)
    show.add_argument("--fact", required=True, metavar="ID", help="fact id, as query_memory returns it")

    listing = commands.add_parser(
        "list", help="what this scope's facts are in, and which have been checked"
    )
    _add_scope(listing)
    listing.add_argument(
        "--state", choices=states.ALL_STATES,
        help="only facts in this state. Without it, everything ever checked",
    )
    listing.add_argument(
        "--limit", type=int, default=DEFAULT_LIMIT, metavar="N",
        help=f"how many to print (default: {DEFAULT_LIMIT})",
    )

    for name, state in _RECORDING.items():
        # One parser shape for the three authoritative states. Built in a loop
        # so a flag added to one cannot be forgotten from the others, which is
        # how --verifier would end up required on two of three.
        recorder = commands.add_parser(
            name, help=f"record that a fact is {state}. Appends; nothing is overwritten"
        )
        _add_scope(recorder)
        recorder.add_argument("--fact", required=True, metavar="ID")
        recorder.add_argument(
            "--verifier", required=True, metavar="WHO",
            help="who is making this call. Required, and an empty one is "
                 "refused: a verification nobody can be identified by is one "
                 "nobody can be held to",
        )
        recorder.add_argument(
            "--method", required=True, metavar="TEXT",
            help="how it was checked. What a later reader uses to decide "
                 "whether to re-run it",
        )
        recorder.add_argument(
            "--kind", choices=states.VERIFIER_KINDS, default=states.HUMAN,
            help=f"what performed the check (default: {states.HUMAN})",
        )
        recorder.add_argument(
            "--policy", metavar="ID", help="the policy this check was made under"
        )
        recorder.add_argument(
            "--evidence", action="append", metavar="ID",
            help="an evidence id this rests on. Repeatable",
        )
        recorder.add_argument(
            "--expires", metavar="ISO",
            help="when this check lapses, with a timezone offset. Only for "
                 "verify: after it the fact reads as stale and the row is left "
                 "alone",
        )
        recorder.add_argument(
            "--detail", metavar="TEXT", help="a note recorded on the row"
        )

    ev = commands.add_parser(
        "evidence", help="record an artefact and link it to a fact"
    )
    _add_scope(ev)
    ev.add_argument("--fact", required=True, metavar="ID")
    ev.add_argument(
        "--kind", required=True, choices=evidence_module.KINDS,
        help="what sort of artefact this is",
    )
    ev.add_argument(
        "--uri", metavar="URL",
        help="where it is. The whole value of an evidence row is that "
             "somebody can go and look",
    )
    ev.add_argument(
        "--detail", metavar="JSON",
        help="a JSON object stored structured, so a reader can query it",
    )
    ev.add_argument(
        "--digest", metavar="HEX",
        help="what it said at the time, so a document edited since is "
             "distinguishable from one that was not",
    )
    ev.add_argument(
        "--occurred", metavar="ISO",
        help="when the artefact came to be, if that is not now. A test run "
             "from last Tuesday reported today is evidence about last Tuesday",
    )
    ev.add_argument("--by", metavar="WHO", default="cli", help="who reported it (default: cli)")

    lesson_parser = commands.add_parser(
        "lessons", help="what this scope's outcomes support, and why if nothing"
    )
    _add_scope(lesson_parser)
