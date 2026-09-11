"""
The single entry point for writing audit rows.

Call ``record()`` rather than creating AuditEvent directly: it pulls the actor
snapshot, request id, IP, and user agent consistently, so no call site can
forget one of them.

Failure policy: recording must not break the action being audited, *except* for
document access. See ``record()`` and ``record_or_raise()``.
"""

import logging

from apps.audit.models import AuditEvent
from apps.core.middleware import get_request_id

logger = logging.getLogger(__name__)


def client_ip(request) -> str | None:
    """Best-effort client IP.

    X-Forwarded-For is trusted only because Caddy sets it and nothing else can
    reach the app port. The left-most entry is the original client; we take the
    right-most that the proxy appended to avoid trusting a client-supplied
    prefix.
    """
    if request is None:
        return None
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return request.META.get("REMOTE_ADDR")


def _build(verb, *, actor=None, request=None, target=None, case_id=None, **metadata):
    if actor is None and request is not None:
        candidate = getattr(request, "user", None)
        if candidate is not None and getattr(candidate, "is_authenticated", False):
            actor = candidate

    target_type = ""
    target_id = ""
    if target is not None:
        target_type = f"{target._meta.app_label}.{target._meta.object_name}"
        target_id = str(target.pk)

    return AuditEvent(
        actor=actor,
        actor_email=getattr(actor, "email", "") or "",
        actor_role=getattr(actor, "role", "") or "",
        verb=str(verb),
        target_type=target_type,
        target_id=target_id,
        case_id_snapshot="" if case_id is None else str(case_id),
        ip=client_ip(request),
        user_agent=(request.META.get("HTTP_USER_AGENT", "") if request is not None else ""),
        request_id=get_request_id(),
        metadata=metadata,
    )


def record(verb, *, actor=None, request=None, target=None, case_id=None, **metadata):
    """Write an audit row, swallowing failures.

    Used for most events, where losing one row is worse than failing the user's
    request. The failure is logged loudly so it is still visible.
    """
    event = _build(verb, actor=actor, request=request, target=target, case_id=case_id, **metadata)
    try:
        event.save()
    except Exception:
        logger.exception("Failed to record audit event %s", verb)
        return None
    return event


def record_or_raise(verb, *, actor=None, request=None, target=None, case_id=None, **metadata):
    """Write an audit row, propagating failures.

    Used for document access. If we cannot record that someone read a
    counselee's file, we must not serve the file — an unlogged disclosure is
    exactly what the audit trail exists to prevent.
    """
    event = _build(verb, actor=actor, request=request, target=target, case_id=case_id, **metadata)
    event.save()
    return event
