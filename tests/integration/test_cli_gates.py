"""The three commands that answer for the data, driven through main().

Coverage found these. `forget` was at 6.6%, `verify` at 8.6% and `retain` at
9.8%, the three least covered modules in the package that are not a competitor
adapter - and they are the three that erase customer data, prove it was not
tampered with, and decide how long it is kept.

The modules underneath them are tested hard. What was not tested is the layer
where `--confirm` lives, which is the only thing standing between a typo and
permanent deletion. A gate nothing exercises is a gate nobody has seen work.
"""

from __future__ import annotations

from fake_embedder import REFERENCE, VectorEmbedder

from echo_memory import server
from echo_memory.cli.main import main
from echo_memory.infra.config import Config
from echo_memory.infra.db import GRAPH_NAME as GRAPH
from echo_memory.infra.db import connect

SCOPE = "shared"


def _seed(migrated_db, fact: str = "billing uses Razorpay") -> tuple[Config, str]:
    config = Config(user_id="ayush", agent_id="claude-code", database_url=migrated_db)
    embedder = VectorEmbedder({fact: REFERENCE, "billing": REFERENCE, "Razorpay": REFERENCE})
    server.startup(config=config, embedder=embedder)
    result = server.write_episode(
        SCOPE, "sess-1",
        [{"name": "billing", "type": "x"}, {"name": "Razorpay", "type": "x"}],
        [{"source": "billing", "target": "Razorpay", "relation_type": "uses",
          "fact": fact, "confidence": "extracted"}],
    )
    assert result["edges_created"], result
    return config, result["edges_created"][0]


def _env(monkeypatch, config: Config) -> None:
    monkeypatch.setenv("ECHO_MEMORY_USER_ID", config.user_id)
    monkeypatch.setenv("ECHO_MEMORY_AGENT_ID", config.agent_id)
    monkeypatch.setenv("ECHO_MEMORY_DATABASE_URL", config.database_url)


def _facts(database_url: str) -> int:
    with connect(database_url) as conn:
        return conn.execute(
            f"""SELECT count(*) FROM {GRAPH}."FACT"
                 WHERE (properties ->> '"group_id"'::agtype) LIKE %s""",
            (f"%{SCOPE}%",),
        ).fetchone()[0]


# --- forget -------------------------------------------------------------


def test_forget_without_confirm_removes_nothing(migrated_db, monkeypatch, capsys):
    """The gate. There is no undo, the plan is cheap to compute, and an
    operator who sees "this would remove 412 facts" first is the difference
    between a correct erasure and an incident."""
    config, fact_id = _seed(migrated_db)
    _env(monkeypatch, config)
    before = _facts(migrated_db)

    code = main(["--scope", SCOPE, "forget", "--fact", fact_id])

    out = capsys.readouterr().out
    assert code == 0
    assert "Would erase" in out
    assert "Nothing has been erased" in out
    assert _facts(migrated_db) == before, "a dry run deleted a fact"


def test_forget_with_confirm_removes_it(migrated_db, monkeypatch, capsys):
    config, fact_id = _seed(migrated_db)
    _env(monkeypatch, config)
    before = _facts(migrated_db)

    code = main(["--scope", SCOPE, "forget", "--fact", fact_id, "--confirm",
                 "--reason", "subject request", "--by", "ayush"])

    assert code == 0
    assert "Erased" in capsys.readouterr().out
    assert _facts(migrated_db) == before - 1


def test_forget_refuses_two_targets_at_once(migrated_db, monkeypatch, capsys):
    """Ambiguity about WHAT is being erased is the one thing that must not
    reach the erasing code."""
    config, fact_id = _seed(migrated_db)
    _env(monkeypatch, config)

    code = main(["--scope", SCOPE, "forget", "--fact", fact_id, "--entity", "billing"])

    assert code == 1
    assert "exactly one" in capsys.readouterr().out


def test_forget_search_lists_without_erasing(migrated_db, monkeypatch, capsys):
    """A text match is not a subject. This path exists to refuse to act on
    one, so it has to be exercised rather than assumed."""
    config, _ = _seed(migrated_db)
    _env(monkeypatch, config)
    before = _facts(migrated_db)

    code = main(["--scope", SCOPE, "forget", "--search", "Razorpay"])

    assert code == 0
    assert "nothing is erased from this list automatically" in capsys.readouterr().out
    assert _facts(migrated_db) == before


# --- retain -------------------------------------------------------------


def test_retain_reports_that_nobody_has_decided(migrated_db, monkeypatch, capsys):
    """"No policy" and "we keep it forever" are different answers and only
    one of them is a finding."""
    config, _ = _seed(migrated_db)
    _env(monkeypatch, config)

    code = main(["--scope", SCOPE, "retain"])

    out = capsys.readouterr().out
    assert code == 0
    assert "has no retention policy" in out
    assert "not the same as keeping facts forever" in out


