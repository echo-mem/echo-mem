"""The late backfill against a real store: what it writes, what it refuses, and
what it puts back.

Production holds 38,479 edges and 2 causal hints, both from a smoke test, so
`trace_cause` has nothing to walk. This command is the way out, and it is only
allowed to exist because it extracts rather than infers. These tests assert the
difference where it is observable: a sentence with no connective never reaches a
model, a proposal that cannot quote the sentence never reaches the graph, a hint
a caller asserted is never touched, and everything written can be taken back.

The model is a fake. Not for speed: a test that called a real one would measure
that model's reading of eight sentences rather than this code's handling of the
answer, and would make the suite non-deterministic and billable. The fake also
lets a badly behaved model be tested, which is the case the guards exist for.
"""

from __future__ import annotations

from types import SimpleNamespace

from fake_embedder import REFERENCE, VectorEmbedder, unit_vector_at_angle

from echo_memory.cli import infer_causal
from echo_memory.infra.config import Config
from echo_memory.infra.db import GRAPH_NAME as GRAPH
from echo_memory.infra.db import connect
from echo_memory.ingestion.write_episode import write_episode
from echo_memory.retrieval.causality import trace_cause

CONFIG = Config(user_id="u1", agent_id="a1", database_url="unused")
GROUP = CONFIG.solo_group_id()

SQLITE = "SQLite concurrent writes"
SWITCH = "switch to Postgres"
REWRITE = "the pool rewrite"
DEPLOY = "the Tuesday deploy"
OUTAGE = "the Friday outage"
INDEX = "the missing node index"

# States a cause, and says which end. The backfill should type these.
F_CONCURRENCY = "SQLite could not take the write concurrency so the store moved to Postgres"
F_REWRITE = "the switch to Postgres is why the pool rewrite was necessary"
# States nothing: two things in one week. This is the co-occurrence a
# statistical pass would type and this one must never see.
F_SAME_WEEK = "the deploy and the outage happened the same week"
# States a cause, and the fake will answer it with a quote that is not in it.
F_INDEX = "reads were slow because every lookup fell back to a sequential scan"
# Already typed by the caller that wrote it. Not a candidate, and --clear must
# leave it alone.
F_ALREADY = "the outage contradicts the claim that concurrency was fine"

# The entity names sit far enough apart that anchoring on one in trace_cause
# does not drag the others in: the walk keeps what scored within ANCHOR_MARGIN
# (0.15) of the best match, and every name here is at most 0.60 against SWITCH.
VECTORS = {
    SWITCH: REFERENCE,
    SQLITE: unit_vector_at_angle(0.40),
    REWRITE: unit_vector_at_angle(-0.50),
    DEPLOY: unit_vector_at_angle(-0.94),
    OUTAGE: unit_vector_at_angle(0.10),
    INDEX: unit_vector_at_angle(-0.20),
    F_CONCURRENCY: unit_vector_at_angle(0.41),
    F_REWRITE: unit_vector_at_angle(0.42),
    F_SAME_WEEK: unit_vector_at_angle(0.43),
    F_INDEX: unit_vector_at_angle(0.44),
    F_ALREADY: unit_vector_at_angle(0.45),
}

# What the fake says when it is shown a sentence: fact text -> (hint, quote).
ANSWERS = {
    F_CONCURRENCY: ("led_to", "could not take the write concurrency so the store moved"),
    F_REWRITE: ("led_to", "is why the pool rewrite was necessary"),
    # A plausible cause, phrased in words that are not in the sentence. This is
    # what a model reasoning from the world rather than reading looks like.
    F_INDEX: ("caused_by", "the node table had no index on id"),
}


