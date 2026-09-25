"""End-to-end tests for write_episode against a real Postgres+AGE+pgvector
database. See conftest.py for the migrated_db fixture and DB-reachability
skip.

Uses the real LocalEmbedder where genuine semantic behavior matters, and the
deterministic VectorEmbedder (fake_embedder.py) where a test needs an exact,
known cosine similarity to hit a specific threshold boundary; a real model's
similarity for arbitrary strings isn't controllable enough for that.
"""

from fake_embedder import REFERENCE, VectorEmbedder, unit_vector_at_angle

from echo_memory.infra.db import GRAPH_NAME, connect
from echo_memory.ingestion.embeddings import LocalEmbedder
from echo_memory.ingestion.write_episode import write_episode


def test_new_entities_and_facts_create_nodes_edges_embeddings(migrated_db):
    conn = connect(migrated_db)
    embedder = LocalEmbedder()

    result = write_episode(
        conn,
        "g1",
        "sess-1",
        [{"name": "Postgres", "type": "tool"}, {"name": "AGE decision", "type": "decision"}],
        [
            {
                "source": "AGE decision",
                "target": "Postgres",
                "relation_type": "uses",
                "fact": "decided to use Postgres",
                "confidence": "extracted",
            }
        ],
        {},
        embedder,
    )

    assert result["ambiguous_entities"] == []
    assert len(result["edges_created"]) == 1

    (node_count,) = conn.execute("SELECT count(*) FROM public.node_embedding").fetchone()
    assert node_count == 2
    (edge_count,) = conn.execute("SELECT count(*) FROM public.fact_embedding").fetchone()
    assert edge_count == 1
    (audit_count,) = conn.execute(
        "SELECT count(*) FROM public.audit_entry WHERE mutation_type = 'created'"
    ).fetchone()
    assert audit_count == 1


def test_exact_match_reuses_existing_node_case_insensitive(migrated_db):
    conn = connect(migrated_db)
    embedder = LocalEmbedder()

    write_episode(
        conn,
        "g1",
        "sess-1",
        [{"name": "Postgres", "type": "tool"}, {"name": "AGE decision", "type": "decision"}],
        [
            {
                "source": "AGE decision",
                "target": "Postgres",
                "relation_type": "uses",
                "fact": "decided to use Postgres",
                "confidence": "extracted",
            }
        ],
        {},
        embedder,
    )
    result2 = write_episode(
        conn,
        "g1",
        "sess-2",
        [{"name": "postgres", "type": "tool"}, {"name": "AGE decision", "type": "decision"}],
        [
            {
                "source": "AGE decision",
                "target": "postgres",
                "relation_type": "mentions",
                "fact": "brought up postgres again",
                "confidence": "extracted",
            }
        ],
        {},
        embedder,
    )

    assert result2["ambiguous_entities"] == []
    (node_count,) = conn.execute("SELECT count(*) FROM public.node_embedding").fetchone()
    assert node_count == 2, "exact (case-insensitive) match must not create a duplicate node"
    (resolved_count,) = conn.execute(
        "SELECT count(*) FROM public.audit_entry WHERE mutation_type = 'entity_resolved' "
        "AND resolution_detail = 'exact match'"
    ).fetchone()
    assert resolved_count == 2  # both entities in the second call matched exactly

    (postgres_node_id,) = conn.execute(
        """SELECT * FROM cypher('echo_memory', $$
            MATCH (n:Node {name: 'Postgres'}) RETURN id(n)
        $$) AS (id agtype)"""
    ).fetchone()
    (affected_node_id,) = conn.execute(
        "SELECT affected_node_id FROM public.audit_entry "
        "WHERE mutation_type = 'entity_resolved' LIMIT 1"
    ).fetchone()
    assert str(affected_node_id) == str(postgres_node_id)


