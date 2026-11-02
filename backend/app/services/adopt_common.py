"""Small pieces shared by every "take over what was made by hand on a firewall" feature
(rule_adopt_service.py for filter rules, object_adopt_service.py for aliases and NAT)."""


class NotAdoptable(Exception):
    """The object exists on the host but cannot be described without distortion.

    `reasons` are machine-readable codes (the UI translates them); `details` maps a code to
    extra text the translation can show (e.g. the alias names a reference points to)."""

    def __init__(self, reasons: list[str], details: dict[str, str] | None = None):
        super().__init__(", ".join(reasons))
        self.reasons = reasons
        self.details = details or {}


class NotFoundOnHost(Exception):
    """The UUID is not (or no longer) present in the host's live configuration."""


def interface_role_for(host, role: str, live_interface: str) -> str:
    """"Adopt as an interface role": the placeholder {{IF_NAME}} to store as the central object's interface. The host must
    have assigned that role to exactly the interface the live object sits on, otherwise nothing is guessed."""
    assigned = next((v.value for v in host.variables if v.name == role), None)
    if assigned is None:
        raise NotAdoptable(["role_not_assigned"], {"role_not_assigned": role})
    if assigned != live_interface:
        raise NotAdoptable(["role_interface_differs"], {"role_interface_differs": f"{role} = {assigned}, live: {live_interface}"})
    return "{{" + role + "}}"


def roles_matching(host, live_interface: str) -> list[str]:
    """The interface roles this host has assigned to `live_interface` (offered when adopting)."""
    return sorted(v.name for v in host.variables if v.name.startswith("IF_") and v.value == live_interface)


def on(value) -> bool:
    """OPNsense booleans arrive as "1"/"0" strings, sometimes as real booleans."""
    return value in ("1", 1, True)


def clean(value) -> str:
    return "" if value is None else str(value).strip()


class LinkTargetNotFound(Exception):
    """The central object chosen to link to was deleted in the meantime."""


class LinkTargetChanged(Exception):
    """The central object no longer equals what is live on the host. `diff` lists the differing fields."""

    def __init__(self, diff: list[dict]):
        super().__init__(", ".join(d["field"] for d in diff))
        self.diff = diff


class HostAlreadyHasObject(Exception):
    """The central object is already deployed to / managed on this host under another UUID."""


class LinkTargetBusy(Exception):
    """The central object has an unconfirmed deployment or a failed rollback; linking waits until it is settled."""


LINK_ERRORS = (LinkTargetNotFound, LinkTargetChanged, HostAlreadyHasObject, LinkTargetBusy)


def link_api_error(exc: Exception):
    """The HTTP error for a failed link (shared by the rule and the object endpoints)."""
    from app.errors import ErrorCode, api_error

    if isinstance(exc, LinkTargetNotFound):
        return api_error(404, ErrorCode.ADOPT_LINK_TARGET_NOT_FOUND)
    if isinstance(exc, LinkTargetChanged):
        return api_error(409, ErrorCode.ADOPT_LINK_TARGET_CHANGED, fields=", ".join(d["field"] for d in exc.diff))
    if isinstance(exc, HostAlreadyHasObject):
        return api_error(409, ErrorCode.ADOPT_LINK_HOST_HAS_OBJECT)
    return api_error(409, ErrorCode.ADOPT_LINK_BUSY)