class FakeExtractor:
    """The model's seat, filled by something that cannot surprise us.

    Records every sentence it was shown, which is how the cue filter is tested:
    the assertion that a co-occurrence sentence is never inferred from is only
    meaningful if we can see that nothing was asked about it.
    """

    name = "fake-extractor"

    def __init__(self, answers: dict[str, tuple[str, str]] | None = None):
        self._answers = ANSWERS if answers is None else answers
        self.seen: list[str] = []
        self.calls = 0

    def __call__(self, batch):
        self.calls += 1
        found = {}
        for candidate in batch:
            self.seen.append(candidate.fact)
            answer = self._answers.get(candidate.fact)
            if answer:
                found[candidate.edge_id] = {"hint": answer[0], "quote": answer[1]}
        return found


def _embedder():
    return VectorEmbedder(dict(VECTORS))


def _write(conn, embedder, source, target, fact, hint=None, session="s1"):
    payload = {
        "source": source, "target": target, "relation_type": "relates",
        "fact": fact, "confidence": "extracted",
    }
    if hint is not None:
        payload["causal_hint"] = hint
    result = write_episode(
        conn, GROUP, session,
        [{"name": source, "type": "thing"}, {"name": target, "type": "thing"}],
        [payload], {}, embedder, assume_new=True,
    )
    assert result.get("edges_created"), result
    return result["edges_created"][0]


def _seed(conn, embedder) -> dict[str, str]:
    """One store holding every case the command has to distinguish."""
    return {
        F_CONCURRENCY: _write(conn, embedder, SQLITE, SWITCH, F_CONCURRENCY),
        F_REWRITE: _write(conn, embedder, SWITCH, REWRITE, F_REWRITE),
        F_SAME_WEEK: _write(conn, embedder, DEPLOY, OUTAGE, F_SAME_WEEK),
        F_INDEX: _write(conn, embedder, INDEX, SWITCH, F_INDEX),
        F_ALREADY: _write(conn, embedder, OUTAGE, SQLITE, F_ALREADY, hint="contradicts"),
    }


def _args(**overrides):
    base = {"scope": "solo", "write": False, "clear": False, "rescan": False,
            "limit": 500, "batch_size": 8}
    return SimpleNamespace(**{**base, **overrides})


def _hint(conn, edge_id) -> tuple[str | None, str | None]:
    row = conn.execute(
        f"""SELECT (e.properties ->> '"causal_hint"'::agtype),
                   (e.properties ->> '"causal_hint_origin"'::agtype)
              FROM {GRAPH}."FACT" e WHERE e.id = %s::text::graphid""",
        (edge_id,),
    ).fetchone()
    return (row[0], row[1])


def _audit(conn, mutation_type: str) -> list[tuple]:
    return conn.execute(
        """SELECT affected_edge_ids::text[], summary, resolution_detail, writer_version
             FROM public.audit_entry
            WHERE group_id = %s AND mutation_type = %s
            ORDER BY id""",
        (GROUP, mutation_type),
    ).fetchall()


def _verdicts(conn) -> dict[str, str]:
    rows = conn.execute(
        "SELECT edge_id::text, verdict FROM public.causal_hint_scan WHERE group_id = %s",
        (GROUP,),
    ).fetchall()
    return {str(e): v for e, v in rows}


# --- the dry run --------------------------------------------------------------

def test_a_dry_run_changes_no_edge_and_says_what_it_would_set(migrated_db, capsys):
    conn = connect(migrated_db)
    edges = _seed(conn, _embedder())
    extractor = FakeExtractor()

    assert infer_causal.run(_args(), CONFIG, conn, extractor) == 0

    out = capsys.readouterr().out
    assert "Would set:" in out
    assert "Nothing was written" in out
    for edge_id in edges.values():
        assert _hint(conn, edge_id)[1] is None, "a dry run must not mark any edge"
    assert _hint(conn, edges[F_CONCURRENCY])[0] is None
    # The proposals survive the run, which is what lets --write apply what was
    # reviewed without asking the model a second time.
    assert len(infer_causal.pending(conn, GROUP)) == 2


