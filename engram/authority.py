"""Stable memory-governance authority derived from immutable provenance."""

from enum import IntEnum


class MemoryAuthority(IntEnum):
    INFERRED = 10
    UNTRUSTED_AGENT = 20
    TRUSTED_AGENT = 30
    TRUSTED_IMPORT = 40
    EXPLICIT_USER = 50


_LABELS = {
    MemoryAuthority.INFERRED: "inferred",
    MemoryAuthority.UNTRUSTED_AGENT: "untrusted_agent",
    MemoryAuthority.TRUSTED_AGENT: "trusted_agent",
    MemoryAuthority.TRUSTED_IMPORT: "trusted_import",
    MemoryAuthority.EXPLICIT_USER: "explicit_user",
}


def derive_memory_authority(*, source_type: str, principal_type: str) -> MemoryAuthority:
    """Derive fixed authority from stored provenance, failing closed on new vocabulary."""
    if principal_type not in {"user", "admin", "agent", "system"}:
        raise ValueError(f"unknown principal type: {principal_type}")
    if source_type in {"extraction", "sync_turn", "pre_compress", "session_end"}:
        return MemoryAuthority.INFERRED
    if source_type == "manual":
        if principal_type in {"user", "admin"}:
            return MemoryAuthority.EXPLICIT_USER
        return MemoryAuthority.TRUSTED_AGENT
    if source_type in {"import", "migration"}:
        if principal_type == "agent":
            return MemoryAuthority.UNTRUSTED_AGENT
        return MemoryAuthority.TRUSTED_IMPORT
    raise ValueError(f"unknown source type: {source_type}")


def authority_label(authority: MemoryAuthority | int) -> str:
    return _LABELS[MemoryAuthority(authority)]


def authority_allows_supersession(
    *, new_authority: MemoryAuthority | int, old_authority: MemoryAuthority | int
) -> bool:
    return int(new_authority) >= int(old_authority)


def qualifies_for_auto_supersession(authority: MemoryAuthority | int) -> bool:
    return int(authority) >= int(MemoryAuthority.TRUSTED_IMPORT)
