"""#162C raw reviewer-provenance normalization contracts."""

from __future__ import annotations

from pathlib import Path

import pytest


def _case(number: int, review_id: str, judgment: str) -> str:
    return f"### Case {number} — `{review_id}`\n\n`{judgment}`\n\nNotes: original wording.\n"


def test_full_reviewer_parser_preserves_notes_and_maps_all_cases() -> None:
    from evals.admission.holdout.provenance import normalize_raw_review

    raw = (
        "REVIEWER C — INDEPENDENT FULL-HOLDOUT REVIEW — BLIND TO A/B AND "
        "CURRENT/CANDIDATE POLICY OUTPUTS\n\n"
        + _case(
            1,
            "rvw_aaaaaaaaaaaaaaaaaaaaaaaa",
            "retain / unverifiable / high / disposition=defer / startup=no / "
            "governed=yes / review=yes / kind=doctrine",
        )
        + _case(
            2,
            "rvw_bbbbbbbbbbbbbbbbbbbbbbbb",
            "do_not_retain / unknown / low / disposition=reject / startup=no / "
            "governed=no / review=no / kind=unknown",
        )
    ).encode()
    packet = {
        "cases": [
            {"review_case_id": "rvw_aaaaaaaaaaaaaaaaaaaaaaaa"},
            {"review_case_id": "rvw_bbbbbbbbbbbbbbbbbbbbbbbb"},
        ]
    }

    normalized = normalize_raw_review(
        reviewer="c", raw=raw, packet=packet, expected_case_numbers=(1, 2)
    )

    assert normalized["reviewer"] == "c"
    assert normalized["source_sha256"]
    assert [r["case"] for r in normalized["records"]] == [1, 2]
    assert normalized["records"][0]["normalized"]["dimensions"]["epistemic_state"] == "unverifiable"
    assert normalized["records"][0]["original_notes"] == "Notes: original wording."


def test_subset_reviewer_rejects_wrong_case_membership() -> None:
    from evals.admission.holdout.provenance import normalize_raw_review

    raw = _case(
        1,
        "rvw_aaaaaaaaaaaaaaaaaaaaaaaa",
        "retain / unverifiable / high / disposition=defer / startup=no / "
        "governed=yes / review=yes / kind=doctrine",
    ).encode()
    packet = {"cases": [{"review_case_id": "rvw_aaaaaaaaaaaaaaaaaaaaaaaa"}]}

    with pytest.raises(ValueError, match="reviewer_case_membership_mismatch"):
        normalize_raw_review(reviewer="b", raw=raw, packet=packet, expected_case_numbers=(2,))


def test_parser_rejects_policy_output_and_unrepresentable_judgment() -> None:
    from evals.admission.holdout.provenance import normalize_raw_review

    packet = {"cases": [{"review_case_id": "rvw_aaaaaaaaaaaaaaaaaaaaaaaa"}]}
    policy_raw = (
        _case(
            1,
            "rvw_aaaaaaaaaaaaaaaaaaaaaaaa",
            "retain / unknown / high / disposition=defer / startup=no / "
            "governed=yes / review=yes / kind=doctrine",
        )
        + "P0 predicted automatic admission\n"
    ).encode()
    with pytest.raises(ValueError, match="reviewer_source_policy_contamination"):
        normalize_raw_review(
            reviewer="c", raw=policy_raw, packet=packet, expected_case_numbers=(1,)
        )

    invalid_raw = _case(
        1,
        "rvw_aaaaaaaaaaaaaaaaaaaaaaaa",
        "retain / speculative / high / disposition=defer / startup=no / "
        "governed=yes / review=yes / kind=doctrine",
    ).encode()
    with pytest.raises(ValueError, match="reviewer_judgment_unrepresentable"):
        normalize_raw_review(
            reviewer="c", raw=invalid_raw, packet=packet, expected_case_numbers=(1,)
        )


def test_private_freeze_requires_private_destination(tmp_path: Path) -> None:
    from evals.admission.holdout.provenance import write_private_provenance

    with pytest.raises(ValueError, match="private_output_must_be_outside_repository"):
        write_private_provenance({"artifact": "x"}, Path(__file__).parents[1] / "bad.json")

    destination = tmp_path / "private.json"
    assert write_private_provenance({"artifact": "x"}, destination) == destination.name
    assert destination.stat().st_mode & 0o777 == 0o600
