"""A digest per fact, chained, so that no change to one is silent.

Provenance in this store is believed because the database says so. Every fact
records its author, its session, the package version that wrote it and when it
became true, and all of it is editable by anyone who can write SQL. For AI
governance that is the gap that matters: the value of "which agent asserted
this" is precisely that it cannot be edited later by whoever finds the answer
inconvenient.

So each fact gets a digest over its own claims, chained to the previous
attestation in the same scope. Changing a fact breaks its digest. Deleting one
breaks the chain at that link. Repairing either means recomputing every digest
after it, and with a key configured, not even then.

## What this proves

A digest stored in the same database as the data it attests is worth something
only if an attacker cannot recompute it, which needs one of two things:

- **A key.** `ECHO_MEMORY_ATTESTATION_KEY` makes each digest an HMAC. Somebody
  with SQL access and no key cannot forge one.
- **An external witness.** Unkeyed, the chain is still a chain: any change
  alters every digest after it, including the head. Recording the head outside
  this database makes a silent rewrite impossible even with no key.

With neither, this detects corruption and careless edits and nothing else.
`report` says which is in force rather than implying a guarantee that is not
there.

## Turning the key on later

Each row records the `keyed` flag it was written under, and verification uses
that row's flag rather than the setting in force when somebody asks. So
enabling `ECHO_MEMORY_ATTESTATION_KEY` on a store that has been running
unkeyed leaves every existing link verifiable exactly as before, and new links
become unforgeable from that point. The chain is then `mixed_keying`, which
`strength` says plainly, because the old part is still only as strong as an
external record of the head.

Turning it off, or verifying somewhere the key is not configured, is the other
direction and cannot work: those links report as `unverifiable`, which is this
report declining to claim something it cannot check rather than an accusation.

What this table does not record is WHICH key, so rotating the key reports every
keyed link as altered at once. `Report.likely_wrong_key` names that pattern for
what it usually is instead of leaving a reader to conclude their store was
rewritten. `trust/vattest.py` carries a `key_id` per link and rotates properly;
doing the same here needs a migration and a backfill.

## What this does not prove

**Not the author.** `agent_id` is still a string the writer supplies. The
attestation proves the recorded author has not changed since the write, not
that it was true at the write. Proving that needs a key per agent, and an
agent here is an LLM in somebody else's process, not a thing that can hold
one.

**Not the interval before the attestation.** A fact attested later than it was
written has a baseline from the moment it was attested and says nothing about
what happened before.

**Not absence of deletion on its own.** A missing link is visible, but whether
it was erasure doing its job or somebody removing evidence is answered by
reconciling against `erasure_event`, which `report` does.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass, field

from echo_memory.infra.db import GRAPH_NAME as GRAPH
from echo_memory.infra.logging import get_logger

_logger = get_logger("attestation")

#: Where the HMAC key is read from. Deliberately not in the database: a key
#: stored next to what it protects protects nothing.
KEY_ENV = "ECHO_MEMORY_ATTESTATION_KEY"

#: What the first attestation in a scope chains to. A literal rather than an
#: empty string so a row whose prev_digest was blanked is distinguishable from
#: the genuine first link.
GENESIS = "genesis"

#: The fact properties the digest covers, in this order. Everything here is a
#: claim the fact makes about itself; changing any of them changes what the
#: fact says or who said it.
COVERED = (
    "relation_type",
    "fact",
    "confidence",
    "causal_hint",
    "t_valid",
    "agent_id",
    "project",
)

_GROUP = """(properties ->> '"group_id"'::agtype)"""

#: Recorded mutations that legitimately rewrite a covered property after the
#: write. `merge` repoints an edge at the surviving node; the causal passes set
#: and clear `causal_hint` on facts written long before.
#:
#: Deliberately NOT `created`: write_episode writes a `created` entry for every
#: fact, in the same transaction as the attestation, so an explanation that
#: accepted it would label every tampered fact as explained by its own
#: creation - which is what the first version of this did, and it made the
#: whole report useless while every test but one passed. Nor `invalidated` or
#: `fact_superseded`, which set `t_invalid` and nothing the digest covers.
REWRITES_A_FACT = ("entity_resolved", "causal_hint_set", "causal_hint_cleared")


class AttestationError(Exception):
    """The request names something that cannot be done, and why."""


class KeyUnavailable(AttestationError):
    """This link was keyed and this process has no key to check it with.

    Its own type because the answer it produces is different from every other
    failure here: not "this fact changed" but "I cannot tell", and a report
    that could not tell those apart would call an entire store forged every
    time somebody verified it on a machine without the key.
    """


def keyed() -> bool:
    return bool(os.environ.get(KEY_ENV, ""))


def canonical(claims: dict) -> bytes:
    """The bytes a digest is taken over.

    JSON with sorted keys and no insignificant whitespace, because the digest
    has to be reproducible by a different version of this code on a different
    machine, and anything that lets two serialisations of the same fact differ
    turns a verification failure into a coin flip.

    `t_invalid` is deliberately absent. Supersession is a normal part of the
    model and sets it long after the write; covering it would make every
    superseded fact fail verification, which would train a reader to ignore
    the report.
    """
    return json.dumps(claims, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode()


def digest(prev: str, body: bytes, *, use_key: bool | None = None) -> str:
    """One link. HMAC when a key is configured, plain SHA-256 otherwise.

    The previous digest is inside the hash rather than beside it, which is
    what makes this a chain: a changed link invalidates every link after it,
    so tampering with one fact means recomputing the whole tail.

    `use_key` decides which of the two algorithms this is, and the default,
    None, means "whatever is configured now". That default is right on the
    write path and wrong on the read path, which is the defect this parameter
    exists to fix. Every row records the `keyed` flag it was written under, so
    a verification asks for that one explicitly: an unkeyed row is checked
    unkeyed even on a machine that now has a key, and a keyed row is never
    checked with the wrong algorithm.

    Asking for a keyed digest with no key raises rather than falling back to
    SHA-256. A fallback would produce a digest that is simply a different
    number, and the caller would read that difference as the fact having
    changed.
    """
    material = prev.encode() + b"\n" + body
    key = os.environ.get(KEY_ENV, "")
    if use_key is None:
        use_key = bool(key)
    if use_key:
        if not key:
            raise KeyUnavailable(
                f"this link was attested with {KEY_ENV} set and this process "
                f"does not have it, so its digest cannot be recomputed"
            )
        return "hmac-sha256:" + hmac.new(key.encode(), material, hashlib.sha256).hexdigest()
    return "sha256:" + hashlib.sha256(material).hexdigest()


def _value(raw):
    """One agtype property as a plain Python value.

    An absent property comes back as the agtype `null` rather than SQL NULL,
    and a digest that treated those two as different values would depend on
    which code path read the fact. Every read path in this package already
    treats a missing property and a null one the same, so this has to as
    well.
    """
    if raw is None or str(raw) == "null":
        return None
    return json.loads(str(raw))


def _decoded(raw):
    """One agtype property as read by `->>`, which hands back raw text.

    The counterpart to `_value`, and they must agree. `_value` decodes the
    JSON quoted form Cypher returns; this one has the decoded text already and
    only has to turn agtype's `null` into Python's. An absent property comes
    back as SQL NULL here and as the agtype `null` through Cypher, and a digest
    that told those apart would depend on which reader saw the fact.
    """
    if raw is None or str(raw) == "null":
        return None
    return str(raw)


def claims_of(conn, group_id: str, edge_id: str) -> dict:
    """What a fact says about itself, read back from the graph.

    Includes the endpoint names, because moving an edge from one node to
    another changes its meaning entirely: "billing uses Razorpay" becomes
    "billing uses Stripe" without touching a single covered property.

    ## Why this reads the tables and not Cypher

    It used to ask through Cypher, and the comment then said the per-property
    projection was "the only form that works on both a map and a scalar". That
    was half right. `properties::text` on a map is indeed refused, but a
    NESTED projection is not: `properties -> '"provenance"' ->> '"session_id"'`
    reaches inside the map and returns the one value this function wants.

    The cost of asking through Cypher was that `MATCH (a)-[e:FACT]->(b) WHERE
    id(e) = $eid` leaves the pattern unbound, so AGE expands every FACT edge in
    the graph before filtering by id. On the write path, once per fact written.
    Measured on this schema, one fact read by id out of a growing graph:

        edges in graph    through Cypher    off the tables    speedup
                   307         0.776 ms          0.409 ms        1.9x
                 1,807         2.339 ms          0.657 ms        3.6x
                 5,807         6.785 ms          0.694 ms        9.8x

    Cypher grows linearly and the table read does not, which is the signature
    of a sequential scan against an index scan. It is the same defect already
    fixed twice in `neighbourhood._adjacent` and once in `_graph_candidates`,
    and migration 0024 describes the shape: "write cost rose from 29ms at
    1,057 nodes to 129ms at 24,054... read as the corpus being large rather
    than as a defect".

    ## Why moving it was dangerous, and what was proven first

    This function feeds `canonical()`, which feeds the digest. The two reads do
    NOT return the same bytes: Cypher returns JSON quoted agtype (`'"uses"'`)
    and `->>` returns raw text (`'uses'`). Measured across seven facts chosen
    to break a naive decoder - embedded newline, embedded quotes, a backslash
    path, unicode, text that looks like a JSON object, a tab, and one fact with
    no causal_hint at all - 51 of 63 fields differed in their raw form.

    What was proven before the change landed is that they agree once DECODED:
    raw text equals `json.loads(quoted)` for all 63, with `t_valid` needing an
    int cast because `->>` hands back a string. So `_value` stays for the
    Cypher form, `_decoded` is its counterpart here, and the digest over every
    covered property is unchanged. `tests/integration/test_attestation.py`
    pins that with a populated store: same claims, same digests, same head,
    clean report.

    If a future change touches either reader, that equivalence is the thing to
    re-prove. Getting it wrong does not fail a test loudly; it reports every
    fact in every store as altered.
    """
    props = ", ".join(
        f'''(e.properties ->> '"{key}"'::agtype)''' for key in COVERED
    )
    row = conn.execute(
        f"""SELECT (a.properties ->> '"name"'::agtype),
                   (b.properties ->> '"name"'::agtype),
                   {props},
                   ((e.properties -> '"provenance"'::agtype)
                        ->> '"session_id"'::agtype)
              FROM {GRAPH}."FACT" e
              JOIN {GRAPH}."Node" a ON a.id = e.start_id
              JOIN {GRAPH}."Node" b ON b.id = e.end_id
             WHERE e.id = %s::graphid
               AND {_GROUP.replace("properties", "e.properties")} = %s""",
        (str(edge_id), group_id),
    ).fetchone()
    if row is None:
        raise AttestationError(f"no fact {edge_id} in {group_id}")

    source, target = _decoded(row[0]), _decoded(row[1])
    covered = dict(zip(COVERED, (_decoded(v) for v in row[2:-1]), strict=True))
    # t_valid is a number in the digest and a string out of ->>. Cast rather
    # than left as text: a digest over "1762800000" is not a digest over
    # 1762800000, and the whole point of this change is that it is.
    if covered.get("t_valid") is not None:
        covered["t_valid"] = int(covered["t_valid"])
    return {
        "group_id": group_id,
        "source": source,
        "target": target,
        **covered,
        "session_id": _decoded(row[-1]),
    }


def head(conn, group_id: str) -> tuple[int, str]:
    """The scope's last link: its sequence number and digest.

    This is the value worth recording somewhere outside the database. One
    string is enough to make every fact in the scope unrewritable, because
    changing any of them changes this.
    """
    row = conn.execute(
        """SELECT seq, digest FROM public.fact_attestation
            WHERE group_id = %s ORDER BY seq DESC LIMIT 1""",
        (group_id,),
    ).fetchone()
    return (row[0], row[1]) if row else (0, GENESIS)


def attest(conn, group_id: str, edge_id: str) -> str:
    """Append this fact to the scope's chain, and return the new digest.

    Called from the write path, inside its transaction. Two writers appending
    to one scope at once would otherwise read the same head and fork the
    chain, and a fork lets a verification pick the branch that passes - so
    this takes a per-scope advisory lock first. Per scope rather than global
    because scopes are tenants, and one account's writes must not be able to
    block another's.
    """
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (group_id,))
    seq, prev = head(conn, group_id)
    body = canonical(claims_of(conn, group_id, edge_id))
    value = digest(prev, body)
    conn.execute(
        """INSERT INTO public.fact_attestation
               (edge_id, group_id, seq, prev_digest, digest, keyed)
           VALUES (%s::graphid, %s, %s, %s, %s, %s)""",
        (edge_id, group_id, seq + 1, prev, value, keyed()),
    )
    return value


@dataclass
class Report:
    """What verification found. Every list is a separate kind of finding
    because they have different causes and different answers."""

    group_id: str
    n_attested: int = 0
    keyed: bool = False
    mixed_keying: bool = False
    #: Facts whose digest no longer matches their content.
    altered: list[dict] = field(default_factory=list)
    #: Facts with no attestation at all. Written before this shipped, or
    #: written by something that bypassed the write path, or had their
    #: attestation row removed - and the three are not distinguishable here.
    unattested: list[str] = field(default_factory=list)
    #: Links whose prev_digest does not match the digest before them.
    broken_links: list[dict] = field(default_factory=list)
    #: Attestations whose fact is gone, reconciled against erasure_event.
    erased: int = 0
    vanished: list[str] = field(default_factory=list)
    #: Links that were attested keyed while this process has no key. Not a
    #: finding about the data: it is this report declining to claim something
    #: it cannot check.
    unverifiable: list[dict] = field(default_factory=list)
    #: How many keyed links were actually recomputed. The denominator for
    #: `likely_wrong_key`, and without it "3 altered" cannot be told apart
    #: from "3 altered out of 3".
    keyed_checked: int = 0

    def is_clean(self) -> bool:
        """Whether anything was found wrong. Deliberately not false for
        `unverifiable`: a link that could not be checked is an absence of
        evidence, and calling it tampering would be a false accusation every
        time somebody verifies a store on a machine without the key."""
        return not (self.altered or self.broken_links or self.vanished)

    @property
    def likely_wrong_key(self) -> bool:
        """Every keyed link that was checked failed, and nothing else did.

        One fact being altered is a finding. Every keyed fact in a scope being
        altered, all at once, with the chain still continuous, is not a story
        about the data: a rewrite that touched all of them would have to leave
        `prev_digest` consistent at every link, which means recomputing the
        whole tail, which a key is what prevents. The ordinary cause is that
        `ECHO_MEMORY_ATTESTATION_KEY` is not the key these rows were written
        with.

        A judgement, not a measurement: this table records whether a row was
        keyed and not which key, so a rotated key and a forged store are not
        distinguishable here. Said as a suspicion so a reader checks the
        variable before believing the alarm.
        """
        return (
            self.keyed_checked > 0
            and len(self.altered) == self.keyed_checked
            and not self.broken_links
            and all(a.get("keyed") for a in self.altered)
        )

    @property
    def strength(self) -> str:
        """What the chain is actually worth, in one phrase.

        A report that says "verified" without this is the thing a reader would
        quote, and it would be wrong unkeyed and unwitnessed.
        """
        caveat = ""
        if self.unverifiable:
            caveat = (
                f"; {len(self.unverifiable)} link(s) were attested with a key "
                f"this process does not have and were not checked at all"
            )
        if self.mixed_keying:
            return (
                "part of this chain is keyed and part is not; the unkeyed "
                "part is only as strong as an external record of the head"
                + caveat
            )
        if self.keyed:
            return (
                f"keyed, so a digest cannot be recomputed without {KEY_ENV}"
                + caveat
            )
        return (
            "unkeyed, so this is only as strong as a record of the head kept "
            "outside this database" + caveat
        )


def report(conn, group_id: str) -> Report:
    """Verify a scope's chain, and say what the result is worth.

    Three separate questions, because an answer that merges them cannot be
    acted on: has any fact changed, is the chain continuous, and does every
    fact have a link at all.
    """
    rows = conn.execute(
        """SELECT a.edge_id::text, a.seq, a.prev_digest, a.digest, a.keyed,
                  a.attested_at
             FROM public.fact_attestation a
            WHERE a.group_id = %s ORDER BY a.seq""",
        (group_id,),
    ).fetchall()

    out = Report(group_id=group_id, n_attested=len(rows))
    if not rows:
        out.keyed = keyed()
        return out

    keyings = {r[4] for r in rows}
    out.keyed = all(keyings)
    out.mixed_keying = len(keyings) > 1

    expected_prev = GENESIS
    for edge_id, seq, prev_digest, stored, row_keyed, attested_at in rows:
        if prev_digest != expected_prev:
            out.broken_links.append({
                "seq": seq, "edge_id": edge_id,
                "expected": expected_prev, "found": prev_digest,
            })
        expected_prev = stored

        try:
            body = canonical(claims_of(conn, group_id, edge_id))
        except AttestationError:
            # The fact is gone. Erasure deletes facts on purpose, so this is
            # only a finding when no erasure can account for it. The counts in
            # erasure_event are what reconciles it; they carry no fact ids,
            # deliberately, so this cannot be matched fact by fact.
            out.vanished.append(edge_id)
            continue

        # The algorithm this row was written under, not the one configured
        # now. Reading it from the environment instead is what made enabling
        # a key report every pre-existing fact as altered: measured on a live
        # store, 3 of 3, while the same report said keyed=False. Supersession
        # was already protected from reading as tampering; this is the same
        # promise for a setting change.
        try:
            recomputed = digest(prev_digest, body, use_key=row_keyed)
        except KeyUnavailable:
            out.unverifiable.append({"seq": seq, "edge_id": edge_id})
            continue
        if row_keyed:
            out.keyed_checked += 1

        # Recomputed with the stored prev_digest rather than the expected one,
        # so a break earlier in the chain does not report every fact after it
        # as altered. One cause, one finding.
        if recomputed != stored:
            out.altered.append({
                "seq": seq, "edge_id": edge_id, "keyed": bool(row_keyed),
                "explanation": _explain(conn, group_id, edge_id, attested_at),
            })

    if out.vanished:
        (erased,) = conn.execute(
            "SELECT coalesce(sum(n_edges), 0) FROM public.erasure_event "
            "WHERE group_id = %s AND status = 'completed'",
            (group_id,),
        ).fetchone()
        out.erased = int(erased)
        if out.erased >= len(out.vanished):
            # Accounted for. Still reported as a count, because "12 facts are
            # gone and 12 were erased" is a different statement from "nothing
            # is missing" and a reader is entitled to the first one.
            out.vanished = []

    _logger.info(
        "attestation_verified",
        extra={"fields": {
            "group_id": group_id, "n_attested": out.n_attested,
            "altered": len(out.altered), "broken_links": len(out.broken_links),
            "vanished": len(out.vanished), "keyed": out.keyed,
            "unverifiable": len(out.unverifiable),
        }},
    )
    return out


def _explain(conn, group_id: str, edge_id: str, attested_at) -> str | None:
    """A recorded mutation that would account for this fact having changed.

    `merge` repoints an edge and the causal passes retype one, both
    legitimately and both with an audit entry. Reporting those as tampering
    would train a reader to ignore the report, which costs more than the
    finding is worth - so they are labelled rather than hidden, and a reader
    still sees that the fact is not what was attested.

    Only entries from the attestation onwards, and only the kinds that rewrite
    something the digest covers. An explanation that looked at every entry
    would find the fact's own creation.
    """
    row = conn.execute(
        # The column is "timestamp", a reserved word, quoted everywhere it
        # appears in this schema.
        """SELECT mutation_type, to_char("timestamp", 'YYYY-MM-DD'), summary
             FROM public.audit_entry
            WHERE group_id = %s
              AND affected_edge_ids && %s::text[]::graphid[]
              AND mutation_type::text = ANY(%s)
              AND "timestamp" >= %s
            ORDER BY "timestamp" DESC LIMIT 1""",
        (group_id, [edge_id], list(REWRITES_A_FACT), attested_at),
    ).fetchone()
    if row is None:
        return None
    kind, when, summary = row
    return f"{kind} on {when}" + (f": {summary}" if summary else "")


def unattested(conn, group_id: str) -> list[str]:
    """Facts in this scope with no attestation.

    Separate from `report` because the answer is usually "every fact written
    before this shipped", which is a backfill rather than a finding, and a
    report that lists thousands of them hides the ones that matter.
    """
    rows = conn.execute(
        f"""SELECT e.id::text
              FROM {GRAPH}."FACT" e
             WHERE {_GROUP.replace("properties", "e.properties")} = %s
               AND NOT EXISTS (
                   SELECT 1 FROM public.fact_attestation a
                    WHERE a.edge_id = e.id)
             ORDER BY e.id""",
        (group_id,),
    ).fetchall()
    return [r[0] for r in rows]


def backfill(conn, group_id: str) -> int:
    """Attest every fact in this scope that has no attestation.

    Establishes a baseline from now. It proves nothing about the interval
    before it ran, and `attested_at` on each row is what says so - which is
    why this is a separate command an operator invokes rather than something
    `init-db` does quietly, where it would look like the facts had been
    attested all along.
    """
    ids = unattested(conn, group_id)
    for edge_id in ids:
        attest(conn, group_id, edge_id)
    return len(ids)
