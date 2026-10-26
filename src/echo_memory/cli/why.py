"""`echo-memory why <fact_id>`: a fact's audit trail as plain sentences.

Rendering is separated from I/O so the interesting cases are testable without
a database, and the interesting case here is not the history. It is what to
say when there is none.

`recall` answers from BOTH scopes and tags each fact with the one it came
from. `why` takes the global `--scope`, which defaults to solo. So the obvious
flow, recall a fact and ask why, hands back an id the next command cannot
resolve, and the old message said "No audit history found", which asserts the
fact has no provenance. It has provenance; it is one scope over. A tool whose
whole claim is that nothing is unexplained should not answer that way.
"""


def render_entry(entry: dict) -> str:
    return f"{entry['timestamp']}: {entry['summary']}"


def render_history(fact_id: str, entries: list[dict],
                   found_in: str | None = None) -> str:
    """The trail, or what to do instead.

    `found_in` names the scope the fact actually lives in, when the caller
    looked there after finding nothing in the selected one. Naming the command
    rather than the flag, because `--scope` is a global option and has to go
    BEFORE the subcommand: `echo-memory why <id> --scope shared` is rejected
    with "unrecognized arguments", which is argparse being argparse and is not
    obvious from a message that only says which scope to try.
    """
    if entries:
        lines = [f"History for fact {fact_id}:"]
        lines += [f"  {render_entry(e)}" for e in entries]
        return "\n".join(lines)

    if found_in:
        return (
            f"Fact {fact_id} is in the {found_in} scope, not the one this "
            f"command looked in.\n"
            f"  echo-memory --scope {found_in} why {fact_id}"
        )

    return f"No audit history found for fact {fact_id!r}."