def test_fact_superseded_invalidates_old_edge(migrated_db):
    conn = connect(migrated_db)
    embedder = LocalEmbedder()

    entities = [{"name": "Postgres", "type": "tool"}, {"name": "AGE decision", "type": "decision"}]
    fact_template = {
        "source": "AGE decision",
        "target": "Postgres",
        "relation_type": "uses",
        "confidence": "extracted",
    }

    r1 = write_episode(
        conn, "g1", "s1", entities,
        [{**fact_template, "fact": "decided to use Postgres for storage"}], {}, embedder,
    )
    r2 = write_episode(
        conn, "g1", "s2", entities,
        [{**fact_template, "fact": "switched to Postgres for durability"}], {}, embedder,
    )

    old_edge_id, new_edge_id = r1["edges_created"][0], r2["edges_created"][0]
    assert old_edge_id != new_edge_id

    (t_invalid,) = conn.execute(
        f"""SELECT * FROM cypher('echo_memory', $$
            MATCH ()-[e:FACT]->() WHERE id(e) = {old_edge_id}
            RETURN e.t_invalid
        $$) AS (t_invalid agtype)"""
    ).fetchone()
    assert t_invalid is not None

    (superseded_count,) = conn.execute(
        "SELECT count(*) FROM public.audit_entry WHERE mutation_type = 'fact_superseded' "
        "AND %s = ANY(affected_edge_ids) AND %s = ANY(affected_edge_ids)",
        (old_edge_id, new_edge_id),
    ).fetchone()
    assert superseded_count == 1


def test_ambiguous_similarity_defers_and_creates_no_edge(migrated_db):
    conn = connect(migrated_db)
    embedder = VectorEmbedder({"Postgres": REFERENCE, "Postgres DB": unit_vector_at_angle(0.80)})

    write_episode(
        conn, "g1", "s1",
        [{"name": "Postgres", "type": "tool"}],
        [], {}, embedder,
    )
    result = write_episode(
        conn, "g1", "s2",
        [{"name": "Postgres DB", "type": "tool"}, {"name": "Postgres", "type": "tool"}],
        [
            {
                "source": "Postgres DB",
                "target": "Postgres",
                "relation_type": "mentions",
                "fact": "irrelevant, should be skipped",
                "confidence": "extracted",
            }
        ],
        {},
        embedder,
    )

    assert result["edges_created"] == []
    assert len(result["ambiguous_entities"]) == 1
    ambiguous = result["ambiguous_entities"][0]
    assert ambiguous["mention"] == "Postgres DB"
    assert ambiguous["candidates"][0]["name"] == "Postgres"
    assert 0.79 < ambiguous["candidates"][0]["similarity"] < 0.81

    (node_count,) = conn.execute("SELECT count(*) FROM public.node_embedding").fetchone()
    assert node_count == 1, "an ambiguous mention must not create a node until resolved"


def test_high_similarity_is_offered_for_confirmation_not_merged(migrated_db):
    """It used to merge on its own above 0.92. SILENT_MERGE is off since
    2026-09-13: calibration put precision at that bar at 50% over two reviewed
    pairs, and the audit log showed the unattended path had fired exactly once
    in the store's history - so it bought almost nothing and was the only way
    two entities could be joined with nobody watching.

    What must not change is the node count. A near-match still has to avoid
    minting a second node for one entity, which is the duplicate bar the trial
    counts; it now does that by waiting for an answer instead of guessing.
    """
    conn = connect(migrated_db)
    embedder = VectorEmbedder(
        {
            "Postgres": REFERENCE,
            "postgres-db": unit_vector_at_angle(0.95),
            "self-reference via alias": unit_vector_at_angle(0.5),
        }
    )
    entities = [{"name": "postgres-db", "type": "tool"}, {"name": "Postgres", "type": "tool"}]
    facts = [{
        "source": "postgres-db", "target": "Postgres", "relation_type": "mentions",
        "fact": "self-reference via alias", "confidence": "extracted",
    }]

    write_episode(conn, "g1", "s1", [{"name": "Postgres", "type": "tool"}], [], {}, embedder)
    asked = write_episode(conn, "g1", "s2", entities, facts, {}, embedder)

    assert [a["mention"] for a in asked["ambiguous_entities"]] == ["postgres-db"]
    assert asked["edges_created"] == []
    (node_count,) = conn.execute("SELECT count(*) FROM public.node_embedding").fetchone()
    assert node_count == 1, "an unanswered near-match must not create a second node"


