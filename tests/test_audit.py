"""
Tests for the audit trail.

The append-only guarantee is the point of this app, so it is tested at both
levels: the Python guard and the database trigger that holds even if the guard
is bypassed.
"""

import pytest
from django.db import DatabaseError, connection, transaction
from django.db.models import ProtectedError

from apps.accounts.models import Role
from apps.audit.models import AuditEvent, AuditVerb
from apps.audit.services import record, record_or_raise


@pytest.mark.django_db
class TestRecording:
    def test_record_snapshots_the_actor(self, counselor):
        event = record(AuditVerb.LOGIN_SUCCEEDED, actor=counselor)
        assert event is not None
        assert event.actor == counselor
        assert event.actor_email == counselor.email
        assert event.actor_role == "counselor"

    def test_a_user_with_history_cannot_be_deleted(self, counselor):
        """Accounts are deactivated, never deleted.

        The audit FK is PROTECT so the trail cannot develop holes. Deactivation
        is the supported way to remove someone's access.
        """
        record(AuditVerb.DOCUMENT_DOWNLOADED, actor=counselor)

        with pytest.raises(ProtectedError):
            counselor.delete()

        counselor.is_active = False
        counselor.save(update_fields=["is_active"])
        assert AuditEvent.objects.filter(actor=counselor).count() == 1

    def test_actor_snapshot_records_the_role_held_at_the_time(self, counselor):
        """A later role change must not rewrite what the trail says.

        This is why actor_role is stored rather than read through the FK.
        """
        event_id = record(AuditVerb.DOCUMENT_DOWNLOADED, actor=counselor).pk

        counselor.role = Role.FINANCIAL_ADMIN
        counselor.save(update_fields=["role"])

        event = AuditEvent.objects.get(pk=event_id)
        assert event.actor_role == Role.COUNSELOR
        assert event.actor.role == Role.FINANCIAL_ADMIN

    def test_target_is_stored_as_strings_not_a_foreign_key(self, counselee):
        event = record(AuditVerb.USER_CREATED, target=counselee)
        assert event.target_type == "accounts.User"
        assert event.target_id == str(counselee.pk)

    def test_metadata_captures_extra_detail(self, counselor):
        event = record(AuditVerb.DOCUMENT_UPLOADED, actor=counselor, filename="intake.pdf")
        assert event.metadata == {"filename": "intake.pdf"}

    def test_system_actions_have_no_actor(self):
        event = record(AuditVerb.MAGIC_LINK_SENT)
        assert event.actor is None
        assert event.actor_email == ""

    def test_record_swallows_failures(self, monkeypatch, counselor):
        """Losing one audit row must not fail the user's request."""

        def boom(self, *args, **kwargs):
            raise DatabaseError("simulated")

        monkeypatch.setattr(AuditEvent, "save", boom)
        assert record(AuditVerb.LOGOUT, actor=counselor) is None

    def test_record_or_raise_propagates_failures(self, monkeypatch, counselor):
        """Document access must not be served if it cannot be recorded."""

        def boom(self, *args, **kwargs):
            raise DatabaseError("simulated")

        monkeypatch.setattr(AuditEvent, "save", boom)
        with pytest.raises(DatabaseError):
            record_or_raise(AuditVerb.DOCUMENT_DOWNLOADED, actor=counselor)


@pytest.mark.django_db
class TestAppendOnly:
    def test_python_guard_blocks_update(self, counselor):
        event = record(AuditVerb.LOGIN_SUCCEEDED, actor=counselor)
        event.verb = AuditVerb.LOGOUT
        with pytest.raises(ValueError):
            event.save()

    def test_python_guard_blocks_delete(self, counselor):
        event = record(AuditVerb.LOGIN_SUCCEEDED, actor=counselor)
        with pytest.raises(ValueError):
            event.delete()

    def test_database_trigger_blocks_update_bypassing_the_orm(self, counselor):
        """The guarantee must hold even for raw SQL.

        This is the case that matters: the Python guard protects against
        programmer error, but only the trigger protects against a queryset
        .update() or an injected statement.
        """
        event = record(AuditVerb.LOGIN_SUCCEEDED, actor=counselor)

        with pytest.raises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE audit_auditevent SET verb = %s WHERE id = %s",
                    ["tampered", event.pk],
                )

        event.refresh_from_db()
        assert event.verb == AuditVerb.LOGIN_SUCCEEDED

    def test_database_trigger_blocks_delete_bypassing_the_orm(self, counselor):
        event = record(AuditVerb.LOGIN_SUCCEEDED, actor=counselor)

        with pytest.raises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM audit_auditevent WHERE id = %s", [event.pk])

        assert AuditEvent.objects.filter(pk=event.pk).exists()

    def test_queryset_update_is_blocked(self, counselor):
        record(AuditVerb.LOGIN_SUCCEEDED, actor=counselor)
        with pytest.raises(DatabaseError), transaction.atomic():
            AuditEvent.objects.all().update(verb="tampered")

    def test_no_change_or_delete_permissions_exist(self):
        """Nothing in the app should be grantable change/delete on audit rows."""
        codenames = set(
            AuditEvent._meta.default_permissions,
        )
        assert codenames == {"add", "view"}


@pytest.mark.django_db
class TestRequestContext:
    def test_request_metadata_is_captured(self, rf, counselor):
        request = rf.get("/", HTTP_USER_AGENT="pytest-agent", REMOTE_ADDR="203.0.113.7")
        request.user = counselor

        event = record(AuditVerb.DOCUMENT_VIEWED, request=request)

        assert event.actor == counselor
        assert event.user_agent == "pytest-agent"
        assert event.ip == "203.0.113.7"

    def test_forwarded_for_uses_the_proxy_appended_entry(self, rf, counselor):
        """A client-supplied X-Forwarded-For prefix must not be trusted."""
        request = rf.get(
            "/",
            HTTP_X_FORWARDED_FOR="198.51.100.1, 203.0.113.7",
            REMOTE_ADDR="10.0.0.2",
        )
        request.user = counselor

        event = record(AuditVerb.DOCUMENT_VIEWED, request=request)
        assert event.ip == "203.0.113.7"
