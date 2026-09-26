"""The published-benchmark harness, end to end against a real database.

The unit tests cover the reader and the scorer. What only a database can show
is the part that has failed twice before: that every turn the file holds is
actually written, and that the key scoring looks for is the key retrieval hands
back. Both of those failed silently in the standalone scripts this replaced -
419 turns became 18 when the entity assertions were missing, and 344 of 680
were deferred with nothing raised - and both looked like successful runs.

The real embedder, on purpose. A deterministic stand-in would prove the loop
runs and say nothing about whether a benchmark number came off the same code
path a user's query does.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from echo_memory.eval import external
from echo_memory.infra.db import connect
from echo_memory.ingestion.embeddings import LocalEmbedder
from echo_memory.retrieval.query_memory import query_memory

FIXTURES = Path(__file__).parent.parent / "fixtures"
LOCOMO = str(FIXTURES / "locomo_fixture.json")
LONGMEMEVAL = str(FIXTURES / "longmemeval_fixture.json")


@pytest.fixture(scope="module")
def embedder():
    model = LocalEmbedder()
    model.embed("warm")
    return model


def _facts_in(conn, group_id: str) -> int:
    return conn.execute(
        "SELECT count(*) FROM public.fact_embedding WHERE group_id = %s", (group_id,)
    ).fetchone()[0]


def test_every_turn_in_the_file_reaches_the_store(migrated_db, embedder):
    """The count is the whole assertion. A harness that loses turns reports
    retrieval over a corpus nobody has, in the flattering direction."""
    instance = next(external.load_locomo(LOCOMO))

    with connect(migrated_db) as conn:
        written = external.ingest(conn, instance, embedder)

        assert written == len(instance.turns) == 12
        assert _facts_in(conn, "locomo:fixture-conv-1") == 12


def test_a_long_turn_is_split_and_every_piece_is_stored(migrated_db, embedder):
    """write_episode refuses a fact over 4,000 characters by RETURNING an
    error. The fixture's handover turn is 4,190, so an unsplit write would be
    counted as a success and lose the one turn flagged as carrying the answer."""
    handover = list(external.load_longmemeval(LONGMEMEVAL))[2]
    pieces = [t for t in handover.turns if t.key == "fixture_answer_3#0"]

    with connect(migrated_db) as conn:
        external.ingest(conn, handover, embedder)

        assert len(pieces) == 2
        assert _facts_in(conn, handover.group_id) == len(handover.turns)


def test_a_refused_write_stops_the_run(migrated_db, embedder):
    """Never silently. The fact is 4,001 characters with no boundary to split
    on, which is what a caller ignoring the return value counts as written."""
    instance = external.Instance(
        instance_id="refusal", prefix="locomo",
        turns=(external.Turn(key="D1:1", node="D1:1", session="D1",
                             speaker="Ada", when="", text="x" * 4_001),),
        questions=(),
    )

    with connect(migrated_db) as conn, pytest.raises(external.WriteRefused):
        external.ingest(conn, instance, embedder)


def test_the_gold_key_is_the_key_retrieval_returns(migrated_db, embedder):
    """The join between ingest and scoring. If provenance and gold ever stop
    agreeing, every recall in the table is zero and nothing says why, so this
    asserts a real hit on a question whose answer is one distinctive turn."""
    instance = next(external.load_locomo(LOCOMO))
    adopted = next(q for q in instance.questions if q.question == "What animal did Ada adopt?")

    with connect(migrated_db) as conn:
        external.ingest(conn, instance, embedder)
        found = query_memory(conn, instance.group_id, adopted.question, 10, embedder)
        row = external.score_question(adopted, found["facts"], instance.sessions)

    assert row["hit@10"] == 1.0
    assert row["recall@10"] == 1.0
    assert row["session@10"] == 1.0
    assert row["answer_words@10"] > 0.0


def test_a_run_reports_what_it_ingested_and_scored(migrated_db, embedder, tmp_path):
    results = tmp_path / "rows.jsonl"

    with connect(migrated_db) as conn:
        out = external.run(conn, "locomo", LOCOMO, embedder, results_path=str(results))

    assert out.instances == 1
    assert out.turns_written == 12
    assert out.scopes_resumed == 0
    # Four of the fixture's five questions cite a turn; the fifth cites none.
    assert len(out.rows) == 4
    assert len(results.read_text().splitlines()) == 4

    payload = json.loads(json.dumps(external.report(out)))
    assert payload["dataset"] == "locomo"
    assert payload["dataset_sha256"]
    assert payload["overall"]["n"] == 4
    assert payload["accuracy"] is None


def test_a_finished_scope_is_scored_again_but_not_rewritten(migrated_db, embedder):
    """A full LongMemEval run is hours and the first attempt was killed near
    its end, losing everything. Resume is what makes the harness usable, and
    a resume that re-ingested would double every scope's facts."""
    with connect(migrated_db) as conn:
        first = external.run(conn, "locomo", LOCOMO, embedder)
        second = external.run(conn, "locomo", LOCOMO, embedder)

        assert first.turns_written == 12
        assert second.turns_written == 0
        assert second.scopes_resumed == 1
        assert len(second.rows) == len(first.rows)
        assert _facts_in(conn, "locomo:fixture-conv-1") == 12


def test_a_half_written_scope_is_rewritten_rather_than_trusted(migrated_db, embedder):
    """A partial haystack would score as a retrieval failure and look like a
    result, so the resume check demands the exact expected count."""
    instance = next(external.load_locomo(LOCOMO))
    half = external.Instance(
        instance_id=instance.instance_id, prefix=instance.prefix,
        turns=instance.turns[:6], questions=instance.questions,
    )

    with connect(migrated_db) as conn:
        external.ingest(conn, half, embedder)

        assert external.already_ingested(conn, instance.group_id, 6) is True
        assert external.already_ingested(conn, instance.group_id, 12) is False


def test_the_guard_counts_facts_outside_the_benchmark_scopes(migrated_db, embedder):
    """Pointing a 246,930 fact run at a real store is unrecoverable in
    practice, so the CLI refuses when the database holds anything else."""
    instance = next(external.load_locomo(LOCOMO))

    with connect(migrated_db) as conn:
        external.ingest(conn, instance, embedder)

        assert external.foreign_facts(conn, "locomo") == 0
        assert external.foreign_facts(conn, "lme") == 12