def test_a_confirmed_match_merges_and_appends_the_alias(migrated_db):
    """The alias half is what makes a resolution a merge rather than a misfiled
    fact, and it is the half that gets missed in cleanup - one store carried a
    node answering to an unrelated name for eight days because of it."""
    conn = connect(migrated_db)
    embedder = VectorEmbedder(
        {
            "Postgres": REFERENCE,
            "postgres-db": unit_vector_at_angle(0.95),
            "self-reference via alias": unit_vector_at_angle(0.5),
        }
    )
    entities = [{"name": "postgres-db", "type": "tool"}, {"name": "Postgres", "type": "tool"}]
    facts = [{
        "source": "postgres-db", "target": "Postgres", "relation_type": "mentions",
        "fact": "self-reference via alias", "confidence": "extracted",
    }]

    write_episode(conn, "g1", "s1", [{"name": "Postgres", "type": "tool"}], [], {}, embedder)
    asked = write_episode(conn, "g1", "s2", entities, facts, {}, embedder)
    node_id = asked["ambiguous_entities"][0]["candidates"][0]["node_id"]

    result = write_episode(
        conn, "g1", "s2", entities, facts,
        {"postgres-db": {"resolved_to": node_id}}, embedder,
    )

    assert result["ambiguous_entities"] == []
    assert len(result["edges_created"]) == 1
    (node_count,) = conn.execute("SELECT count(*) FROM public.node_embedding").fetchone()
    assert node_count == 1, "a confirmed match must not create a new node"

    (aliases,) = conn.execute(
        """SELECT * FROM cypher('echo_memory', $$
            MATCH (n:Node {name: 'Postgres'}) RETURN n.aliases
        $$) AS (aliases agtype)"""
    ).fetchone()
    assert "postgres-db" in str(aliases)


def test_entity_resolutions_confirms_ambiguous_match(migrated_db):
    conn = connect(migrated_db)
    embedder = VectorEmbedder(
        {
            "Postgres": REFERENCE,
            "Postgres DB": unit_vector_at_angle(0.80),
            "confirmed later": unit_vector_at_angle(0.5),
        }
    )

    write_episode(conn, "g1", "s1", [{"name": "Postgres", "type": "tool"}], [], {}, embedder)
    first = write_episode(
        conn, "g1", "s2",
        [{"name": "Postgres DB", "type": "tool"}, {"name": "Postgres", "type": "tool"}],
        [
            {
                "source": "Postgres DB",
                "target": "Postgres",
                "relation_type": "mentions",
                "fact": "confirmed later",
                "confidence": "extracted",
            }
        ],
        {},
        embedder,
    )
    node_id = first["ambiguous_entities"][0]["candidates"][0]["node_id"]

    second = write_episode(
        conn, "g1", "s2",
        [{"name": "Postgres DB", "type": "tool"}, {"name": "Postgres", "type": "tool"}],
        [
            {
                "source": "Postgres DB",
                "target": "Postgres",
                "relation_type": "mentions",
                "fact": "confirmed later",
                "confidence": "extracted",
            }
        ],
        {"Postgres DB": {"resolved_to": node_id, "rationale": "same thing"}},
        embedder,
    )

    assert second["ambiguous_entities"] == []
    assert len(second["edges_created"]) == 1
    (node_count,) = conn.execute("SELECT count(*) FROM public.node_embedding").fetchone()
    assert node_count == 1


def test_entity_resolutions_rejects_as_new(migrated_db):
    conn = connect(migrated_db)
    embedder = VectorEmbedder(
        {
            "Postgres": REFERENCE,
            "Postgres DB": unit_vector_at_angle(0.80),
            "deferred": unit_vector_at_angle(0.5),
            "actually a different thing": unit_vector_at_angle(0.3),
        }
    )

    write_episode(conn, "g1", "s1", [{"name": "Postgres", "type": "tool"}], [], {}, embedder)
    write_episode(
        conn, "g1", "s2",
        [{"name": "Postgres DB", "type": "tool"}, {"name": "Postgres", "type": "tool"}],
        [
            {
                "source": "Postgres DB",
                "target": "Postgres",
                "relation_type": "mentions",
                "fact": "deferred",
                "confidence": "extracted",
            }
        ],
        {},
        embedder,
    )
    second = write_episode(
        conn, "g1", "s2",
        [{"name": "Postgres DB", "type": "tool"}, {"name": "Postgres", "type": "tool"}],
        [
            {
                "source": "Postgres DB",
                "target": "Postgres",
                "relation_type": "mentions",
                "fact": "actually a different thing",
                "confidence": "extracted",
            }
        ],
        {"Postgres DB": {"resolved_to": "new"}},
        embedder,
    )

    assert len(second["edges_created"]) == 1
    (node_count,) = conn.execute("SELECT count(*) FROM public.node_embedding").fetchone()
    assert node_count == 2, "an explicit 'new' resolution must create its own node"


