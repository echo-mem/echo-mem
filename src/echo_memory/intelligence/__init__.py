"""The learning layer: outcomes recorded as graph structure, lessons derived
from them at read time.

    vocabulary  the reserved words, the support and agreement thresholds
    naming      where an outcome's sign lives, written once for both sides
    lessons     the read-time derivation. Writes nothing
    outcomes    the write side, which goes through the one write path

`outcomes` is deliberately NOT imported here, and that is the only unusual
thing about this file. It imports the write path; `lessons` must not, and
there is a test asserting that its source does not mention it. If this package
pulled the write path in on import, every reader of `lessons` would pull it in
too through this module, and the separation the test defends would be true of
one file and false of the program.

So the write side is imported by name where it is used:

    from echo_memory.intelligence import outcomes

which is one line longer and leaves the read path able to import this package
without the ingestion stack behind it.
"""

from echo_memory.intelligence.lessons import (
    SUPPORTING_STATES,
    candidates,
    for_query,
    repeated_failures,
)
from echo_memory.intelligence.naming import (
    SIGN_SEPARATOR,
    outcome_node_name,
    outcome_subject,
    sign_of_node_name,
)
from echo_memory.intelligence.vocabulary import (
    LESSON_BASES,
    MIN_SUPPORT,
    OUTCOME_SIGNS,
    REQUIRED_AGREEMENT,
    RESERVED_NODE_TYPES,
    RESERVED_RELATION_TYPES,
    SIGN_VALUE,
)

__all__ = [
    "LESSON_BASES",
    "MIN_SUPPORT",
    "OUTCOME_SIGNS",
    "REQUIRED_AGREEMENT",
    "RESERVED_NODE_TYPES",
    "RESERVED_RELATION_TYPES",
    "SIGN_SEPARATOR",
    "SIGN_VALUE",
    "SUPPORTING_STATES",
    "candidates",
    "for_query",
    "outcome_node_name",
    "outcome_subject",
    "repeated_failures",
    "sign_of_node_name",
]
