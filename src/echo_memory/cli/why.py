"""echo-memory why <fact_id>: renders a fact's audit trail as plain-language
sentences (see the CEO plan's scope decision #3). Rendering logic is
separated from I/O so it's unit-testable without a database."""


def render_entry(entry: dict) -> str:
    return f"{entry['timestamp']}: {entry['summary']}"


def render_history(fact_id: str, entries: list[dict]) -> str:
    if not entries:
        return f"No audit history found for fact {fact_id!r}."
    lines = [f"History for fact {fact_id}:"]
    lines += [f"  {render_entry(e)}" for e in entries]
    return "\n".join(lines)
