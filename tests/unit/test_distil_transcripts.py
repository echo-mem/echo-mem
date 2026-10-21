"""Reading a harness transcript without a model, and without reading 170MB.

Every test here is a way the extraction can look like it works and quietly
return the wrong thing. The one that matters most is the first: a `user`
record is usually not a person. In the session this was built against, tool
results outnumbered real turns 7,923 to 649, so an extractor that trusts the
record type feeds a model twelve times the tokens and almost none of the
signal.
"""

from __future__ import annotations

import json

from echo_memory.distil import transcripts


def _write(path, records):
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return path


def _human(text):
    return {"type": "user", "message": {"content": [{"type": "text", "text": text}]}}


def _tool_result(text="the command printed this"):
    return {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "content": text}]},
    }


SPOKEN = "never push straight to main, open a pull request every time"


def test_a_tool_result_is_not_a_person_speaking(tmp_path):
    """Identified structurally, by the tool_result block, rather than guessed
    at from the text. This is the 12:1 noise."""
    path = _write(tmp_path / "s.jsonl", [_tool_result(), _human(SPOKEN), _tool_result()])

    assert transcripts.human_turns(path) == [SPOKEN]


def test_an_acknowledgement_carries_no_fact(tmp_path):
    """"yes", "do it", "go on". They cost tokens to ask a model about and can
    never answer."""
    path = _write(tmp_path / "s.jsonl", [_human("yes do it"), _human(SPOKEN)])

    assert transcripts.human_turns(path) == [SPOKEN]


def test_a_slash_command_is_addressed_to_the_harness(tmp_path):
    path = _write(tmp_path / "s.jsonl", [_human("/compact the conversation now"), _human(SPOKEN)])

    assert transcripts.human_turns(path) == [SPOKEN]


def test_a_long_paste_is_truncated_rather_than_dropped(tmp_path):
    """A pasted log is mostly noise, but its opening line is often the
    instruction that frames it, so the turn is kept and cut."""
    pasted = "here is the failing output, fix it: " + ("x" * 9000)
    path = _write(tmp_path / "s.jsonl", [_human(pasted)])

    turns = transcripts.human_turns(path)

    assert len(turns) == 1
    assert len(turns[0]) == transcripts.MAX_TURN_CHARS
    assert turns[0].startswith("here is the failing output")


def test_a_half_written_last_line_is_skipped_not_fatal(tmp_path):
    """These files are appended to by a running process, so the final line can
    legitimately be incomplete while the command reads it."""
    path = tmp_path / "s.jsonl"
    path.write_text(json.dumps(_human(SPOKEN)) + "\n" + '{"type": "user", "mess', encoding="utf-8")

    assert transcripts.human_turns(path) == [SPOKEN]


def test_a_plain_string_content_is_read_too(tmp_path):
    """Not every record uses the block form."""
    path = _write(tmp_path / "s.jsonl", [{"type": "user", "message": {"content": SPOKEN}}])

    assert transcripts.human_turns(path) == [SPOKEN]


def test_assistant_and_system_records_are_ignored(tmp_path):
    path = _write(
        tmp_path / "s.jsonl",
        [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "I will"}]}},
            {"type": "system", "content": "a reminder nobody typed"},
            _human(SPOKEN),
        ],
    )

    assert transcripts.human_turns(path) == [SPOKEN]


def test_the_limit_stops_reading_early(tmp_path):
    """The point of the limit is not to trim the result, it is to stop
    streaming a very large file once enough has been found."""
    path = _write(tmp_path / "s.jsonl", [_human(f"{SPOKEN} number {i}") for i in range(10)])

    assert len(transcripts.human_turns(path, limit=3)) == 3


def test_an_unreadable_file_returns_nothing_rather_than_raising(tmp_path):
    assert transcripts.human_turns(tmp_path / "does-not-exist.jsonl") == []


def test_a_machine_with_no_history_is_not_an_error(tmp_path):
    """A project Claude Code has never opened has no directory. That is not a
    failure, it is a project Claude Code has never opened."""
    assert transcripts.sessions_for("/no/such/project", root=tmp_path) == []


def test_the_slug_matches_what_the_harness_writes():
    assert (
        transcripts.project_slug("/Users/a/work/echo-mem") == "-Users-a-work-echo-mem"
    )


def test_sessions_come_back_newest_first(tmp_path):
    """A person distilling "the session I just finished" means the newest one,
    and making them read mtimes to find it would be the command's job
    undone."""
    root = tmp_path / "projects"
    directory = root / transcripts.project_slug("/w/proj")
    directory.mkdir(parents=True)
    for name, when in (("old", 1_000_000), ("new", 2_000_000)):
        p = _write(directory / f"{name}.jsonl", [_human(SPOKEN)])
        import os

        os.utime(p, (when, when))

    found = transcripts.sessions_for("/w/proj", root=root)

    assert [s.session_id for s in found] == ["new", "old"]
    assert found[0].project == "proj"
