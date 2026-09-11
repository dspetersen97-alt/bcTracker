"""
The audit trail.

This is the accountability record, distinct from application logging: it answers
"who looked at this counselee's file, and when". Every document view and
download writes a row here.

Two design choices worth knowing:

  * The target is stored as ``(target_type, target_id)`` strings rather than a
    GenericForeignKey, so an audit row survives deletion of the thing it
    describes. An audit trail that vanishes with its subject is not a trail.
  * The table is append-only, enforced by a database trigger (see migration
    0002) so it holds against raw SQL and queryset ``.update()``, not only
    against mistakes made through the model.
  * The actor's email and role are snapshotted alongside the foreign key. The
    key is PROTECT so accounts with history cannot be removed through the ORM;
    the snapshots mean the row still says who acted even if a row is removed
    out of band, directly in the database.
"""

from django.conf import settings
from django.db import models


class AuditVerb(models.TextChoices):
    """Known audit verbs.

    Kept as choices for discoverability, but the column is a plain CharField
    with no check constraint: a new verb must never be the reason an audited
    action fails to record.
    """

    LOGIN_SUCCEEDED = "auth.login.succeeded", "Login succeeded"
    LOGIN_FAILED = "auth.login.failed", "Login failed"
    LOGOUT = "auth.logout", "Logout"
    MFA_ENROLLED = "auth.mfa.enrolled", "MFA enrolled"
    MFA_FAILED = "auth.mfa.failed", "MFA verification failed"
    MFA_VERIFIED = "auth.mfa.verified", "MFA verified"
    MAGIC_LINK_SENT = "auth.magic_link.sent", "Magic link sent"
    LOGIN_LINK_THROTTLED = "auth.login_link.throttled", "Login link throttled"
    PASSWORD_CHANGED = "auth.password.changed", "Password changed"
    # Recorded once, when the lockout begins. A run of these for one address is
    # the clearest signal this system produces that somebody is guessing.
    LOGIN_LOCKED_OUT = "auth.login.locked_out", "Login locked out"

    USER_CREATED = "user.created", "User created"
    USER_DEACTIVATED = "user.deactivated", "User deactivated"
    ROLE_CHANGED = "user.role_changed", "Role changed"
    INVITATION_SENT = "user.invitation_sent", "Invitation sent"

    CASE_CREATED = "case.created", "Case created"
    CASE_UPDATED = "case.updated", "Case updated"
    CASE_CLOSED = "case.closed", "Case closed"
    CASE_MEMBER_ADDED = "case.member_added", "Case member added"
    CASE_MEMBER_ENDED = "case.member_ended", "Case membership ended"
    CASE_REASSIGNED = "case.reassigned", "Case reassigned"
    # Viewing a case record is recorded as well as viewing a document. Who looked
    # at whose file is the question an access review actually asks, and a trail
    # that only covers downloads cannot answer it.
    CASE_VIEWED = "case.viewed", "Case viewed"
    PROFILE_UPDATED = "profile.updated", "Profile updated"

    DOCUMENT_UPLOADED = "document.uploaded", "Document uploaded"
    DOCUMENT_VIEWED = "document.viewed", "Document metadata viewed"
    DOCUMENT_DOWNLOADED = "document.downloaded", "Document downloaded"
    DOCUMENT_UPDATED = "document.updated", "Document details updated"
    DOCUMENT_DELETED = "document.deleted", "Document deleted"
    DOCUMENT_SCAN_REJECTED = "document.scan_rejected", "Document rejected by scanner"
    # Sharing changes who can read a file, so it is the document equivalent of
    # adding someone to a case: recorded on its own rather than folded into a
    # generic "updated".
    DOCUMENT_SHARED = "document.shared", "Document shared with the case"
    DOCUMENT_UNSHARED = "document.unshared", "Document returned to private"

    # Messaging. A message body is never recorded here — the metadata carries a
    # character count and nothing else. The trail's job is who corresponded with
    # whom and when; putting the words in a second table would create a copy of
    # the conversation outside the access rules that govern the first.
    THREAD_STARTED = "thread.started", "Conversation started"
    THREAD_VIEWED = "thread.viewed", "Conversation read"
    THREAD_CLOSED = "thread.closed", "Conversation closed"
    THREAD_REOPENED = "thread.reopened", "Conversation reopened"
    MESSAGE_SENT = "message.sent", "Message sent"
    MESSAGE_ATTACHMENT_ADDED = "message.attachment.added", "File sent with a message"
    # Recorded with record_or_raise, as a document download is: an unlogged
    # disclosure is the thing this trail exists to prevent.
    MESSAGE_ATTACHMENT_DOWNLOADED = "message.attachment.downloaded", "Message attachment opened"
    MESSAGE_ATTACHMENT_REJECTED = "message.attachment.rejected", "Message attachment refused"

    BOOKING_CREATED = "booking.created", "Booking created"
    BOOKING_CONFIRMED = "booking.confirmed", "Booking confirmed"
    BOOKING_CANCELLED = "booking.cancelled", "Booking cancelled"
    BOOKING_RESCHEDULED = "booking.rescheduled", "Booking rescheduled"
    BOOKING_COMPLETED = "booking.completed", "Session recorded as held"
    BOOKING_NO_SHOW = "booking.no_show", "Session recorded as missed"
    BOOKING_NOTE_UPDATED = "booking.note_updated", "Counselor note updated"
    BOOKING_REMINDER_SENT = "booking.reminder_sent", "Appointment reminder sent"

    AVAILABILITY_ADDED = "availability.added", "Office hours added"
    AVAILABILITY_UPDATED = "availability.updated", "Office hours updated"
    AVAILABILITY_REMOVED = "availability.removed", "Office hours removed"
    AVAILABILITY_OVERRIDDEN = "availability.overridden", "Calendar exception added"
    AVAILABILITY_OVERRIDE_REMOVED = "availability.override_removed", "Calendar exception removed"

    # Connecting a calendar sends appointment times to a system outside this one,
    # and disconnecting stops it. Both are recorded because "when did this
    # counselor's appointments start reaching Google, and whose Google account was
    # it" is a question an access review can be expected to ask, and the answer
    # cannot be reconstructed from the credential row once it is revoked.
    GOOGLE_CONNECTED = "google.connected", "Google calendar connected"
    GOOGLE_DISCONNECTED = "google.disconnected", "Google calendar disconnected"
    GOOGLE_CONNECT_REFUSED = "google.connect_refused", "Google connection refused"
    GOOGLE_SETTINGS_CHANGED = "google.settings_changed", "Google calendar settings changed"

    # Billing. Amounts are recorded here on purpose, unlike a message body or a
    # session note: what somebody was charged and what they paid is exactly the
    # kind of fact an accountability record is for, and none of it is counseling
    # content. What is never recorded is a *reason* that came from the counseling —
    # a void reason and a waiver reason are staff-written billing text, which is why
    # they are safe to include.
    FEE_ADDED = "fee.added", "Fee added to the schedule"
    FEE_ENDED = "fee.ended", "Fee withdrawn from the schedule"
    SESSION_RECORDED = "session.recorded", "Billable session recorded"
    SESSION_AMENDED = "session.amended", "Billable session corrected"
    INVOICE_CREATED = "invoice.created", "Invoice drafted"
    INVOICE_ISSUED = "invoice.issued", "Invoice issued"
    INVOICE_VIEWED = "invoice.viewed", "Invoice viewed"
    INVOICE_VOIDED = "invoice.voided", "Invoice voided"
    INVOICE_WRITTEN_OFF = "invoice.written_off", "Invoice written off"
    INVOICE_REMINDER_SENT = "invoice.reminder_sent", "Invoice reminder sent"
    PAYMENT_RECORDED = "payment.recorded", "Payment recorded"
    PAYMENT_REVERSED = "payment.reversed", "Payment reversed"

    # Stripe. Recorded with record_or_raise where a counselee's email address is
    # about to leave the building — the same rule as a document download, for the
    # same reason: an unlogged disclosure is what this trail exists to prevent.
    STRIPE_CUSTOMER_CREATED = "stripe.customer_created", "Counselee registered with Stripe"
    STRIPE_CHECKOUT_STARTED = "stripe.checkout_started", "Card payment started"
    STRIPE_WEBHOOK_RECEIVED = "stripe.webhook_received", "Stripe webhook received"
    STRIPE_WEBHOOK_REFUSED = "stripe.webhook_refused", "Stripe webhook refused"
    STRIPE_RECONCILED = "stripe.reconciled", "Stripe reconciliation run"

    ACCESS_DENIED = "access.denied", "Access denied"

    # Scheduled maintenance. Recorded because both of these move counselee data:
    # a purge destroys some of it, and a backup produces a second copy of all of
    # it. Written by the cron sidecar, so the actor is null.
    BACKUP_CREATED = "backup.created", "Database backup created"
    BACKUP_FAILED = "backup.failed", "Database backup failed"
    # A person turning a backup back into a readable database dump is the single
    # broadest read of counselee data this system permits, and it happens at a
    # shell rather than through a view. It is recorded for the same reason a
    # document download is.
    BACKUP_DECRYPTED = "backup.decrypted", "Database backup decrypted"
    RETENTION_PURGED = "retention.purged", "Expired records purged"


