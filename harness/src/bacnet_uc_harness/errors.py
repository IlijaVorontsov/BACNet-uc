# SPDX-License-Identifier: Apache-2.0
"""Exception hierarchy of the harness.

All errors raised by the harness derive from :class:`HarnessError`, so a
caller (for example an MCP tool handler) can catch one type and report the
message. Timeouts additionally derive from the built-in :class:`TimeoutError`
(which is also ``asyncio.TimeoutError``).
"""

from __future__ import annotations

from typing import Any, Literal

BacnetErrorKind = Literal["error", "reject", "abort"]


class HarnessError(Exception):
    """Base class of all harness errors."""


class SmpError(HarnessError):
    """An SMP request failed on the node.

    Attributes:
        group: SMP group of an SMP v2 ``{"err": {"group": g, "rc": n}}``
            response, or ``None`` for a legacy ``{"rc": n}`` (``mcumgr_err_t``)
            response.
        rc: the group rc (v2) or the legacy MCUmgr error code.
        rc_name: symbolic name of ``rc`` (for example ``"NOT_FOUND"`` for the
            BACnet-uc groups, ``"FILE_NOT_FOUND"`` for the FS group, ``"ENOTSUP"``
            for legacy codes), ``"rc<n>"`` if unknown.
        response: the full decoded response map (may carry more fields, e.g.
            ``reboot_required`` of ``uc_node reload`` or ``len`` of an FS
            offset error).
    """

    def __init__(
        self,
        group: int | None,
        rc: int,
        rc_name: str | None = None,
        message: str | None = None,
        response: dict[str, Any] | None = None,
    ) -> None:
        if rc_name is None:
            from bacnet_uc_harness.smp.groups import rc_name as _rc_name

            rc_name = _rc_name(group, rc)
        self.group = group
        self.rc = rc
        self.rc_name = rc_name
        self.response = response if response is not None else {}
        if message is None:
            from bacnet_uc_harness.smp.groups import group_name

            if group is None:
                message = f"SMP error rc={rc} ({rc_name})"
            else:
                message = f"SMP group {group} ({group_name(group)}) rc={rc} ({rc_name})"
        super().__init__(message)


class BacnetError(HarnessError):
    """A BACnet confirmed request was answered with Error, Reject or Abort.

    Attributes:
        kind: ``"error"``, ``"reject"`` or ``"abort"``.
        error_class: BACnet error class (kind ``"error"``), else ``None``.
        error_code: BACnet error code (kind ``"error"``), else ``None``.
        reason: reject or abort reason (kinds ``"reject"``/``"abort"``), else
            ``None``.
    """

    def __init__(
        self,
        kind: BacnetErrorKind,
        error_class: int | None = None,
        error_code: int | None = None,
        reason: int | None = None,
        message: str | None = None,
    ) -> None:
        self.kind = kind
        self.error_class = error_class
        self.error_code = error_code
        self.reason = reason
        if message is None:
            from bacnet_uc_harness.bacnet import enums

            if kind == "error":
                message = (
                    "BACnet error: "
                    f"{enums.error_class_name(error_class if error_class is not None else -1)}"
                    f"/{enums.error_code_name(error_code if error_code is not None else -1)}"
                )
            elif kind == "reject":
                message = f"BACnet reject: {enums.reject_reason_name(reason or 0)}"
            else:
                message = f"BACnet abort: {enums.abort_reason_name(reason or 0)}"
        super().__init__(message)

    @property
    def error_class_name(self) -> str | None:
        from bacnet_uc_harness.bacnet import enums

        return None if self.error_class is None else enums.error_class_name(self.error_class)

    @property
    def error_code_name(self) -> str | None:
        from bacnet_uc_harness.bacnet import enums

        return None if self.error_code is None else enums.error_code_name(self.error_code)


class HarnessTimeout(HarnessError, TimeoutError):
    """No (valid) response within the timeout, after all retries."""
