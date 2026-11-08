"""Document review state machine: draft > in_review > approved, with reject and reopen."""
from __future__ import annotations

from ..models import Document, now_iso

ALLOWED = {
    "submit": ("draft", "in_review"),
    "approve": ("in_review", "approved"),
    "reject": ("in_review", "rejected"),
    "reopen": ("rejected", "draft"),
}


class TransitionError(ValueError):
    pass


def pending_human_fields(doc: Document) -> list[str]:
    return [n for n, f in doc.fields.items() if f.source == "human" and f.pending]


def pending_other_fields(doc: Document) -> list[str]:
    return [n for n, f in doc.fields.items() if f.source != "human" and f.pending]


def transition(doc: Document, action: str, user: str, require_counsel: bool) -> tuple[Document, dict]:
    """Returns the updated document and an audit record. Raises TransitionError when not allowed."""
    if action not in ALLOWED:
        raise TransitionError(f"unknown action {action!r}")
    src, dst = ALLOWED[action]
    if doc.state != src:
        raise TransitionError(f"cannot {action} a document in state {doc.state!r}")
    if action in ("submit", "approve"):
        missing = pending_human_fields(doc)
        if missing:
            raise TransitionError(f"human input missing: {', '.join(missing)}")
    if action == "approve":
        if not user:
            raise TransitionError("approver identity is required")
        if require_counsel and not doc.counsel_reviewed:
            raise TransitionError("rules and template have not been signed off by counsel")
    new = doc.model_copy(deep=True)
    new.state = dst  # type: ignore[assignment]
    if action == "approve":
        new.approved_by, new.approved_at = user, now_iso()
    audit = {
        "id": f"{doc.id}.{action}.{now_iso()}",
        "tenantId": doc.tenant_id,
        "at": now_iso(),
        "user": user,
        "action": action,
        "documentId": doc.id,
        "fromState": src,
        "toState": dst,
        "warnings": pending_other_fields(doc) if action == "approve" else [],
    }
    return new, audit
