"""Tests for the read-only recall evaluation report."""

from __future__ import annotations

from evals.recall.contracts import usefulness_perturbation_report
from evals.recall.metrics import build_packet_change_metrics, build_profile_metrics


def test_metrics_are_deterministic_and_keep_unknown_labels_unknown() -> None:
    legacy = {
        "items": [{"id": "a"}, {"id": "b"}],
        "item_count": 2,
        "byte_count": 20,
        "effective_byte_budget": 100,
        "effective_token_budget": 50,
        "effective_item_budget": 3,
        "omitted_by_admission": {},
    }
    governed = {
        "items": [{"id": "b"}, {"id": "c"}],
        "item_count": 2,
        "byte_count": 18,
        "effective_byte_budget": 100,
        "effective_token_budget": 50,
        "effective_item_budget": 3,
        "omitted_by_admission": {"missing_assessment": 1},
    }

    change = build_packet_change_metrics(legacy, governed)
    profile = build_profile_metrics(
        [
            {
                "legacy": legacy,
                "candidate": governed,
                "labels": {
                    "contamination": {"a": "avoided", "c": "unknown"},
                    "usefulness": {"a": "useful", "c": "unknown"},
                },
            }
        ]
    )

    assert change == {
        "packet_count": 1,
        "identical_packet_rate": 0.0,
        "membership_change_rate": 1.0,
        "ordering_only_change_rate": 0.0,
        "mean_jaccard": 1 / 3,
        "added_items": 1,
        "removed_items": 1,
        "item_count_delta": 0,
        "byte_count_delta": -2,
        "omission_reasons": {"missing_assessment": 1},
    }
    assert profile["contamination"] == {
        "avoided": 1,
        "introduced": 0,
        "unknown": 1,
        "known_denominator": 1,
        "avoided_rate": 1.0,
        "introduced_rate": 0.0,
    }
    assert profile["usefulness"] == {
        "legacy_useful_retained": 0,
        "legacy_useful_withheld": 1,
        "candidate_only_useful": 0,
        "unknown": 1,
    }


def test_usefulness_perturbation_uses_the_shared_non_amplifying_contract() -> None:
    report = usefulness_perturbation_report()

    assert report["adjustments"] == {
        "none": 0.0,
        "positive": 0.10,
        "negative": -0.10,
        "mixed": 0.0,
    }
    assert report["one_and_many_same_sign_are_equal"] is True
    assert report["repeated_exposure_changes_utility"] is False