def test_validation_rejects_bad_confidence(migrated_db):
    conn = connect(migrated_db)
    embedder = VectorEmbedder({"X": REFERENCE})
    result = write_episode(
        conn, "g1", "s1", [{"name": "X", "type": "tool"}],
        [{"source": "X", "target": "X", "relation_type": "uses", "fact": "f", "confidence": "certain"}],
        {}, embedder,
    )
    assert "error" in result
    assert "confidence" in result["error"]


def test_validation_rejects_fact_referencing_unknown_entity(migrated_db):
    conn = connect(migrated_db)
    embedder = VectorEmbedder({"X": REFERENCE})
    result = write_episode(
        conn, "g1", "s1", [{"name": "X", "type": "tool"}],
        [{"source": "X", "target": "Y", "relation_type": "uses", "fact": "f", "confidence": "extracted"}],
        {}, embedder,
    )
    assert "error" in result
    assert "not in entities" in result["error"]


def test_validation_rejects_too_many_entities(migrated_db):
    conn = connect(migrated_db)
    embedder = VectorEmbedder({})
    entities = [{"name": f"e{i}", "type": "tool"} for i in range(51)]
    result = write_episode(conn, "g1", "s1", entities, [], {}, embedder)
    assert "error" in result
    assert "too many entities" in result["error"]


def test_low_scoring_true_duplicate_gets_surfaced_not_silently_missed(migrated_db):
    """Regression test for a real measured gap: "AGE" vs "Apache AGE" score
    0.497 with the real embedder, well below the threshold this started at
    (0.75). See resolution.py's module docstring."""
    conn = connect(migrated_db)
    embedder = LocalEmbedder()

    write_episode(conn, "g1", "s1", [{"name": "AGE", "type": "tool"}], [], {}, embedder)
    result = write_episode(
        conn, "g1", "s2", [{"name": "Apache AGE", "type": "tool"}], [], {}, embedder
    )

    assert len(result["ambiguous_entities"]) == 1
    assert result["ambiguous_entities"][0]["mention"] == "Apache AGE"
    assert result["ambiguous_entities"][0]["candidates"][0]["name"] == "AGE"


def test_negation_pair_never_silently_merges_even_at_high_similarity(migrated_db):
    """Regression test for a real measured risk: "t_valid" vs "t_invalid"
    score 0.867 with the real embedder, inside what would otherwise be
    silent-merge territory for a naive threshold. The deterministic guard
    must force this to ambiguous regardless of the score."""
    conn = connect(migrated_db)
    embedder = LocalEmbedder()

    write_episode(conn, "g1", "s1", [{"name": "t_valid", "type": "tool"}], [], {}, embedder)
    result = write_episode(
        conn, "g1", "s2", [{"name": "t_invalid", "type": "tool"}], [], {}, embedder
    )

    assert len(result["ambiguous_entities"]) == 1
    (node_count,) = conn.execute("SELECT count(*) FROM public.node_embedding").fetchone()
    assert node_count == 1, "t_invalid must not silently merge into t_valid"