def test_retain_refuses_a_duration_with_no_reason(migrated_db, monkeypatch, capsys):
    """Refused at the CLI, before the module that would also refuse it. The
    message is the point: it says why a reason is required rather than naming
    a missing argument."""
    config, _ = _seed(migrated_db)
    _env(monkeypatch, config)

    code = main(["--scope", SCOPE, "retain", "--days", "90"])

    assert code == 1
    assert "not a compliance answer" in capsys.readouterr().out


def test_retain_enforce_without_confirm_removes_nothing(migrated_db, monkeypatch, capsys):
    config, _ = _seed(migrated_db)
    _env(monkeypatch, config)
    main(["--scope", SCOPE, "retain", "--days", "1",
          "--reason", "the dispute window", "--by", "ayush"])
    with connect(migrated_db) as conn:
        conn.execute(
            f"""UPDATE {GRAPH}."FACT"
                   SET properties = properties || ('{{"t_valid": ' ||
                       (extract(epoch FROM now())::bigint - 864000)::text || '}}')::agtype
                 WHERE (properties ->> '"group_id"'::agtype) LIKE %s""",
            (f"%{SCOPE}%",),
        )
    before = _facts(migrated_db)
    capsys.readouterr()

    code = main(["--scope", SCOPE, "retain", "--enforce"])

    out = capsys.readouterr().out
    assert code == 0
    assert "Would erase" in out
    assert "Nothing has been erased" in out
    assert _facts(migrated_db) == before, "a dry run deleted a fact"


def test_retain_enforce_with_confirm_removes_what_is_overdue(migrated_db, monkeypatch, capsys):
    config, _ = _seed(migrated_db)
    _env(monkeypatch, config)
    main(["--scope", SCOPE, "retain", "--days", "1",
          "--reason", "the dispute window", "--by", "ayush"])
    with connect(migrated_db) as conn:
        conn.execute(
            f"""UPDATE {GRAPH}."FACT"
                   SET properties = properties || ('{{"t_valid": ' ||
                       (extract(epoch FROM now())::bigint - 864000)::text || '}}')::agtype
                 WHERE (properties ->> '"group_id"'::agtype) LIKE %s""",
            (f"%{SCOPE}%",),
        )
    capsys.readouterr()

    code = main(["--scope", SCOPE, "retain", "--enforce", "--confirm", "--by", "ayush"])

    assert code == 0
    assert "Erased" in capsys.readouterr().out
    assert _facts(migrated_db) == 0


# --- verify -------------------------------------------------------------


def test_verify_is_clean_on_an_untouched_store(migrated_db, monkeypatch, capsys):
    config, _ = _seed(migrated_db)
    _env(monkeypatch, config)

    code = main(["--scope", SCOPE, "verify"])

    out = capsys.readouterr().out
    assert code == 0
    assert "Nothing has changed behind this store's back" in out


def test_verify_always_says_what_the_answer_is_worth(migrated_db, monkeypatch, capsys):
    """A report that said "verified" without this sentence would be the line
    somebody quotes. Unkeyed, the chain is worth exactly as much as a record
    of the head kept outside the database."""
    config, _ = _seed(migrated_db)
    _env(monkeypatch, config)

    main(["--scope", SCOPE, "verify"])

    assert "Unkeyed" in capsys.readouterr().out


def test_verify_exits_nonzero_when_a_fact_was_changed(migrated_db, monkeypatch, capsys):
    """The exit code matters as much as the text: this is the command a
    scheduled job runs, and a job reads the status rather than the prose."""
    config, fact_id = _seed(migrated_db)
    _env(monkeypatch, config)
    with connect(migrated_db) as conn:
        conn.execute(
            f"""UPDATE {GRAPH}."FACT"
                   SET properties = properties || '{{"fact": "billing uses Stripe"}}'::agtype
                 WHERE id = %s::graphid""",
            (fact_id,),
        )

    code = main(["--scope", SCOPE, "verify"])

    out = capsys.readouterr().out
    assert code == 1
    assert "no longer match what was attested" in out
    assert "NO RECORDED MUTATION ACCOUNTS FOR THIS" in out


def test_verify_head_prints_something_to_record_elsewhere(migrated_db, monkeypatch, capsys):
    config, _ = _seed(migrated_db)
    _env(monkeypatch, config)

    code = main(["--scope", SCOPE, "verify", "--head"])

    out = capsys.readouterr().out
    assert code == 0
    assert "sha256:" in out
    assert "outside the database" in out
