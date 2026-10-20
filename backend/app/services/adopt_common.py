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


def on(value) -> bool:
    """OPNsense booleans arrive as "1"/"0" strings, sometimes as real booleans."""
    return value in ("1", 1, True)


def clean(value) -> str:
    return "" if value is None else str(value).strip()
