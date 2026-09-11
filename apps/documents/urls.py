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
    path("cases/<int:case_pk>/documents/", views.case_documents, name="case_documents"),
    path("cases/<int:case_pk>/documents/upload/", views.upload, name="upload"),
    path("documents/<int:pk>/", views.detail, name="detail"),
    path("documents/<int:pk>/download/", views.download, name="download"),
    path("documents/<int:pk>/thumbnail/", views.thumbnail, name="thumbnail"),
    path("documents/<int:pk>/edit/", views.edit, name="edit"),
    path("documents/<int:pk>/share/", views.share, name="share"),
    path("documents/<int:pk>/delete/", views.delete, name="delete"),
]
