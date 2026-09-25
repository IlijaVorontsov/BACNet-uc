"""Who is calling: bearer tokens from ``hub.yaml`` (``auth.tokens``), or dev
mode (no tokens: every request is user ``dev`` with all roles, the hub only
listens on a loopback address, and requests from pages of other sites are
refused).

Event streams may pass the token as ``?access_token=`` because
``EventSource`` cannot set headers; other requests must use the header, so
tokens do not end up in URLs without need.
"""

from __future__ import annotations

import hmac
from urllib.parse import urlsplit

from fastapi import Request

from ..core.errors import HubError, PolicyDenied
from ..policy.policy import ROLES, role_rank
from ..runtime.config import HubConfig, Identity

DEV = Identity("dev", frozenset(ROLES))


class Unauthorized(HubError):
    code = "unauthorized"


class Auth:
    def __init__(self, config: HubConfig) -> None:
        self.dev_mode = config.auth.dev_mode
        #: Raises ValidationFailed for a token whose environment variable is missing.
        self._tokens = list(config.auth.identities().items())

    def caller(self, request: Request) -> Identity:
        """FastAPI dependency: the identity of a request, from its header."""
        return self._identify(request, query=False)

    def stream_caller(self, request: Request) -> Identity:
        """The same for event streams, which may also use ``?access_token=``."""
        return self._identify(request, query=True)

    def _identify(self, request: Request, *, query: bool) -> Identity:
        if self.dev_mode:
            _same_origin(request)
            return DEV
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if not (header and scheme.lower() == "bearer") and query:
            token = request.query_params.get("access_token", "")
        token = token.strip()
        found = None
        # Every configured token is compared, in constant time, so the timing tells nothing.
        for known, identity in self._tokens:
            if hmac.compare_digest(known.encode(), token.encode()) and token:
                found = identity
        if found is None:
            raise Unauthorized("a valid bearer token is required (Authorization: Bearer <token>)")
        return found


def _same_origin(request: Request) -> None:
    """Dev mode has no credentials to forge, and a loopback address does not
    stop a page from another site that is open in the same browser: its
    forms and no-cors requests reach 127.0.0.1 too. Browsers name that
    page's origin in ``Origin``, so a request whose origin is not the hub's
    own is refused."""
    origin = request.headers.get("origin")
    if origin is not None and urlsplit(origin).netloc.lower() != request.headers.get("host", "").lower():
        raise PolicyDenied(f"dev mode refuses requests from another site (Origin {origin[:100]})")


def require(caller: Identity, least: str) -> None:
    """``PolicyDenied`` unless the caller has role ``least`` or a stronger one."""
    if role_rank(caller.roles) < role_rank([least]):
        raise PolicyDenied(f"this needs the role {least} or above")
