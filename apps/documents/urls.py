"""
Document routes.

Documents are addressed by their own primary key rather than nested under a case
path. The scoping layer is what decides reachability, so a case id in the URL
would be decoration — and decoration that invites a view to trust it.
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
]