class AuditEvent(models.Model):
    # PROTECT, so a user who has done anything auditable cannot be deleted.
    #
    # SET_NULL would be the obvious choice, but it is incompatible with the
    # append-only trigger: nulling the column is an UPDATE, so deleting a user
    # would fail with a confusing database error from deep inside a cascade.
    # PROTECT states the real policy instead — accounts are deactivated
    # (is_active=False), never deleted. That matches Case.counselor and
    # Document.owner, which are PROTECT for the same reason, and it means the
    # trail cannot develop holes.
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="audit_events",
        help_text="Null for actions taken by scheduled jobs rather than a person.",
    )
    actor_email = models.EmailField(
        blank=True,
        help_text="Snapshot taken at the time of the action; not affected by later edits.",
    )
    actor_role = models.CharField(max_length=20, blank=True)

    verb = models.CharField(max_length=60, db_index=True)

    target_type = models.CharField(max_length=60, blank=True)
    target_id = models.CharField(max_length=64, blank=True)

    # Which case the action concerned, when applicable. Stored as a plain id so
    # this table has no FK into counseling data.
    case_id_snapshot = models.CharField(max_length=64, blank=True)

    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    request_id = models.CharField(max_length=36, blank=True, db_index=True)

    # Free-form detail: filename, previous role, cancellation reason, etc.
    # Must never contain document contents or a decryption key.
    metadata = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        # Newest first is how this is always read.
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["target_type", "target_id", "-created_at"]),
            models.Index(fields=["actor", "-created_at"]),
            models.Index(fields=["case_id_snapshot", "-created_at"]),
        ]
        # No default permissions: nothing in the application should ever be
        # granted change_auditevent or delete_auditevent.
        default_permissions = ("add", "view")

    def __str__(self) -> str:
        who = self.actor_email or "system"
        return f"{self.created_at:%Y-%m-%d %H:%M} {who} {self.verb}"

    def save(self, *args, **kwargs):
        if self.pk is not None:
            # Belt to the database's braces. Catches a programming mistake at
            # the point of the mistake, with a clearer message than a DB error.
            raise ValueError("AuditEvent rows are append-only and cannot be modified.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("AuditEvent rows are append-only and cannot be deleted.")
