"""
Messaging routes.

A thread is addressed by its own public id, with no case in the path, for the
reason documents/urls.py gives: the scoping layer decides reachability, so a case
id in the URL would be decoration that invites a view to trust it. The two routes
that *are* nested under a case are the ones whose subject is the case — its list
of conversations, and starting a new one.

There is no route that edits or deletes a message. That is the model's policy, not
an omission; see Message.save.

An attachment is addressed by its own key too, and there is exactly one route for
it: no thumbnail, no inline preview, no upload endpoint. A file arrives as part of
a message, so it is posted to the two routes that write one.
"""

from django.urls import path

from apps.messaging import views

app_name = "messaging"

urlpatterns = [
    path("messages/", views.index, name="index"),
    path("cases/<publicid:case_public_id>/messages/", views.case_threads, name="case_threads"),
    path("cases/<publicid:case_public_id>/messages/new/", views.start, name="start"),
    path("messages/attachments/<publicid:public_id>/", views.attachment, name="attachment"),
    path("messages/<publicid:public_id>/", views.thread, name="thread"),
    path("messages/<publicid:public_id>/close/", views.close, name="close"),
    path("messages/<publicid:public_id>/reopen/", views.reopen, name="reopen"),
]
