"""Expansion that follows asserted links, not shared endpoints.

The structural hop walks from a fact that matched to every fact touching
either of its entities, which is not a relation anybody stated: it is two
sentences naming the same thing. Measured, it costs 0.099 to 0.142 MRR on
three of four shapes.

So the property under test here is a restriction, and the test that matters is
the negative one: a fact that merely shares an entity must NOT come back. Every
other assertion in this file is satisfied by the structural hop too, so on its
own none of them would notice if the channel quietly walked structure.
"""

from __future__ import annotations

import pytest
from fake_embedder import REFERENCE, VectorEmbedder, unit_vector_at_angle
from hash_embedder import HashEmbedder

from echo_memory.infra.db import connect
from echo_memory.ingestion.write_episode import embedding_text, write_episode
from echo_memory.retrieval import authored
from echo_memory.retrieval.query_memory import _graph_candidates

GROUP = "g-authored"

#: How many facts share the seed's entity and assert nothing about it. More
#: than the five seeds the hop takes, so the content channels' top five is
#: entirely seed-plus-distractors and the asserted link is NOT among them.
#: With fewer, the link is itself a content match, the structural hop walks
#: from IT to the far fact, and the two channels become indistinguishable -
#: which is how the first version of the end-to-end test below passed with the
#: parameter dropped entirely.
DISTRACTORS = 6


def _fact(conn, source, target, text, embedder, hint=None, relation="uses") -> str:
    fact = {
        "source": source, "target": target, "relation_type": relation,
        "fact": text, "confidence": "extracted",
    }
    if hint is not None:
        fact["causal_hint"] = hint
    result = write_episode(
        conn, GROUP, f"ep-{text[:24]}",
        [{"name": source, "type": "thing"}, {"name": target, "type": "thing"}],
        [fact], None, embedder, assume_new=True,
    )
    assert result["edges_created"], result
    return result["edges_created"][0]


def _texts() -> list[tuple[str, str, str, str, str | None]]:
    """(key, source, target, fact, hint), in write order."""
    rows = [
        ("seed", "checkout", "the pool", "checkout borrows from the pool", None),
        ("link", "the pool", "the 502s",
         "sizing it at five meant gateway timeouts", "led_to"),
        ("far", "the 502s", "the postmortem",
         "the timeouts are written up in the postmortem", None),
    ]
    for i in range(DISTRACTORS):
        rows.append((
            f"distractor{i}", "the pool", f"neighbour {i}",
            f"the pool is also wired to neighbour {i}", None,
        ))
    return rows


def _embedder(query: str = "checkout") -> VectorEmbedder:
    """Deterministic ranking, stated rather than hoped for.

    The query, the seed and the distractors are near each other; the link and
    the far fact are not, so neither reaches the content channels' top five
    and the only way either can enter an answer is the hop. A hash embedder
    gives an arbitrary order here, and an arbitrary order decides which
    channel wins by accident.
    """
    vectors = {query: REFERENCE}
    for key, source, target, fact, _hint in _texts():
        if key == "seed":
            angle = 1.0
        elif key.startswith("distractor"):
            # Distinct angles, so their order among themselves is fixed too.
            angle = 0.95 - 0.01 * int(key.removeprefix("distractor"))
        else:
            angle = 0.05
        vectors[embedding_text(source, target, fact)] = unit_vector_at_angle(angle)
        vectors[fact] = unit_vector_at_angle(angle)
        for name in (source, target):
            vectors.setdefault(name, unit_vector_at_angle(0.2))
    return VectorEmbedder(vectors)


def _corpus(conn, embedder=None) -> dict[str, str]:
    """One seed, one authored link off it, one fact on the far side of that
    link, and six facts that share the seed's entity and assert nothing.

    The distractors are the point. A popular entity in a real store has dozens
    of them, they are what the structural hop returns, and they are why it
    dilutes an answer.
    """
    embedder = embedder or _embedder()
    return {
        key: _fact(conn, source, target, fact, embedder, hint=hint)
        for key, source, target, fact, hint in _texts()
    }


def test_it_walks_the_link_and_what_the_link_points_at(migrated_db):
    """Both tiers, in order: the asserted link first, then the fact on its far
    end. The far fact is the one a two-fact question needs, and reaching it is
    the entire argument for expanding at all."""
    with connect(migrated_db) as conn:
        ids = _corpus(conn)
        got = authored.candidates(conn, GROUP, [ids["seed"]], limit=50)

    assert got[0] == ids["link"], "the asserted link was not returned first"
    assert ids["far"] in got, "the far end of the link was never reached"


def test_a_fact_that_merely_shares_an_entity_is_not_a_link(migrated_db):
    """The restriction this channel exists for, and the only assertion in this
    file the structural hop would fail."""
    with connect(migrated_db) as conn:
        ids = _corpus(conn)
        got = authored.candidates(conn, GROUP, [ids["seed"]], limit=50)
        structural = _graph_candidates(conn, GROUP, [ids["seed"]], 50)

    distractors = {ids[k] for k in ids if k.startswith("distractor")}
    assert not distractors & set(got), (
        "a fact that only shares an entity came back as an authored link"
    )
    # And the contrast, measured in the same breath rather than asserted: the
    # structural hop returns every one of them.
    assert distractors <= set(structural), (
        "the structural hop stopped returning shared-endpoint facts, so this "
        "comparison no longer says anything"
    )


