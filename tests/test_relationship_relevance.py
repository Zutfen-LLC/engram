"""Unit tests for the candidate-profile relationship-relevance contract
(issue #190 / ENG-RECALL-003D — ``relationship_recall.compute_relationship_relevance``).

Pure-function coverage, no DB: the invariants the issue states mechanically —

* importance/source trust/memory confidence/human verification/review state/
  exposure counters/epistemic state are not inputs, so they cannot move
  relationship relevance by construction;
* graph/tunnel contribution is bounded to ``[0, 1]`` even for stored edge
  weights above 1;
* a direct hit's relevance is never demoted by links and an unlinked direct
  item's relevance is exactly its similarity (byte-compatibility with the
  pre-#190 signal contract);
* a relationship can only derive relevance from the source-seed score and
  its own bounded contribution;
* identical inputs produce identical relevance and ordering.
"""

from __future__ import annotations

import inspect

from engram.config import settings
from engram.relationship_recall import (
    RELATIONSHIP_RELEVANCE_VERSION,
    compute_relationship_relevance,
)


def _weights() -> tuple[float, float, float]:
    return (
        settings.relationship_score_weight_semantic,
        settings.relationship_score_weight_relationship,
        settings.relationship_score_weight_tunnel,
    )


def test_version_is_pinned():
    assert RELATIONSHIP_RELEVANCE_VERSION == "relationship-relevance-v1"


def test_unlinked_direct_hit_relevance_is_exactly_its_similarity():
    """Byte-compatibility property: with no graph/tunnel links the relevance
    contract must not rescale the direct semantic score — #190 packets
    without expandable relationships produce the pre-#190 values."""
    for similarity in (0.0, 0.25, 0.9, 1.0):
        result = compute_relationship_relevance(
            direct_semantic_score=similarity,
            source_seed_score=0.0,
        )
        assert result.relevance_score == round(similarity, 4)
        assert result.origins == ("semantic",)
        assert result.direct is True
        assert result.direct_semantic_score == similarity
        assert result.graph_contribution == 0.0
        assert result.graph_edge_types == ()
        assert result.tunnel_labels == ()


def test_links_never_demote_a_direct_hit():
    """Monotonicity: being additionally relationship-linked can only raise a
    direct item's relevance, never lower it below its own similarity."""
    similarity = 0.8
    for links in (
        [],
        [("mentions", 0.3)],
        [("supports", 1.0)],
        [("derived_from", 1.0), ("references", 0.6)],
    ):
        result = compute_relationship_relevance(
            direct_semantic_score=similarity,
            source_seed_score=1.0,
            graph_links=links,
            tunnel_labels=["w1<->w2"] if links else [],
        )
        assert result.relevance_score >= similarity
        assert 0.0 <= result.relevance_score <= 1.0


def test_graph_contribution_is_bounded_to_unit_interval():
    """A stored edge weight above 1 cannot inflate the contribution; the
    relevance stays within [0, 1]."""
    result = compute_relationship_relevance(
        direct_semantic_score=None,
        source_seed_score=1.0,
        graph_links=[("derived_from", 7.5)],
        tunnel_labels=["t"],
    )
    assert result.graph_contribution == 1.0
    w_sem, w_graph, w_tunnel = _weights()
    assert result.relevance_score == round(w_sem + w_graph + w_tunnel, 4)
    assert result.relevance_score <= 1.0


def test_expansion_only_relevance_is_bounded_by_seed_and_relationship_contract():
    """A relationship cannot create relevance outside the bounded
    source-seed/relationship contract: the max attainable relevance is the
    weighted combination, never more."""
    w_sem, w_graph, w_tunnel = _weights()
    maxed = compute_relationship_relevance(
        direct_semantic_score=None,
        source_seed_score=1.0,
        graph_links=[("derived_from", 1.0)],
        tunnel_labels=["t"],
    )
    assert maxed.relevance_score == round(w_sem + w_graph + w_tunnel, 4)
    assert maxed.relevance_score < 1.0

    # Weaker seed -> strictly weaker relevance under the same relationships.
    weaker = compute_relationship_relevance(
        direct_semantic_score=None,
        source_seed_score=0.5,
        graph_links=[("derived_from", 1.0)],
        tunnel_labels=["t"],
    )
    assert weaker.relevance_score < maxed.relevance_score


def test_strongest_edge_wins_and_edge_types_are_sorted_unique():
    result = compute_relationship_relevance(
        direct_semantic_score=None,
        source_seed_score=0.9,
        graph_links=[
            ("mentions", 0.3),
            ("supports", 0.6),
            ("supports", 0.2),
            ("derived_from", 0.95),
        ],
    )
    assert result.graph_contribution == 0.95
    assert result.graph_edge_types == ("derived_from", "mentions", "supports")


def test_origins_are_ordered_and_deduplicated():
    result = compute_relationship_relevance(
        direct_semantic_score=0.4,
        source_seed_score=0.7,
        graph_links=[("supports", 0.6)],
        tunnel_labels=["b", "a", "a"],
    )
    assert result.origins == ("semantic", "graph", "tunnel")
    assert result.tunnel_labels == ("a", "b")
    assert result.direct is True


def test_identical_inputs_produce_identical_relevance_and_components():
    kwargs = {
        "direct_semantic_score": None,
        "source_seed_score": 0.77,
        "graph_links": [("supports", 0.6), ("references", 0.4)],
        "tunnel_labels": ["ops"],
    }
    first = compute_relationship_relevance(**kwargs)
    second = compute_relationship_relevance(**kwargs)
    assert first == second
    assert first.payload() == second.payload()
    assert first.payload()["version"] == RELATIONSHIP_RELEVANCE_VERSION


def test_utility_and_epistemic_inputs_are_structurally_absent():
    """The excluded signal families are not parameters: importance, source
    trust, memory confidence, human verification, review state, exposure
    counters, and epistemic/risk state cannot be expressed at all."""
    parameters = inspect.signature(compute_relationship_relevance).parameters
    assert set(parameters) == {
        "direct_semantic_score",
        "source_seed_score",
        "graph_links",
        "tunnel_labels",
    }


def test_payload_exposes_structured_components():
    w_sem, w_graph, w_tunnel = _weights()
    result = compute_relationship_relevance(
        direct_semantic_score=None,
        source_seed_score=0.8,
        graph_links=[("supports", 0.5)],
        tunnel_labels=["eng<->ops"],
    )
    payload = result.payload()
    assert payload["components"] == {
        "semantic": round(w_sem * 0.8, 4),
        "graph": round(w_graph * 0.5, 4),
        "tunnel": round(w_tunnel * 1.0, 4),
    }
    assert payload["direct"] is False
    assert payload["direct_semantic_score"] is None
    assert payload["source_seed_score"] == 0.8
    assert payload["relevance_score"] == result.relevance_score


def test_direct_item_ignores_source_seed_score_for_its_semantic_component():
    """A merged (direct + expanded) item's semantic component is its own
    similarity, not the source seed's."""
    w_sem, w_graph, _ = _weights()
    result = compute_relationship_relevance(
        direct_semantic_score=0.6,
        source_seed_score=1.0,
        graph_links=[("supports", 0.8)],
    )
    assert result.components["semantic"] == round(w_sem * 0.6, 4)
    assert result.components["graph"] == round(w_graph * 0.8, 4)
