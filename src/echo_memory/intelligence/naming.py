"""Where an outcome's sign lives, written once so two sides cannot disagree.

A FACT edge's properties are fixed by `write_episode`: relation_type, fact,
confidence, causal_hint, validity, group_id, project, agent_id, provenance. A
Node's are name, type, group_id, aliases. There is no spare column for "did
this work", and adding one would mean editing the write path every feature
wants a field - which is how a schema stops being a schema.

So the sign rides in the outcome node's NAME, and that turns out to be the
right place rather than a workaround, for a reason about identity.

Migration 0016 made node names unique per scope: one node per name. Edge
identity is (source, target, relation_type). So if a success and a later
failure of the same thing both landed on a node called
`checkout-error-rate-2026-11`, they would be the same two endpoints with the
same relation type - and the second write would SUPERSEDE the first. Two
genuine observations, one surviving. The store would say the decision worked,
or that it failed, depending only on which report arrived last, and the
supersession would look like a correction rather than the loss of a data
point.

Naming the sign into the node splits them. A success and a failure become two
nodes, two edges, two observations, and the lesson layer gets to count both -
which is the whole arithmetic MIN_SUPPORT and REQUIRED_AGREEMENT do.

## Why this module exists at all

`outcomes.py` writes the name and `lessons.py` reads the sign back out of it.
Those are the two sides of one format, and a format with two implementations
has gone blind in this codebase twice: a group id derived a second time in a
reader, and a licence guard asserting a correction existed while the stale
claim sat beside it. One function each way, imported by both, and nothing in
here touches a database or imports the write path.
"""

from __future__ import annotations

from echo_memory.intelligence.vocabulary import OUTCOME_SIGNS, UNKNOWN

#: Between the outcome's own name and its sign. Chosen because it does not
#: occur in an identifier a reporter would send, and because the composed name
#: still reads as English to somebody looking at the graph: the name is the
#: thing a person recognises and the sign is an annotation on it.
#:
#: Deliberately not a character a caller might already be using as a
#: separator. A dot, a colon or a slash all appear in real outcome names
#: (`checkout.error_rate`, `ci:build-412`, `deploy/prod`), and parsing on one
#: of those would split a name that was never annotated.
SIGN_SEPARATOR = " :: "


def outcome_node_name(name: str, sign: str) -> str:
    """The node name for an observation of `name` that came out `sign`.

    Raises on a sign outside OUTCOME_SIGNS. A sign nobody can read back is
    worse than no sign, because the node reads as annotated and the lesson
    layer will treat it as having no directional value - silently dropping a
    real observation out of the arithmetic.
    """
    if sign not in OUTCOME_SIGNS:
        raise ValueError(
            f"unknown outcome sign {sign!r}, expected one of {list(OUTCOME_SIGNS)}"
        )
    if SIGN_SEPARATOR in name:
        raise ValueError(
            f"outcome name {name!r} already contains {SIGN_SEPARATOR!r}, which "
            f"is how the sign is attached. Rename it: a name carrying the "
            f"separator cannot be told apart from an already-signed one."
        )
    return f"{name}{SIGN_SEPARATOR}{sign}"


def sign_of_node_name(node_name: str) -> str:
    """The sign an outcome node carries, or `unknown` if it carries none.

    `unknown` rather than an error, because this reads nodes that were written
    before any of this existed, and by callers that type a node `outcome`
    without going through the event path. SIGN_VALUE scores `unknown` at zero,
    so such a node counts as evidence that something was observed without
    counting for or against the decision - which is the honest reading of a
    node that never said how it went.
    """
    _, separator, tail = node_name.rpartition(SIGN_SEPARATOR)
    if not separator or tail not in OUTCOME_SIGNS:
        return UNKNOWN
    return tail


def outcome_subject(node_name: str) -> str:
    """The outcome's own name, with any sign annotation removed. What a reader
    should be shown: `checkout-error-rate-2026-11`, not the composed form."""
    head, separator, tail = node_name.rpartition(SIGN_SEPARATOR)
    if not separator or tail not in OUTCOME_SIGNS:
        return node_name
    return head
