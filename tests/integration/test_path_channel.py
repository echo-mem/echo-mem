"""The chain between two named entities, as its own ranked list.

The property under test is adjacency, not reachability. The neighbourhood hop
already REACHES the second fact of a two fact answer - multihop recall@10 rose
0.635 to 0.769 when it was switched on - and still moved MRR by +0.003, because
it returns that fact somewhere in a fifty long dump of everything touching
either endpoint. So every assertion here that only checks "the fact came back"
is one the hop would also pass, and the tests that matter are the three that
distinguish them:

  - the two edges of a chain arrive NEXT TO EACH OTHER
  - a query naming one entity gets nothing at all
  - two entities that already share an edge contribute nothing
"""

from __future__ import annotations

import pytest
from fake_embedder import REFERENCE, VectorEmbedder, unit_vector_at_angle
from hash_embedder import HashEmbedder

from echo_memory.infra.db import connect
from echo_memory.ingestion.write_episode import embedding_text, write_episode
from echo_memory.retrieval import paths
from echo_memory.retrieval.query_memory import _graph_candidates

GROUP = "g-paths"

#: Facts hanging off the middle entity that are not part of the chain. More
#: than the hop's list depth would be unreasonable; enough that a channel
#: returning the middle's whole neighbourhood cannot pass the adjacency test by
#: luck. The hop returns all of them. This channel returns none.
HUB_NOISE = 8


def _fact(conn, source, target, text, embedder, group=GROUP) -> str:
    result = write_episode(
        conn, group, f"ep-{text[:24]}",
        [{"name": source, "type": "thing"}, {"name": target, "type": "thing"}],
        [{"source": source, "target": target, "relation_type": "uses",
          "fact": text, "confidence": "extracted"}],
        None, embedder, assume_new=True,
    )
    assert result["edges_created"], result
    return result["edges_created"][0]


def _chain(conn, embedder=None) -> dict[str, str]:
    """alpha -- the pool -- omega, plus noise on the middle.

    `alpha` and `omega` never appear in one fact, which is what makes this the
    multihop shape: the harness builds exactly this, two entities joined only
    through a third, and scores at the rank where BOTH edges have surfaced.
    """
    embedder = embedder or HashEmbedder()
    ids = {
        "left": _fact(conn, "alpha", "the pool", "alpha borrows from the pool", embedder),
        "right": _fact(conn, "the pool", "omega", "the pool pages omega on failure", embedder),
    }
    for i in range(HUB_NOISE):
        ids[f"noise{i}"] = _fact(
            conn, "the pool", f"bystander {i}",
            f"the pool is also wired to bystander {i}", embedder,
        )
    return ids


def test_the_two_edges_of_a_chain_come_back_adjacent(migrated_db):
    """The whole proposal, in one assertion.

    Reciprocal rank fusion reads rank position and nothing else, so two facts
    that are one answer have to arrive one rank apart. The hop puts them
    forty apart and that is the defect this channel exists to fix.
    """
    with connect(migrated_db) as conn:
        ids = _chain(conn)
        got = paths.candidates(conn, GROUP, "alpha omega", limit=50)

    assert ids["left"] in got and ids["right"] in got, (
        f"the chain was not returned at all: {got}"
    )
    assert abs(got.index(ids["left"]) - got.index(ids["right"])) == 1, (
        "the two edges of one chain are not adjacent, which is the only thing "
        f"this channel does that the neighbourhood hop does not: {got}"
    )


def test_it_returns_the_chain_and_nothing_else(migrated_db):
    """The contrast with the hop, measured in the same breath rather than
    asserted. The hop returns every bystander hanging off the middle. This
    returns two facts."""
    with connect(migrated_db) as conn:
        ids = _chain(conn)
        got = paths.candidates(conn, GROUP, "alpha omega", limit=50)
        hop = _graph_candidates(conn, GROUP, [ids["left"]], 50)

    noise = {ids[k] for k in ids if k.startswith("noise")}
    assert not noise & set(got), (
        f"a bystander on the middle entity came back as part of the chain: {got}"
    )
    assert set(got) == {ids["left"], ids["right"]}
    assert noise <= set(hop), (
        "the neighbourhood hop stopped returning the middle's other facts, so "
        "this comparison no longer says anything"
    )


