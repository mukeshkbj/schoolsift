from __future__ import annotations


class SchoolSiftError(Exception):
    status_code = 500
    code = "INTERNAL_ERROR"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class AuthenticationError(SchoolSiftError):
    status_code = 401
    code = "UNAUTHENTICATED"


class ForbiddenError(SchoolSiftError):
    status_code = 403
    code = "FORBIDDEN"


class BadRequestError(SchoolSiftError):
    status_code = 400
    code = "BAD_REQUEST"


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


class ProviderNotConfiguredError(SchoolSiftError):
    status_code = 400
    code = "PROVIDER_NOT_CONFIGURED"


class ProviderError(SchoolSiftError):
    status_code = 502
    code = "PROVIDER_ERROR"


class VaultError(SchoolSiftError):
    status_code = 500
    code = "VAULT_ERROR"


class ProviderAuthError(ProviderError):
    status_code = 401
    code = "PROVIDER_AUTH"


class ProviderNotFoundError(ProviderError):
    status_code = 404
    code = "PROVIDER_NOT_FOUND"


class FullResyncRequiredError(ProviderError):
    code = "FULL_RESYNC_REQUIRED"


class ContentError(SchoolSiftError):
    status_code = 500
    code = "CONTENT_ERROR"


class UnsupportedDocumentError(SchoolSiftError):
    status_code = 422
    code = "UNSUPPORTED_DOCUMENT"


class AgentError(SchoolSiftError):
    status_code = 502
    code = "AGENT_ERROR"


class UnsafeAgentOutputError(AgentError):
    code = "UNSAFE_AGENT_OUTPUT"


class AgentNotConfiguredError(SchoolSiftError):
    status_code = 503
    code = "AGENT_NOT_CONFIGURED"


class ModelAccessError(SchoolSiftError):
    status_code = 503
    code = "MODEL_UNAVAILABLE"


class UnsafeProposalError(SchoolSiftError):
    status_code = 422
    code = "UNSAFE_PROPOSAL"


class WebhookNotConfiguredError(SchoolSiftError):
    status_code = 503
    code = "WEBHOOK_NOT_CONFIGURED"


class WebhookAuthError(SchoolSiftError):
    status_code = 401
    code = "WEBHOOK_UNAUTHORIZED"


class WebhookValidationError(SchoolSiftError):
    status_code = 422
    code = "WEBHOOK_VALIDATION"


class QueueError(SchoolSiftError):
    status_code = 502
    code = "QUEUE_ERROR"


class PersistenceError(SchoolSiftError):
    status_code = 500
    code = "PERSISTENCE_ERROR"


class MigrationError(RuntimeError):
    pass


class DeliveryUncertainError(ProviderError):
    status_code = 502

    def __init__(self, message: str, *, operation_id: str | None = None) -> None:
        super().__init__(message)
        self.operation_id = operation_id
