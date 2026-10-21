"""`echo-memory distil`: read the session the harness already recorded.

Dry run by default. It prints what it would write and exits, because an
extraction pass over a chat transcript is a proposal and not a result, and a
command that silently filled somebody's graph with a model's reading of their
conversation would be worse than the gap it closes.

The arrangement, stated once so it is not mistaken for a change of position:
the engine's write path still calls no model. This is a command a person
types, using a key from their own environment, over a transcript the harness
wrote without any model in the loop. The same shape as `infer-causal-hints`.
"""

from __future__ import annotations

from echo_memory.cli import llm
from echo_memory.distil import candidates as extraction
from echo_memory.distil import transcripts
from echo_memory.ingestion.embeddings import LocalEmbedder
from echo_memory.ingestion.write_episode import write_episode

# Printed before the candidates, because the first run is where somebody
# decides whether to trust this at all.
PREAMBLE = (
    "These are proposals read out of your own session, not facts. Each one "
    "carries the words you typed that support it; anything the model could "
    "not quote has already been dropped."
)


def _render(found: list[extraction.Candidate]) -> str:
    lines = []
    for i, c in enumerate(found, start=1):
        lines.append(f"  {i:>2}. {c.subject} --{c.relation}--> {c.object}")
        lines.append(f"      {c.fact}")
        lines.append(f'      you said: "{c.quote[:110]}"')
        lines.append("")
    return "\n".join(lines)


def run(args, config, conn) -> int:
    if not llm.configured():
        print(
            f"error: set {llm.KEY_VAR} and {llm.MODEL_VAR} to distil a session.\n"
            f"This is the only command in the engine that calls a model, it uses "
            f"your key, and it reads a transcript that was written without one.",
        )
        return 1

    sessions = transcripts.sessions_for(args.cwd or ".")
    if not sessions:
        print(
            "No Claude Code transcript for this project. Transcripts live in "
            f"{transcripts.CLAUDE_PROJECTS}/<project>/ and are written by the "
            "harness, so there is nothing to distil until a session has run here."
        )
        return 0

    chosen = sessions[0]
    if args.session:
        matches = [s for s in sessions if s.session_id.startswith(args.session)]
        if not matches:
            print(f"error: no session here starting {args.session!r}")
            return 1
        chosen = matches[0]

    turns = transcripts.human_turns(chosen.path)
    if not turns:
        print(f"Session {chosen.session_id[:8]} has nothing a person typed.")
        return 0

    if args.limit:
        turns = turns[-args.limit :]

    print(
        f"Session {chosen.session_id[:8]}, {chosen.size_bytes / 1e6:.0f}MB on disk, "
        f"{len(turns)} turns you typed.\n"
        f"Reading them in batches of {extraction.BATCH_TURNS} with "
        f"{llm.provider_from_env().model}.\n"
    )

    result = extraction.extract(turns, llm.provider_from_env(), llm.complete)

    for failure in result.failures:
        print(f"  ! {failure}")

    if not result.candidates:
        print("Nothing worth recording. An honest empty answer, not a failure.")
        return 0 if result.ok else 1

    print(f"{PREAMBLE}\n")
    print(_render(result.candidates))

    if not args.write:
        print(
            f"{len(result.candidates)} candidate(s) from {result.batches} batch(es). "
            f"Nothing has been written. Re-run with --write to store them."
        )
        return 0

    group_id = config.group_id(args.scope)
    embedder = LocalEmbedder()
    stored = deferred = 0
    for c in result.candidates:
        entities, facts = c.as_episode()
        # assume_new, because these entities come from a conversation rather
        # than from the graph. The resolver's ambiguous band would otherwise
        # defer most of them, which is exactly the silent non-write this whole
        # command exists to stop happening.
        written = write_episode(
            conn, group_id, f"distil:{chosen.session_id}", entities, facts, None,
            embedder, project=chosen.project, agent_id="distil", assume_new=True,
        )
        if written.get("edges_created") or written.get("superseded"):
            stored += 1
        else:
            deferred += 1

    print(f"Stored {stored} of {len(result.candidates)}.")
    if deferred:
        # Counted rather than assumed. A write that returns is not a write
        # that stored, and reporting the call count here would repeat the
        # exact mistake that cost a customer 25,000 facts.
        print(
            f"{deferred} did not store: already present unchanged, refused, or "
            f"deferred on an ambiguous entity. `echo-memory why <id>` explains one."
        )
    return 0
