import itertools

import pytest

from engram.api.routes.memory import RememberRequest
from engram.authority import (
    MemoryAuthority,
    authority_allows_supersession,
    authority_label,
    derive_memory_authority,
    qualifies_for_auto_supersession,
)


def test_stable_ordinals_and_labels() -> None:
    expected = {
        MemoryAuthority.INFERRED: (10, "inferred"),
        MemoryAuthority.UNTRUSTED_AGENT: (20, "untrusted_agent"),
        MemoryAuthority.TRUSTED_AGENT: (30, "trusted_agent"),
        MemoryAuthority.TRUSTED_IMPORT: (40, "trusted_import"),
        MemoryAuthority.EXPLICIT_USER: (50, "explicit_user"),
    }
    actual = {
        authority: (int(authority), authority_label(authority)) for authority in expected
    }
    assert actual == expected


@pytest.mark.parametrize(
    ("source_type", "principal_type", "expected"),
    [
        ("manual", "user", 50), ("manual", "admin", 50),
        ("manual", "agent", 30), ("manual", "system", 30),
        ("import", "user", 40), ("import", "admin", 40),
        ("import", "system", 40), ("import", "agent", 20),
        ("migration", "user", 40), ("migration", "admin", 40),
        ("migration", "system", 40), ("migration", "agent", 20),
        ("extraction", "user", 10), ("sync_turn", "admin", 10),
        ("pre_compress", "agent", 10), ("session_end", "system", 10),
    ],
)
def test_derivation(source_type: str, principal_type: str, expected: int) -> None:
    assert derive_memory_authority(
        source_type=source_type, principal_type=principal_type
    ) == expected


def test_exhaustive_supersession_matrix() -> None:
    values = list(MemoryAuthority)
    for new, old in itertools.product(values, repeat=2):
        assert authority_allows_supersession(
            new_authority=new, old_authority=old
        ) is (int(new) >= int(old))


def test_automatic_threshold() -> None:
    assert [qualifies_for_auto_supersession(value) for value in MemoryAuthority] == [
        False, False, False, True, True
    ]


@pytest.mark.parametrize(
    ("source_type", "principal_type"), [("unknown", "user"), ("manual", "unknown")]
)
def test_unknown_provenance_fails_closed(source_type: str, principal_type: str) -> None:
    with pytest.raises(ValueError):
        derive_memory_authority(source_type=source_type, principal_type=principal_type)


def test_remember_request_does_not_accept_authority_override() -> None:
    request = RememberRequest.model_validate(
        {"content": "safe", "source_type": "extraction", "authority": 50}
    )
    assert "authority" not in request.model_dump()