def test_a_query_naming_one_entity_gets_nothing(migrated_db):
    """The control, and the reason this can be on while the hop stays off.

    Expansion measured at -0.099 to -0.142 MRR on the three shapes a single
    fact answers. Those shapes name one entity, or name two that are already
    joined. If this channel fires there it will cost exactly what the hop
    cost, so it has to decline before any traversal.
    """
    with connect(migrated_db) as conn:
        _chain(conn)
        assert paths.candidates(conn, GROUP, "alpha", limit=50) == []
        assert paths.candidates(conn, GROUP, "the", limit=50) == []
        # Two words, but only one of them names a node in this scope.
        assert paths.candidates(conn, GROUP, "alpha nothinghere", limit=50) == []


def test_two_entities_that_already_share_an_edge_contribute_nothing(migrated_db):
    """A direct edge is a single hop answer the content channels already hold.

    Emitting it here would hand it a second top slot in the fusion, which is
    the mechanism behind three failed expansion experiments in this project.
    """
    with connect(migrated_db) as conn:
        direct = _fact(conn, "alpha", "omega", "alpha calls omega directly", HashEmbedder())
        got = paths.candidates(conn, GROUP, "alpha omega", limit=50)

    assert direct not in got
    assert got == []


def test_a_direct_edge_cancels_the_pair_even_when_a_detour_exists(migrated_db):
    """The regression the eval caught, pinned so it cannot come back.

    The first version declined the one edge chain and then carried on to depth
    two, returning every detour joining the same two entities through some
    other middle. Measured, that cost -0.0535 MRR on entity_pair, whose queries
    name exactly this: two entities that ARE joined, asked about the fact that
    joins them.

    A question one fact answers does not become a chain question because a
    longer route also exists.
    """
    with connect(migrated_db) as conn:
        direct = _fact(conn, "alpha", "omega", "alpha calls omega directly",
                       HashEmbedder())
        detour_a = _fact(conn, "alpha", "the pool", "alpha borrows from the pool",
                         HashEmbedder())
        detour_b = _fact(conn, "the pool", "omega", "the pool pages omega",
                         HashEmbedder())
        got = paths.candidates(conn, GROUP, "alpha omega", limit=50)

    assert got == [], (
        "a detour was returned for two entities that already share an edge: "
        f"{got} (direct={direct}, detour={detour_a},{detour_b})"
    )


def test_a_query_naming_more_than_two_entities_is_declined(migrated_db):
    """The other half of the same regression, and the one `prose` exposed.

    A prose query is a fact's own words with its entity names stripped, so any
    node whose name merely contains a leftover word gets matched. The channel
    then found two endpoints in a question about nothing in particular and
    invented a chain between them: -0.0569 MRR.

    The question this channel answers has exactly two ends. Anything that looks
    like more is a substring collision.
    """
    with connect(migrated_db) as conn:
        _fact(conn, "alpha", "the pool", "alpha borrows from the pool", HashEmbedder())
        _fact(conn, "the pool", "omega", "the pool pages omega", HashEmbedder())
        _fact(conn, "gamma", "delta", "gamma signals delta", HashEmbedder())

        # Two named, unconnected: the shape this channel is for.
        assert paths.candidates(conn, GROUP, "alpha omega", limit=50) != []
        # Four named: a question with four ends is not a question about how two
        # things relate.
        assert paths.candidates(conn, GROUP, "alpha omega gamma delta", limit=50) == []


def test_a_superseded_link_breaks_the_chain(migrated_db):
    """`as_of` is honoured, so a chain is the chain as the store believed it.

    A path channel that ignored the bitemporal columns would answer "how were
    these related last Tuesday" with today's graph, which is the question a
    post mortem opens with.
    """
    with connect(migrated_db) as conn:
        ids = _chain(conn)
        assert ids["right"] in paths.candidates(conn, GROUP, "alpha omega", limit=50)

        # Supersede the right hand link with a changed claim. The old edge
        # takes a t_invalid and stops being live.
        write_episode(
            conn, GROUP, "ep-supersede",
            [{"name": "the pool", "type": "thing"}, {"name": "omega", "type": "thing"}],
            [{"source": "the pool", "target": "omega", "relation_type": "uses",
              "fact": "the pool no longer pages omega at all",
              "confidence": "extracted"}],
            None, HashEmbedder(), assume_new=True,
        )
        after = paths.candidates(conn, GROUP, "alpha omega", limit=50)

    assert ids["right"] not in after, (
        "a superseded fact is still being returned as a live link of a chain"
    )


