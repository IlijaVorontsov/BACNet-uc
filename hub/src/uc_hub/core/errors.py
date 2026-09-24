"""Exception hierarchy. Tool handlers turn these into tool results the model
can read; the HTTP API maps them to status codes (see ``api``)."""

from __future__ import annotations


class HubError(Exception):
    """Base class. ``code`` is a short stable identifier for clients."""

    code = "error"


class NotFound(HubError):
    code = "not_found"


class InvalidRequest(HubError):
    code = "invalid"


class Unsupported(HubError):
    """The device or driver does not implement this operation."""

    code = "unsupported"


class DeviceError(HubError):
    """The device answered with an error (SMP rc, BACnet Error PDU, ...)."""

    code = "device_error"

    def __init__(self, message: str, *, rc: int | None = None, group: int | None = None):
        super().__init__(message)
        self.rc = rc
        self.group = group


class DeviceTimeout(HubError):
    code = "timeout"


class PolicyDenied(HubError):
    """The policy engine refused the operation. Never retried automatically."""

    code = "denied"


class ValidationFailed(HubError):
    """A manifest or document does not match its schema."""

    code = "validation"

    def __init__(self, message: str, errors: list[str] | None = None):
        super().__init__(message)
        self.errors = errors or []
