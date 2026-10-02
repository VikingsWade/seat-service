from __future__ import annotations


class ApiError(Exception):
    """An expected, client-facing failure rendered as a JSON error body."""

    def __init__(self, status: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra


class Decline(ApiError):
    """A domain-level refusal. `reason` is the label used on the decline counter."""

    def __init__(self, status: int, code: str, message: str, reason: str | None = None, **extra):
        super().__init__(status, code, message, **extra)
        self.reason = reason or code


class DatabaseUnavailable(Exception):
    """The datastore cannot be reached right now."""


class RestartTransaction(Exception):
    """Raised inside a transaction to roll back and run it again from the top."""
