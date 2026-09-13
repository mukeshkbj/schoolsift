from __future__ import annotations


class SchoolSiftError(Exception):
    status_code = 500
    code = "INTERNAL_ERROR"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(SchoolSiftError):
    status_code = 404
    code = "NOT_FOUND"


class ConflictError(SchoolSiftError):
    status_code = 409
    code = "VERSION_CONFLICT"


class HashMismatchError(ConflictError):
    code = "HASH_MISMATCH"


class NotApprovableError(ConflictError):
    code = "ESCALATION_NOT_APPROVABLE"
