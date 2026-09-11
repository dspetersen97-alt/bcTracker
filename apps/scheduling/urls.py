"""
Scheduling routes.

Appointments are addressed by their own primary key, like documents, because the
scoping layer decides reachability and a case id in the path would be decoration
a view might be tempted to trust. The two case-keyed routes are the ones where the
case really is the subject: booking into it, and its own diary.

``/availability/`` carries no counselor id at all. That is the point — the view
cannot be pointed at another counselor's office hours because there is nowhere to
put their id.
"""

from django.urls import path

from apps.scheduling import views
from apps.scheduling.google import views as google_views

app_name = "scheduling"

urlpatterns = [
    # A counselor's own office hours.
    path("availability/", views.availability, name="availability"),
    path("availability/add/", views.availability_add, name="availability_add"),
    path(
        "availability/<publicid:public_id>/edit/", views.availability_edit, name="availability_edit"
    ),
    path(
        "availability/<publicid:public_id>/delete/",
        views.availability_delete,
        name="availability_delete",
    ),
    path("availability/exceptions/add/", views.override_add, name="override_add"),
    path(
        "availability/exceptions/<publicid:public_id>/delete/",
        views.override_delete,
        name="override_delete",
    ),
    # The counselor's own Google connection. No counselor id here either, and for
    # the same reason: the consent flow is theirs and nobody else can start it.
    path("availability/google/", google_views.settings_page, name="google_settings"),
    path("availability/google/connect/", google_views.connect, name="google_connect"),
    path("availability/google/callback/", google_views.callback, name="google_callback"),
    path("availability/google/disconnect/", google_views.disconnect, name="google_disconnect"),
    path("availability/google/resync/", google_views.resync, name="google_resync"),
    # The diary.
    path("appointments/", views.appointments, name="appointments"),
    path("appointments/<publicid:public_id>/", views.detail, name="detail"),
    path("appointments/<publicid:public_id>/confirm/", views.confirm, name="confirm"),
    path("appointments/<publicid:public_id>/cancel/", views.cancel, name="cancel"),
    path("appointments/<publicid:public_id>/reschedule/", views.reschedule, name="reschedule"),
    path("appointments/<publicid:public_id>/outcome/", views.outcome, name="outcome"),
    path("appointments/<publicid:public_id>/note/", views.note, name="note"),
    # Keyed by a case, because the case is the subject.
    path(
        "cases/<publicid:case_public_id>/appointments/",
        views.case_appointments,
        name="case_appointments",
    ),
    path("cases/<publicid:case_public_id>/book/", views.book, name="book"),
    path("cases/<publicid:case_public_id>/schedule/", views.schedule, name="schedule"),
]