def test_superseding_a_fact_is_reported_to_the_agent(migrated_db):
    """Supersession is the commonest contradiction and it already happened
    silently: _find_active_edge invalidated the predecessor, wrote an audit
    entry, and told the caller nothing. An agent that just overwrote what it
    recorded last week had no way to know it had."""
    conn = connect(migrated_db)
    embedder = LocalEmbedder()
    ents = [{"name": "api", "type": "service"}, {"name": "prod", "type": "env"}]

    def episode(session, text):
        return write_episode(
            conn, "g1", session, ents,
            [{"source": "api", "target": "prod", "relation_type": "runs_on",
              "fact": text, "confidence": "extracted"}],
            {}, embedder,
        )

    first = episode("sess-1", "api runs on prod")
    second = episode("sess-2", "api runs on staging")

    assert first["superseded"] == [], "nothing to supersede on the first write"
    assert len(second["superseded"]) == 1
    assert second["superseded"][0]["replaced"] == "api runs on prod"
    assert second["superseded"][0]["with"] == "api runs on staging"
    assert second["superseded"][0]["fact_id"]


def test_two_agents_writing_one_triple_leave_one_active_edge(migrated_db):
    """`adopt` wires six clients to one shared scope, so one triple can be
    written twice at once. Two active edges for one triple - or two nodes for
    one entity - is criterion 6's own duplicate bar, inflated by the command
    meant to make that gate measurable.

    This passes both with and without the explicit advisory lock, because
    _increment_write_episode_count's upsert on group_state already takes a row
    lock as the transaction's first statement. That is worth pinning: the
    invariant is what matters, and it currently rests on a counter nobody
    documented as load-bearing. If someone moves or removes that counter, this
    test is what notices."""
    import threading

    embedder = LocalEmbedder()
    ents = [{"name": "api", "type": "service"}, {"name": "prod", "type": "env"}]
    barrier = threading.Barrier(2)
    errors = []

    def writer(session, text):
        try:
            conn = connect(migrated_db)
            barrier.wait(timeout=10)
            write_episode(
                conn, "g-race", session, ents,
                [{"source": "api", "target": "prod", "relation_type": "runs_on",
                  "fact": text, "confidence": "extracted"}],
                {}, embedder,
            )
            conn.close()
        except Exception as e:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(e)

    threads = [
        threading.Thread(target=writer, args=(f"s{i}", f"api runs on host-{i}"))
        for i in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"writer raised: {errors}"
    conn = connect(migrated_db)
    conn.execute("LOAD 'age'")
    conn.execute('SET search_path = ag_catalog, "$user", public')
    row = conn.execute(
        f"""SELECT * FROM cypher('{GRAPH_NAME}', $$
            MATCH ()-[e:FACT]->()
            WHERE e.group_id = 'g-race' AND e.t_invalid IS NULL
            RETURN count(e)
        $$) AS (n agtype)"""
    ).fetchone()
    assert int(str(row[0])) == 1, "two concurrent writers left two active edges"


def test_the_existing_edge_lookup_uses_an_index(migrated_db):
    """Every fact written checks whether this triple already has an active edge,
    to supersede rather than duplicate. Asked through Cypher as
    `MATCH (a)-[e:FACT]->(b) WHERE id(a) = $sid AND id(b) = $tid`, that cannot
    use an index - AGE expands the match and filters afterwards - so it walked
    every FACT edge in the scope on every fact. At 776 facts it cost 651 ms per
    call and 93% of a six-fact write.

    Pins the plan, because the answer was never wrong; only the way it was
    reached was."""
    conn = connect(migrated_db)
    embedder = LocalEmbedder()
    write_episode(
        conn, "g-plan", "sess-plan",
        [{"name": "Postgres", "type": "tool"}, {"name": "AGE decision", "type": "decision"}],
        [{"source": "AGE decision", "target": "Postgres", "relation_type": "uses",
          "fact": "decided to use Postgres", "confidence": "extracted"}],
        {}, embedder,
    )

    ends = conn.execute(
        f"""SELECT start_id::text, end_id::text FROM {GRAPH_NAME}."FACT" LIMIT 1"""
    ).fetchone()
    assert ends, "the episode wrote no edge"

    plan = "\n".join(
        r[0] for r in conn.execute(
            f"""EXPLAIN SELECT id::text FROM {GRAPH_NAME}."FACT"
                WHERE (properties ->> '"group_id"'::agtype) = %s
                  AND start_id = %s::text::graphid
                  AND end_id = %s::text::graphid""",
            ("g-plan", ends[0], ends[1]),
        ).fetchall()
    )
    assert "Seq Scan" not in plan, f"the edge lookup stopped using an index:\n{plan}"


def test_writing_the_same_triple_twice_still_supersedes(migrated_db):
    """The lookup moved off Cypher and onto the edge table. What it is for -
    finding the active edge for this exact (source, target, relation) so a
    second fact supersedes rather than duplicates - has to survive that."""
    conn = connect(migrated_db)
    embedder = LocalEmbedder()
    entities = [{"name": "deploy branch", "type": "policy"},
                {"name": "Acme", "type": "company"}]

    def _write(fact):
        return write_episode(
            conn, "g-sup", "sess-sup", entities,
            [{"source": "Acme", "target": "deploy branch", "relation_type": "deploys_from",
              "fact": fact, "confidence": "extracted"}],
            {}, embedder,
        )

    _write("the deploy branch is master")
    second = _write("the deploy branch is main, never master")

    assert second["superseded"], "the second write did not supersede the first"
    active = conn.execute(
        f"""SELECT count(*) FROM {GRAPH_NAME}."FACT"
            WHERE (properties ->> '"group_id"'::agtype) = 'g-sup'
              AND (properties ->> '"t_invalid"'::agtype) IS NULL"""
    ).fetchone()[0]
    assert active == 1, "superseding left two active edges for one triple"


def test_a_deferred_fact_is_never_embedded(migrated_db):
    """A fact touching an ambiguous mention waits for the caller to say which
    candidate it meant, so it is not written and must not be embedded either.
    Prefetching every fact up front did exactly that: work the write discarded,
    and on a strict embedder an error for a string the episode never stored."""
    embedder = VectorEmbedder({
        "Postgres": REFERENCE,
        "Postgres DB": unit_vector_at_angle(0.80),
        "written fact": REFERENCE,
        "Postgres Postgres. written fact": REFERENCE,
    })
    conn = connect(migrated_db)
    write_episode(
        conn, "g-defer", "s1", [{"name": "Postgres", "type": "tool"}],
        [{"source": "Postgres", "target": "Postgres", "relation_type": "is",
          "fact": "written fact", "confidence": "extracted"}],
        {"Postgres": {"resolved_to": "new"}}, embedder,
    )

    # "Postgres DB" scores mid-range against "Postgres": ambiguous, so its fact
    # is deferred. Its composed text is deliberately not registered, so the
    # embedder raises if anything tries to embed it.
    result = write_episode(
        conn, "g-defer", "s2", [{"name": "Postgres DB", "type": "tool"}],
        [{"source": "Postgres DB", "target": "Postgres DB", "relation_type": "is",
          "fact": "deferred fact nobody registered a vector for",
          "confidence": "extracted"}],
        {}, embedder,
    )

    assert result["ambiguous_entities"], "the mention was not treated as ambiguous"
    assert result["edges_created"] == []


def test_a_deferral_lists_only_candidates_at_or_above_the_bar(migrated_db):
    """The candidate list is the caller's evidence for a choice, and it used to
    be the whole top-5 regardless of score. A deferral caused by one 0.708
    match therefore also offered neighbours at 0.10 and 0.067 - entries that
    had no part in the decision and cannot be chosen on their merits. A reader
    of that list reasonably concluded the bar was somewhere near 0.06.

    All in the plane of the first two axes, so every similarity below is the
    cosine of an angle and not a model's opinion: the mention sits at 0.80
    from 'Postgres' and 0.12 from 'Nagoya'."""
    conn = connect(migrated_db)
    embedder = VectorEmbedder({
        "Postgres": REFERENCE,
        "Nagoya": unit_vector_at_angle(-0.50),
        "Postgres DB": unit_vector_at_angle(0.80),
    })

    for name in ("Postgres", "Nagoya"):
        write_episode(conn, "g1", "s1", [{"name": name, "type": "tool"}], [], {}, embedder)

    result = write_episode(
        conn, "g1", "s2", [{"name": "Postgres DB", "type": "tool"}], [], {}, embedder,
    )

    (ambiguous,) = result["ambiguous_entities"]
    offered = ambiguous["candidates"]
    assert [c["name"] for c in offered] == ["Postgres"], offered
    assert all(c["similarity"] >= 0.45 for c in offered)
