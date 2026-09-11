"""Tests for the soft-delete base model and the request-id middleware."""

import pytest
from django.test import override_settings
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.audit.services import record
from apps.core.middleware import get_request_id
from tests.testapp.models import Widget


@pytest.mark.django_db
class TestSoftDelete:
    def test_default_manager_hides_deleted_rows(self, counselor):
        kept = Widget.objects.create(name="kept")
        gone = Widget.objects.create(name="gone")
        gone.soft_delete(by=counselor)

        assert list(Widget.objects.all()) == [kept]
        assert Widget.all_objects.count() == 2

    def test_soft_delete_records_who_and_when(self, counselor):
        widget = Widget.objects.create(name="w")
        before = timezone.now()

        widget.soft_delete(by=counselor)

        widget.refresh_from_db()
        assert widget.deleted_by == counselor
        assert widget.deleted_at >= before
        assert widget.is_deleted is True

    def test_soft_delete_refreshes_updated_at(self):
        """soft_delete names updated_at in update_fields, or auto_now is skipped."""
        widget = Widget.objects.create(name="w")
        original = widget.updated_at

        widget.soft_delete()

        widget.refresh_from_db()
        assert widget.updated_at > original

    def test_queryset_delete_is_soft(self):
        """A bulk .delete() must not destroy counseling records."""
        Widget.objects.create(name="a")
        Widget.objects.create(name="b")

        Widget.objects.all().delete()

        assert Widget.objects.count() == 0
        assert Widget.all_objects.count() == 2
        assert Widget.all_objects.dead().count() == 2

    def test_hard_delete_is_available_but_explicit(self):
        Widget.objects.create(name="a")

        Widget.all_objects.all().hard_delete()

        assert Widget.all_objects.count() == 0

    def test_alive_and_dead_filters(self, counselor):
        alive = Widget.objects.create(name="alive")
        dead = Widget.objects.create(name="dead")
        dead.soft_delete(by=counselor)

        assert list(Widget.all_objects.alive()) == [alive]
        assert list(Widget.all_objects.dead()) == [dead]

    def test_deleting_the_deleter_preserves_the_record(self, counselor):
        """deleted_by is SET_NULL so removing a user cannot erase the record."""
        widget = Widget.objects.create(name="w")
        widget.soft_delete(by=counselor)

        counselor.delete()

        widget.refresh_from_db()
        assert widget.deleted_by is None
        assert widget.is_deleted is True


class TestRequestIDMiddleware:
    def test_request_id_is_set_and_echoed(self, client):
        response = client.get("/healthz")
        assert "X-Request-ID" in response
        assert len(response["X-Request-ID"]) == 32

    def test_request_id_is_not_taken_from_the_client(self, client):
        """A client-supplied id would let a caller forge audit correlation."""
        response = client.get("/healthz", HTTP_X_REQUEST_ID="attacker-chosen")
        assert response["X-Request-ID"] != "attacker-chosen"

    def test_each_request_gets_a_distinct_id(self, client):
        first = client.get("/healthz")["X-Request-ID"]
        second = client.get("/healthz")["X-Request-ID"]
        assert first != second

    def test_context_var_is_empty_outside_a_request(self):
        assert get_request_id() == ""


@pytest.mark.django_db
class TestHealthz:
    def test_healthz_reports_ok_when_the_database_is_reachable(self, client):
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_healthz_requires_no_authentication(self, client):
        # The container healthcheck cannot log in.
        assert client.get("/healthz").status_code == 200

    def test_healthz_leaks_no_configuration_detail(self, client):
        body = client.get("/healthz").json()
        assert set(body) == {"status"}

    @override_settings(SECURE_SSL_REDIRECT=True, SECURE_REDIRECT_EXEMPT=[r"^healthz$"])
    def test_healthz_is_not_redirected_to_https(self, client):
        """The container healthcheck reaches the app with no proxy in front.

        With SECURE_SSL_REDIRECT on and no exemption it gets a 301 to an https
        URL nothing listens on, so the container never reports healthy. The
        pattern here must stay in step with SECURE_REDIRECT_EXEMPT in
        config/settings/prod.py; the point of the test is that it really does
        match the request path, which is easy to get wrong by one slash.
        """
        assert client.get("/healthz").status_code == 200


@pytest.mark.django_db
class TestTimestamps:
    def test_created_at_is_timezone_aware(self):
        """USE_TZ is load-bearing for scheduling; catch it being turned off."""
        widget = Widget.objects.create(name="w")
        assert timezone.is_aware(widget.created_at)

    def test_audit_rows_written_outside_a_request_have_no_request_id(self):
        """This is how scheduled-job activity is distinguished from a session."""
        event = record("test.verb")
        assert event.request_id == ""
        assert AuditEvent.objects.count() == 1