def test_a_sentence_with_no_causal_connective_is_never_shown_to_a_model(migrated_db):
    """The first refusal, and the one that keeps this extraction. "The deploy and
    the outage happened the same week" is co-occurrence; a pass that asked a
    model about it would be inviting exactly the inference this project refuses
    - and would pay for the privilege."""
    conn = connect(migrated_db)
    edges = _seed(conn, _embedder())
    extractor = FakeExtractor()

    infer_causal.run(_args(), CONFIG, conn, extractor)

    assert F_SAME_WEEK not in extractor.seen
    assert _verdicts(conn)[edges[F_SAME_WEEK]] == infer_causal.NO_CUE


def test_a_hint_a_caller_wrote_is_not_a_candidate(migrated_db):
    conn = connect(migrated_db)
    _seed(conn, _embedder())
    extractor = FakeExtractor()

    infer_causal.run(_args(), CONFIG, conn, extractor)

    assert F_ALREADY not in extractor.seen


# --- the write ---------------------------------------------------------------

def test_write_sets_the_hint_marks_it_and_audits_it(migrated_db):
    from echo_memory import __version__

    conn = connect(migrated_db)
    edges = _seed(conn, _embedder())

    assert infer_causal.run(_args(write=True), CONFIG, conn, FakeExtractor()) == 0

    assert _hint(conn, edges[F_CONCURRENCY]) == ("led_to", infer_causal.ORIGIN)
    assert _hint(conn, edges[F_REWRITE]) == ("led_to", infer_causal.ORIGIN)

    entries = _audit(conn, "causal_hint_set")
    assert len(entries) == 2
    by_edge = {ids[0]: (summary, detail, version) for ids, summary, detail, version in entries}
    summary, detail, version = by_edge[edges[F_CONCURRENCY]]
    assert "led_to" in summary
    # The quote is the justification, so the trail has to carry it: without it
    # nobody reviewing this later can tell extraction from invention.
    assert "could not take the write concurrency so the store moved" in detail
    # A null writer_version after migration 0019 is read by `health` as proof
    # the writer predates the column, which would make every store this touched
    # report a stale server.
    assert version == __version__


def test_a_proposal_that_cannot_quote_the_fact_is_dropped(migrated_db, capsys):
    """The guard that does the real work. The fake answers F_INDEX with a cause
    that is true-sounding and not in the sentence, which is what a model
    reasoning from the world looks like from the outside."""
    conn = connect(migrated_db)
    edges = _seed(conn, _embedder())

    infer_causal.run(_args(write=True), CONFIG, conn, FakeExtractor())

    assert _hint(conn, edges[F_INDEX]) == (None, None)
    assert _verdicts(conn)[edges[F_INDEX]] == infer_causal.NO_CAUSE
    assert "quote was not in the fact's own text" in capsys.readouterr().out


def test_a_second_run_does_not_read_the_same_sentences_again(migrated_db):
    """Resumability, which at 38,479 facts is the difference between one bill and
    several. A verdict is recorded per fact examined, including the ones that
    stated nothing - those are most of them."""
    conn = connect(migrated_db)
    _seed(conn, _embedder())
    infer_causal.run(_args(write=True), CONFIG, conn, FakeExtractor())

    second = FakeExtractor()
    infer_causal.run(_args(write=True), CONFIG, conn, second)

    assert second.calls == 0 and second.seen == []


def test_a_window_smaller_than_the_store_is_resumed_not_restarted(migrated_db):
    conn = connect(migrated_db)
    _seed(conn, _embedder())

    first = FakeExtractor()
    infer_causal.run(_args(write=True, limit=2), CONFIG, conn, first)
    second = FakeExtractor()
    infer_causal.run(_args(write=True, limit=2), CONFIG, conn, second)

    # Four facts are candidates (the fifth already carries a caller's hint), and
    # no sentence is read twice across the two runs.
    assert set(first.seen).isdisjoint(second.seen)
    assert len(_verdicts(conn)) == 4


# --- taking it back ----------------------------------------------------------

