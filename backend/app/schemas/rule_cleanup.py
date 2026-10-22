"""Request and response shapes of the rule-cleanup API (routers/rule_cleanup.py, DESIGN-1.31 §4).

Responses mirror services/rule_cleanup_service.read_operation. Texts are not part of the API: ``message`` holds an
ErrorCode name (``errors.<CODE>`` in the UI, e.g. BEREINIGUNG_POSITION_NICHT_WIEDERHERSTELLBAR) or the firewall's
own wording; ``warnings`` is a list of ErrorCode names.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

CleanupKind = Literal["disable", "enable", "delete"]
# Longest list the API accepts at all; the effective limit is the setting cleanup_max_rules (ceiling 25).
MAX_SELECTION = 100


class PreviewItem(BaseModel):
    """One rule of the selection. ``kind`` (alias ``mode``) must be the same for all items of one preview."""
    uuid: str = Field(min_length=1, max_length=64)
    kind: CleanupKind | None = None
    mode: CleanupKind | None = None
    # The per-rule acknowledgement for a "probable" rule (the UI's checkbox).
    ack: bool = False


class PreviewRequest(BaseModel):
    """``{kind, uuids, acknowledge}`` or ``{items:[{uuid, kind|mode, ack}]}`` (or both: they are merged)."""
    kind: CleanupKind | None = None
    uuids: list[str] = Field(default_factory=list, max_length=MAX_SELECTION)
    items: list[PreviewItem] = Field(default_factory=list, max_length=MAX_SELECTION)
    acknowledge: list[str] = Field(default_factory=list, max_length=MAX_SELECTION)
    unused_days: int | None = Field(default=None, ge=1, le=3650)
    ack_text_version: str | None = Field(default=None, max_length=16)

    @model_validator(mode="after")
    def _single_kind(self):
        kinds = {k for k in [self.kind, *[i.kind or i.mode for i in self.items]] if k}
        if len(kinds) > 1:
            raise ValueError("kind must be the same for every item")
        return self

    def resolved(self) -> tuple[str | None, list[str], list[str]]:
        kind = self.kind or next((i.kind or i.mode for i in self.items if i.kind or i.mode), None)
        uuids = [*self.uuids, *[i.uuid for i in self.items]]
        acknowledge = [*self.acknowledge, *[i.uuid for i in self.items if i.ack]]
        return kind, list(dict.fromkeys(u.strip() for u in uuids)), list(dict.fromkeys(a.strip() for a in acknowledge))


class ConfirmName(BaseModel):
    """Deploy and restore: the exact host name, typed by a human."""
    confirm_name: str = Field(max_length=255)


class CleanupItemRead(BaseModel):
    id: str
    host_id: str
    host_name: str | None = None
    rule_uuid: str
    seq: int
    carp_role: str | None = None
    # queued, disabling, disabled, confirmed, reverting, reverted, deleting, deleted, restoring, restored, failed,
    # unknown, revert_failed, skipped (never attempted: the run stopped earlier)
    state: str
    description: str | None = None
    categories: Any = None
    confidence: str | None = None
    cover_uuid: str | None = None
    prio_group: str | None = None
    anchor_next_uuid: str | None = None
    anchor_prev_uuid: str | None = None
    source_item_id: str | None = None
    restored_uuid: str | None = None
    changed_at: str | None = None
    message: str | None = None
    has_snapshot: bool = False
    # A deleted rule with a stored snapshot that has not been restored yet (POST items/{id}/restore, admin).
    restorable: bool = False


class CleanupOperationRead(BaseModel):
    id: str
    host_id: str
    host_name: str | None = None
    cluster_name: str | None = None
    kind: str  # disable | enable | delete | restore
    # preview, running, awaiting_confirm, confirmed, partial, apply_failed, reverting, reverted, revert_failed, done,
    # failed, cancelled, expired
    status: str
    actor: str
    created_at: str | None = None
    expires_at: str | None = None
    started_at: str | None = None
    heartbeat_at: str | None = None
    revert_deadline: str | None = None
    confirmed_at: str | None = None
    finished_at: str | None = None
    message: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    # Only while status == "preview".
    plan: dict[str, Any] | None = None
    items: list[CleanupItemRead] = Field(default_factory=list)
    can_confirm: bool = False
    can_revert: bool = False
    can_retry: bool = False
    # ErrorCode names, e.g. BEREINIGUNG_POSITION_NICHT_WIEDERHERSTELLBAR (restore only).
    warnings: list[str] = Field(default_factory=list)


class CleanupPreviewRead(BaseModel):
    preview_id: str
    expires_at: str | None = None
    kind: str
    plan: dict[str, Any]
    max_rules: int
    window_seconds: int
    operation: CleanupOperationRead