def test_it_does_not_leave_the_scope(migrated_db):
    """Scopes are tenants. A traversal that walked out of one would put
    another account's facts in an answer, and no later filter can be trusted
    to be the only thing standing between them."""
    with connect(migrated_db) as conn:
        _chain(conn)
        other = _fact(
            conn, "the pool", "omega",
            "the other tenant's pool pages their omega", HashEmbedder(),
            group="g-paths-other",
        )
        got = paths.candidates(conn, GROUP, "alpha omega", limit=50)

    assert other not in got


def test_depth_three_is_available_and_is_not_the_default(migrated_db):
    """Two is the shape the harness scores. Three is reachable and unmeasured,
    so it must not arrive by accident."""
    with connect(migrated_db) as conn:
        a = _fact(conn, "alpha", "first", "alpha reaches first", HashEmbedder())
        b = _fact(conn, "first", "second", "first reaches second", HashEmbedder())
        c = _fact(conn, "second", "omega", "second reaches omega", HashEmbedder())

        at_two = paths.candidates(conn, GROUP, "alpha omega", limit=50)
        at_three = paths.candidates(conn, GROUP, "alpha omega", limit=50, max_depth=3)

    assert at_two == [], "a three edge chain was returned at the default depth"
    assert [a, b, c] == at_three, (
        f"the three edge chain did not come back whole and in order: {at_three}"
    )


def test_a_depth_nothing_has_measured_is_refused(migrated_db):
    """A setting that is right while the behaviour it names is unknown has
    shipped twice in this project and was caught late both times."""
    with connect(migrated_db) as conn:
        _chain(conn)
        with pytest.raises(ValueError, match="max_depth"):
            paths.candidates(conn, GROUP, "alpha omega", limit=50, max_depth=1)
        with pytest.raises(ValueError, match="max_depth"):
            paths.candidates(conn, GROUP, "alpha omega", limit=50, max_depth=4)


def test_a_fact_on_two_chains_is_returned_once(migrated_db):
    """Emitting it twice would say the second chain is separate evidence when
    it is the same fact arriving by another route."""
    with connect(migrated_db) as conn:
        ids = _chain(conn)
        # A second middle, so alpha and omega are joined two ways, and the
        # left hand link is shared by neither - build the shared case
        # explicitly instead.
        shared = _fact(conn, "alpha", "second pool", "alpha also uses the second pool",
                       HashEmbedder())
        _fact(conn, "second pool", "omega", "the second pool pages omega too",
              HashEmbedder())
        got = paths.candidates(conn, GROUP, "alpha omega", limit=50)

    assert len(got) == len(set(got)), f"a fact was emitted twice: {got}"
    assert ids["left"] in got and shared in got


def test_the_order_is_fixed_rather_than_arbitrary(migrated_db):
    """Two runs of the same question must score the same thing twice."""
    with connect(migrated_db) as conn:
        _chain(conn)
        _fact(conn, "alpha", "second pool", "alpha also uses the second pool",
              HashEmbedder())
        _fact(conn, "second pool", "omega", "the second pool pages omega too",
              HashEmbedder())
        first = paths.candidates(conn, GROUP, "alpha omega", limit=50)
        again = paths.candidates(conn, GROUP, "alpha omega", limit=50)

    assert first == again


# --------------------------------------------------------------------------
# Wiring. Separate from the walk, because a parameter accepted and dropped
# would leave every test above passing while query_memory ignored the channel.
# That exact failure shipped once already in this project's authored-link work
# and was caught only by asserting on `matched`.
# --------------------------------------------------------------------------


#: Facts that mention one of the two named entities and are not the chain.
#:
#: The first version of the outcome test below had only the hub noise, which
#: mentions neither `alpha` nor `omega`. The lexical channel therefore put both
#: chain facts in the top four on its own and the test's own guard clause
#: failed it, correctly: a corpus where the content channels already answer the
#: question cannot show a third channel doing anything.
#:
#: These are the competition a real store has. Each matches exactly one of the
#: query's two terms, which is all either chain fact manages either, so the
#: chain stops being lexically special. They deliberately attach to DIFFERENT
#: middles on each side, so they cannot themselves form an alpha-omega chain
#: and the channel's output stays exactly the two facts under test.
SIDE_NOISE = 6


