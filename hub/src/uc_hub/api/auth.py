"""Who is calling: bearer tokens from ``hub.yaml`` (``auth.tokens``), or dev
mode (no tokens: every request is user ``dev`` with all roles, and the hub
only listens on a loopback address).

Event streams may pass the token as ``?access_token=`` because
``EventSource`` cannot set headers; other requests must use the header, so
tokens do not end up in URLs without need.
"""

from __future__ import annotations

import hmac
from collections.abc import Iterable

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


def ranked(roles: Iterable[str]) -> list[str]:
    """Roles in increasing order of authority."""
    return sorted(set(roles) & set(ROLES), key=ROLES.index)


def require(caller: Identity, least: str) -> None:
    """``PolicyDenied`` unless the caller has role ``least`` or a stronger one."""
    if role_rank(caller.roles) < role_rank([least]):
        raise PolicyDenied(f"this needs the role {least} or above")
