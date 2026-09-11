"""
Counseling URLs.

Every route is named, because tests/test_access_matrix.py enumerates the URLconf
by name and fails if a route is missing from its matrix.
"""

from django.urls import path

from apps.counseling import views

app_name = "counseling"

urlpatterns = [
    # The role router. Not the site root any more — that is apps/core's home page,
    # which every role lands on. This is what a view redirects to when it wants
    # "wherever this person belongs" without deciding where that is.
    path("dashboard/", views.dashboard, name="dashboard"),
    # Role landing pages.
    path("caseload/", views.counselor_dashboard, name="counselor_dashboard"),
    path("my-cases/", views.my_cases, name="my_cases"),
    path("billing/caseloads/", views.caseload_index, name="caseload_index"),
    # Cases.
    path("cases/", views.case_list, name="case_list"),
    path("cases/new/", views.case_create, name="case_create"),
    path("cases/<publicid:public_id>/", views.case_detail, name="case_detail"),
    path("cases/<publicid:public_id>/edit/", views.case_edit, name="case_edit"),
    path("cases/<publicid:public_id>/close/", views.case_close, name="case_close"),
    path("cases/<publicid:public_id>/members/add/", views.case_member_add, name="case_member_add"),
    path(
        "cases/<publicid:public_id>/members/<publicid:member_public_id>/end/",
        views.case_member_end,
        name="case_member_end",
    ),
    # People.
    path("counselees/new/", views.counselee_create, name="counselee_create"),
    path("counselees/<publicid:public_id>/", views.counselee_detail, name="counselee_detail"),
    path("profile/practice/", views.counselor_profile_edit, name="counselor_profile_edit"),
    path("profile/intake/", views.counselee_profile_edit, name="counselee_profile_edit"),
    path(
        "counselees/<publicid:public_id>/intake/",
        views.counselee_profile_edit,
        name="counselee_profile_edit_for",
    ),
]
