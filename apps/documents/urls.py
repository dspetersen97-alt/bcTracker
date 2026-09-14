"""
Document routes.

Documents are addressed by their own public id rather than nested under a case
path. The scoping layer is what decides reachability, so a case id in the URL
would be decoration — and decoration that invites a view to trust it.

``publicid`` rather than ``int``: the ten random digits are what appears in a URL
anywhere in this application, and the converter refuses the sequential id the
row's primary key still is. See apps/core/ids.py.
"""

from django.urls import path

from apps.documents import views

app_name = "documents"

urlpatterns = [
    path("my-documents/", views.my_documents, name="my_documents"),
    path("cases/<publicid:case_public_id>/documents/", views.case_documents, name="case_documents"),
    path("cases/<publicid:case_public_id>/documents/upload/", views.upload, name="upload"),
    path("documents/<publicid:public_id>/", views.detail, name="detail"),
    path("documents/<publicid:public_id>/download/", views.download, name="download"),
    path("documents/<publicid:public_id>/view/", views.preview, name="preview"),
    path("documents/<publicid:public_id>/thumbnail/", views.thumbnail, name="thumbnail"),
    path("documents/<publicid:public_id>/edit/", views.edit, name="edit"),
    path("documents/<publicid:public_id>/share/", views.share, name="share"),
    path("documents/<publicid:public_id>/delete/", views.delete, name="delete"),
    # The template library. Not nested under a case, because a template belongs to
    # the ministry rather than to anybody's counseling — the one route here that does
    # carry a case is ``template_use``, where the case is what is being written to.
    path("templates/", views.template_library, name="template_library"),
    path("templates/add/", views.template_upload, name="template_upload"),
    path(
        "templates/<publicid:public_id>/download/",
        views.template_download,
        name="template_download",
    ),
    path(
        "templates/<publicid:public_id>/thumbnail/",
        views.template_thumbnail,
        name="template_thumbnail",
    ),
    path("templates/<publicid:public_id>/edit/", views.template_edit, name="template_edit"),
    path(
        "templates/<publicid:public_id>/withdraw/",
        views.template_withdraw,
        name="template_withdraw",
    ),
    path(
        "cases/<publicid:case_public_id>/documents/from-template/<publicid:public_id>/",
        views.template_use,
        name="template_use",
    ),
]
