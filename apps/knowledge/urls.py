"""
Knowledge base routes.

Addressed by public id like everything else — ten random digits, so a link to one
resource cannot be turned into a link to the next by adding one. See apps/core/ids.py.

Comments hang off ``/knowledge/comments/<id>/remove/`` rather than under their
resource. The comment's own id is enough to find it, and its resource is on the row:
a path carrying both would give a caller two things to disagree about.
"""

from django.urls import path

from apps.knowledge import views

app_name = "knowledge"

urlpatterns = [
    path("knowledge/", views.home, name="home"),
    path("knowledge/add/", views.add, name="add"),
    path("knowledge/<publicid:public_id>/", views.detail, name="detail"),
    path("knowledge/<publicid:public_id>/download/", views.download, name="download"),
    path("knowledge/<publicid:public_id>/edit/", views.edit, name="edit"),
    path("knowledge/<publicid:public_id>/remove/", views.remove, name="remove"),
    path("knowledge/<publicid:public_id>/comment/", views.comment, name="comment"),
    path(
        "knowledge/comments/<publicid:public_id>/remove/",
        views.comment_remove,
        name="comment_remove",
    ),
]
