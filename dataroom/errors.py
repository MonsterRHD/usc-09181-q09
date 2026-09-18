"""Domain error types.

Every error carries a stable ``code`` so API clients (and tests) can
branch on semantics rather than on translated message text.
"""


class DataRoomError(Exception):
    code = "error"
    http_status = 400

    def __init__(self, message: str, code: str | None = None, status: int | None = None):
        super().__init__(message)
        if code:
            self.code = code
        if status:
            self.http_status = status


class NotFound(DataRoomError):
    code = "not_found"
    http_status = 404


class Conflict(DataRoomError):
    code = "conflict"
    http_status = 409


class ValidationFailed(DataRoomError):
    code = "validation_failed"
    http_status = 422


class AuthorizationError(DataRoomError):
    code = "forbidden"
    http_status = 403


class LinkExpired(DataRoomError):
    """Raised for share-link access past expiry; the response must not
    reveal whether the linked document exists."""

    code = "link_unavailable"
    http_status = 410


class LinkRevoked(DataRoomError):
    code = "link_unavailable"
    http_status = 410