def test_clear_removes_what_this_command_wrote_and_nothing_else(migrated_db, capsys):
    conn = connect(migrated_db)
    edges = _seed(conn, _embedder())
    infer_causal.run(_args(write=True), CONFIG, conn, FakeExtractor())

    assert infer_causal.run(_args(clear=True, write=True), CONFIG, conn) == 0

    assert _hint(conn, edges[F_CONCURRENCY]) == (None, None)
    assert _hint(conn, edges[F_REWRITE]) == (None, None)
    # Written by the caller in the episode that created it, so invisible to this.
    assert _hint(conn, edges[F_ALREADY])[0] == "contradicts"
    assert "Cleared 2 hint(s)" in capsys.readouterr().out

    cleared = _audit(conn, "causal_hint_cleared")
    assert len(cleared) == 2
    assert all("cleared causal_hint" in summary for _, summary, _, _ in cleared)


def test_clear_is_a_dry_run_too_unless_write_is_passed(migrated_db, capsys):
    conn = connect(migrated_db)
    edges = _seed(conn, _embedder())
    infer_causal.run(_args(write=True), CONFIG, conn, FakeExtractor())

    infer_causal.run(_args(clear=True), CONFIG, conn)

    assert "Would clear" in capsys.readouterr().out
    assert _hint(conn, edges[F_CONCURRENCY])[0] == "led_to"


def test_a_cleared_hint_is_not_proposed_again(migrated_db):
    """A clear is a judgement. Re-proposing what an operator has just rejected
    would make the command argue with them, and would charge them for it."""
    conn = connect(migrated_db)
    _seed(conn, _embedder())
    infer_causal.run(_args(write=True), CONFIG, conn, FakeExtractor())
    infer_causal.run(_args(clear=True, write=True), CONFIG, conn)

    again = FakeExtractor()
    infer_causal.run(_args(write=True), CONFIG, conn, again)

    assert again.calls == 0
    assert infer_causal.pending(conn, GROUP) == []


def test_rescan_reads_the_text_again_without_undoing_what_was_applied(migrated_db):
    conn = connect(migrated_db)
    edges = _seed(conn, _embedder())
    infer_causal.run(_args(write=True), CONFIG, conn, FakeExtractor())

    after = FakeExtractor()
    infer_causal.run(_args(rescan=True), CONFIG, conn, after)

    # The facts that stated nothing are read again; the two already typed are
    # not candidates any more, because they now carry a hint.
    assert F_INDEX in after.seen
    assert F_CONCURRENCY not in after.seen
    assert _hint(conn, edges[F_CONCURRENCY]) == ("led_to", infer_causal.ORIGIN)


# --- what it was all for -----------------------------------------------------

def test_a_backfilled_chain_is_walkable_by_trace_cause(migrated_db):
    """The point of the exercise. Two facts written by sessions that never
    mentioned causality assemble into a chain once their own sentences have been
    read, which is what `trace_cause` was shipped to do and has had nothing to
    do since."""
    conn = connect(migrated_db)
    embedder = _embedder()
    _seed(conn, embedder)

    before = trace_cause(conn, GROUP, SWITCH, embedder)
    assert before["causes"] == [] and before["effects"] == []
    assert "no causal_hint has been recorded" in before["note"]

    infer_causal.run(_args(write=True), CONFIG, conn, FakeExtractor())

    after = trace_cause(conn, GROUP, SWITCH, embedder)
    assert [link["fact"] for chain in after["causes"] for link in chain] == [F_CONCURRENCY]
    assert [link["fact"] for chain in after["effects"] for link in chain] == [F_REWRITE]


def test_nothing_to_do_says_so_without_a_provider(migrated_db, capsys):
    """An empty scope must not send anybody to configure a model key: there is
    nothing for a model to read."""
    conn = connect(migrated_db)
    assert infer_causal.run(_args(), CONFIG, conn) == 0
    assert "nothing left to examine" in capsys.readouterr().out
