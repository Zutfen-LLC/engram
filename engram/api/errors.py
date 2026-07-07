"""Centralized mapping from database constraint failures to stable HTTP errors.

Routes generally let ``IntegrityError``/``DataError`` propagate rather than
catching them individually. These two exception handlers classify the
underlying Postgres SQLSTATE and translate *client-caused* constraint
failures (CHECK, FK, NOT NULL, unique, malformed literals) into a documented
4xx JSON body — see docs/api-errors.md.

Anything we don't recognize (or that isn't a client-caused violation, e.g. a
connection failure) is deliberately re-raised so it falls through to
FastAPI's default handling and surfaces as a 500. We never guess a 4xx for an
unrecognized SQLSTATE — that would risk mislabeling a genuine server fault.

Route-level handling still comes first for cases that need business logic
(e.g. ``remember``'s dedup detection on the unique index) — those routes
catch ``IntegrityError`` themselves and only re-raise when the failure isn't
the case they're handling, at which point it reaches these handlers.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DataError, IntegrityError

# Postgres SQLSTATE codes, Class 23 (Integrity Constraint Violation).
_CHECK_VIOLATION = "23514"
_NOT_NULL_VIOLATION = "23502"
_FOREIGN_KEY_VIOLATION = "23503"
_UNIQUE_VIOLATION = "23505"
_EXCLUSION_VIOLATION = "23P01"

_INTEGRITY_STATUS_BY_SQLSTATE: dict[str, int] = {
    _CHECK_VIOLATION: 422,
    _NOT_NULL_VIOLATION: 422,
    _FOREIGN_KEY_VIOLATION: 422,
    _UNIQUE_VIOLATION: 409,
    _EXCLUSION_VIOLATION: 409,
}

_INTEGRITY_CODE_BY_SQLSTATE: dict[str, str] = {
    _CHECK_VIOLATION: "check_violation",
    _NOT_NULL_VIOLATION: "not_null_violation",
    _FOREIGN_KEY_VIOLATION: "foreign_key_violation",
    _UNIQUE_VIOLATION: "unique_violation",
    _EXCLUSION_VIOLATION: "exclusion_violation",
}

# Postgres SQLSTATE codes, Class 22 (Data Exception) that indicate a
# malformed client-supplied value (bad enum/UUID literal, out-of-range
# number, bad date) rather than a driver or server bug.
_DATA_ERROR_SQLSTATES: frozenset[str] = frozenset(
    {
        "22P02",  # invalid_text_representation
        "22003",  # numeric_value_out_of_range
        "22007",  # invalid_datetime_format
        "22008",  # datetime_field_overflow
    }
)


# SQLite (used only by the fast, non-Postgres unit test suite) has no
# SQLSTATE machinery — its DBAPI exceptions carry only a message string. We
# fall back to matching its fixed, well-known constraint-failure phrasing so
# those tests keep exercising this same code path. Postgres/asyncpg (the only
# driver used in production and in the Compose integration tests) always
# takes the precise SQLSTATE path above.
_SQLITE_MESSAGE_CLASSIFICATION: tuple[tuple[str, str], ...] = (
    ("UNIQUE constraint failed", _UNIQUE_VIOLATION),
    ("CHECK constraint failed", _CHECK_VIOLATION),
    ("FOREIGN KEY constraint failed", _FOREIGN_KEY_VIOLATION),
    ("NOT NULL constraint failed", _NOT_NULL_VIOLATION),
)


def _dbapi_exc(exc: Exception) -> Any:
    """Return the deepest driver-native exception SQLAlchemy wrapped.

    SQLAlchemy's asyncpg dialect re-wraps the raw ``asyncpg`` exception into
    its own ``AsyncAdapt_asyncpg_dbapi`` error class on ``.orig`` — that
    wrapper forwards ``sqlstate`` but drops ``constraint_name``/``table_name``.
    The original asyncpg exception (which has both) is preserved as
    ``orig.__cause__`` (SQLAlchemy raises the wrapper ``from`` it).
    """
    orig = getattr(exc, "orig", None)
    if orig is None:
        return exc
    cause = getattr(orig, "__cause__", None)
    return cause if cause is not None else orig


def _sqlstate(exc: Exception) -> str | None:
    native = _dbapi_exc(exc)
    sqlstate = getattr(native, "sqlstate", None)
    if sqlstate is not None:
        return str(sqlstate)
    message = str(native)
    for phrase, code in _SQLITE_MESSAGE_CLASSIFICATION:
        if phrase in message:
            return code
    return None


def _constraint_name(exc: Exception) -> str | None:
    name = getattr(_dbapi_exc(exc), "constraint_name", None)
    return str(name) if name is not None else None


def _error_response(*, status_code: int, code: str, constraint: str | None) -> JSONResponse:
    message = f"request rejected by database constraint ({code})"
    if constraint:
        message += f": {constraint}"
    detail: dict[str, Any] = {"message": message, "code": code}
    if constraint:
        detail["constraint"] = constraint
    return JSONResponse(status_code=status_code, content={"detail": detail})


async def integrity_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, IntegrityError)
    sqlstate = _sqlstate(exc)
    if sqlstate not in _INTEGRITY_STATUS_BY_SQLSTATE:
        # Unrecognized integrity failure — don't guess a 4xx; let it surface
        # as the 500 it actually is.
        raise exc
    return _error_response(
        status_code=_INTEGRITY_STATUS_BY_SQLSTATE[sqlstate],
        code=_INTEGRITY_CODE_BY_SQLSTATE[sqlstate],
        constraint=_constraint_name(exc),
    )


async def data_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, DataError)
    sqlstate = _sqlstate(exc)
    if sqlstate not in _DATA_ERROR_SQLSTATES:
        raise exc
    return _error_response(status_code=422, code="invalid_value", constraint=None)
