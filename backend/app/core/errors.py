"""Typed application errors surfaced as API problem payloads.

API routes translate these into HTTP status codes with ``{code, message}``
detail bodies; tracebacks are never leaked to clients.
"""

from __future__ import annotations


class AppError(Exception):
    """Base class for expected, user-visible failures."""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(
        self, message: str, *, status_code: int | None = None, code: str | None = None
    ) -> None:
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        if code is not None:
            self.code = code


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"