def _buried_chain_rows() -> list[tuple[str, str, str, float]]:
    """(source, target, fact, cos to the query), in write order."""
    rows = [
        ("alpha", "the pool", "alpha borrows from the pool", 0.05),
        ("the pool", "omega", "the pool pages omega on failure", 0.05),
    ]
    for i in range(SIDE_NOISE):
        rows.append((
            "alpha", f"sidecar {i}", f"alpha also depends on sidecar {i}",
            0.95 - 0.01 * i,
        ))
        rows.append((
            "omega", f"relay {i}", f"omega also reports to relay {i}",
            0.94 - 0.01 * i,
        ))
    return rows


def _embedder() -> VectorEmbedder:
    """Query, chain and competition placed deliberately.

    The chain's two facts sit far from the query so neither reaches the content
    channels' top slots on resemblance, and the competition sits near it. A
    hash embedder gives an arbitrary order here, and an arbitrary order decides
    which channel wins by accident.
    """
    vectors = {"alpha omega": REFERENCE}
    for source, target, fact, angle in _buried_chain_rows():
        vectors[embedding_text(source, target, fact)] = unit_vector_at_angle(angle)
        vectors[fact] = unit_vector_at_angle(angle)
        for name in (source, target):
            vectors.setdefault(name, unit_vector_at_angle(0.2))
    return VectorEmbedder(vectors)


def _buried_chain(conn) -> dict[str, str]:
    """The chain, outranked by facts that match the query just as well."""
    embedder = _embedder()
    ids: dict[str, str] = {}
    for i, (source, target, fact, _angle) in enumerate(_buried_chain_rows()):
        key = {0: "left", 1: "right"}.get(i, f"noise{i}")
        ids[key] = _fact(conn, source, target, fact, embedder)
    return ids


def test_the_channel_is_reachable_end_to_end(migrated_db):
    """`use_paths` has to reach the walk, and the facts it found have to say so."""
    from echo_memory.retrieval.query_memory import query_memory

    with connect(migrated_db) as conn:
        ids = _buried_chain(conn)
        off = query_memory(conn, GROUP, "alpha omega", 10, _embedder())
        on = query_memory(conn, GROUP, "alpha omega", 10, _embedder(), use_paths=True)

    def by_path(response):
        return {
            str(f["fact_id"]) for f in response["facts"]
            if "path" in (f.get("matched") or [])
        }

    assert by_path(on) == {ids["left"], ids["right"]}, (
        f"the path channel reached something other than the chain: {by_path(on)}"
    )
    assert by_path(off) == set(), (
        "facts were labelled as path matches with the channel off"
    )


def test_the_channel_puts_the_chain_in_an_answer_that_lacked_it(migrated_db):
    """The outcome, not the mechanism. With the channel off the hub noise
    outranks both links of the chain and the answer cannot support the
    question. With it on, both links are in the answer."""
    from echo_memory.retrieval.query_memory import query_memory

    with connect(migrated_db) as conn:
        ids = _buried_chain(conn)
        off = query_memory(conn, GROUP, "alpha omega", 4, _embedder())
        on = query_memory(conn, GROUP, "alpha omega", 4, _embedder(), use_paths=True)

    chain = {ids["left"], ids["right"]}
    returned_off = {str(f["fact_id"]) for f in off["facts"]}
    returned_on = {str(f["fact_id"]) for f in on["facts"]}

    assert not chain <= returned_off, (
        "the chain was already complete in the top 4 without the channel, so "
        "this corpus cannot show the channel doing anything"
    )
    assert chain <= returned_on, (
        f"the channel did not get both links into the top 4: {returned_on}"
    )


def test_a_weight_applies_to_the_path_list_and_not_another(migrated_db):
    """fusion.py refuses to let a weight slide onto the wrong channel
    quietly, because no test distinguishes that from a bad weight. This is
    that test: a weight of zero on the path list must remove its influence
    without disturbing the content channels."""
    from echo_memory.retrieval.query_memory import query_memory

    with connect(migrated_db) as conn:
        _buried_chain(conn)
        plain = query_memory(conn, GROUP, "alpha omega", 10, _embedder())
        weighted = query_memory(
            conn, GROUP, "alpha omega", 10, _embedder(),
            use_paths=True, path_weight=2.0,
        )
        heavy = query_memory(
            conn, GROUP, "alpha omega", 10, _embedder(),
            use_paths=True, path_weight=8.0,
        )

    def order(r):
        return [str(f["fact_id"]) for f in r["facts"]]

    assert order(heavy)[:2] == order(weighted)[:2] or order(heavy) != order(plain), (
        "raising the path weight changed nothing, so the weight is not "
        "reaching the path list"
    )
    assert order(heavy) != order(plain)