def test_a_scope_with_no_asserted_links_expands_to_nothing(migrated_db):
    """What makes a flat ablation row readable. On a corpus whose facts carry
    no hints - which is every corpus this project has measured on, 6 links
    across 2,001 facts in the store this was written against - the channel
    must return nothing at all, so a score identical to shipping is a
    statement about the corpus rather than a channel that did something and
    changed nothing."""
    with connect(migrated_db) as conn:
        # HashEmbedder here and below: these assert on what the walk reaches,
        # which does not depend on ranking at all.
        seed = _fact(conn, "checkout", "the pool",
                     "checkout borrows from the pool", HashEmbedder())
        _fact(conn, "the pool", "pgbouncer",
              "the pool runs behind pgbouncer", HashEmbedder())

        assert authored.candidates(conn, GROUP, [seed], limit=50) == []
        assert _graph_candidates(conn, GROUP, [seed], 50) != []


def test_a_contradiction_is_a_link_worth_following(migrated_db):
    """`trace_cause` excludes `contradicts` because a contradiction is not a
    step in a causal chain. Retrieval is not walking a chain, and a fact that
    contradicts what matched is among the most useful things an answer can
    carry."""
    with connect(migrated_db) as conn:
        seed = _fact(conn, "checkout", "the pool",
                     "checkout borrows from the pool", HashEmbedder())
        against = _fact(
            conn, "the pool", "the runbook",
            "the runbook says the pool was never the cause",
            HashEmbedder(), hint="contradicts",
        )
        got = authored.candidates(conn, GROUP, [seed], limit=50)

    assert against in got


def test_the_order_is_fixed_rather_than_arbitrary(migrated_db):
    """Two runs of the same question must score the same thing twice. Within a
    tier the order is seed rank then edge id, which is arbitrary as relevance
    and FIXED, which is what a measurement needs."""
    with connect(migrated_db) as conn:
        ids = _corpus(conn)
        first = authored.candidates(conn, GROUP, [ids["seed"]], limit=50)
        again = authored.candidates(conn, GROUP, [ids["seed"]], limit=50)

    assert first == again
    assert first == sorted(first, key=lambda e: (first.index(e),))


def test_the_seeds_themselves_are_never_returned(migrated_db):
    """A seed is already in the content channels. Returning it here would hand
    the same fact a second top slot in the fusion, which is the mechanism
    behind three failed expansion experiments."""
    with connect(migrated_db) as conn:
        ids = _corpus(conn)
        got = authored.candidates(conn, GROUP, [ids["seed"], ids["link"]], limit=50)

    assert ids["seed"] not in got
    assert ids["link"] not in got


def test_it_does_not_leave_the_scope(migrated_db):
    """Scopes are tenants. An expansion that walked out of one would put
    another account's facts in an answer, and no later filter can be trusted
    to be the only thing standing between them."""
    with connect(migrated_db) as conn:
        ids = _corpus(conn)
        other = write_episode(
            conn, "g-authored-other", "ep-other",
            [{"name": "the pool", "type": "thing"},
             {"name": "another tenant", "type": "thing"}],
            [{"source": "the pool", "target": "another tenant",
              "relation_type": "uses",
              "fact": "the pool is why the other tenant paged",
              "confidence": "extracted", "causal_hint": "led_to"}],
            None, HashEmbedder(), assume_new=True,
        )["edges_created"][0]

        got = authored.candidates(conn, GROUP, [ids["seed"]], limit=50)

    assert other not in got


def test_a_misspelt_expansion_is_refused_rather_than_ignored(migrated_db):
    """A setting that is right while the behaviour it names is absent has
    shipped twice in this project and was caught late both times."""
    from echo_memory.retrieval.query_memory import query_memory

    with connect(migrated_db) as conn:
        _corpus(conn)
        with pytest.raises(ValueError, match="authored"):
            query_memory(
                conn, GROUP, "checkout", 10, _embedder(),
                graph_hops=1, expansion="authoured",
            )


def test_the_hop_actually_uses_the_channel_end_to_end(migrated_db):
    """The wiring, separately from the walk. `expansion` has to reach the hop:
    a parameter accepted and dropped would leave every test above passing
    while query_memory kept walking structure.

    Asserted on `matched`, which names the channel that found each fact,
    rather than on the order of the answer. On a corpus small enough to write
    down, the content channels return every fact either way and the two
    orderings come out identical - an ordering comparison here passed with the
    parameter dropped entirely, which is the whole failure this test exists to
    catch.
    """
    from echo_memory.retrieval.query_memory import query_memory

    with connect(migrated_db) as conn:
        ids = _corpus(conn)
        structural = query_memory(
            conn, GROUP, "checkout", 10, _embedder(), graph_hops=1,
        )
        asserted = query_memory(
            conn, GROUP, "checkout", 10, _embedder(), graph_hops=1,
            expansion="authored",
        )

    rev = {v: k for k, v in ids.items()}

    def by_graph(response):
        return {
            rev.get(str(f["fact_id"]), "?")
            for f in response["facts"]
            if "graph" in (f.get("matched") or [])
        }

    assert by_graph(asserted) == {"link", "far"}, (
        "the authored hop reached something other than the far end of the "
        f"asserted link: {by_graph(asserted)}"
    )
    # Not a named distractor: the first four are themselves content matches
    # and so are seeds, which the hop never returns. Any of them reaching the
    # answer through the hop is the contrast this test needs.
    assert {d for d in by_graph(structural) if d.startswith("distractor")}, (
        "the structural hop stopped reaching shared-endpoint facts, so this "
        "comparison no longer says anything"
    )
    assert "far" not in by_graph(structural), (
        "the structural hop reached the far fact in one hop, which would make "
        "the two channels indistinguishable on this corpus"
    )
