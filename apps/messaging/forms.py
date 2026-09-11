"""
The two forms: start a conversation, and answer one.

Both are plain ``Form`` rather than ``ModelForm``. A ModelForm over ``Message``
would let a field be added to the template that sets a column — ``thread``,
``author`` — and the author of a message is not something a browser gets to
state.

Both also carry an optional ``files`` field. Django has no multiple-file field of
its own, so ``MultipleFileField`` below is the documented recipe: the plumbing that
makes ``clean`` run per file instead of once over the last one. What it does *not*
do is decide whether a file is acceptable — that is
``documents.ingest.accept``, reached through the messaging service, because a
size cap enforced only in a form is a cap enforced only for browsers.
"""

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import Role
from apps.messaging.models import MESSAGE_MAX_ATTACHMENTS, MESSAGE_MAX_CHARACTERS


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    """A FileField that keeps every selected file rather than the last one.

    Django's ``FileField.clean`` takes a single value; a ``multiple`` input posts a
    list, and without this the list is silently reduced. Returns a list so the
    caller can hand it straight to the service.
    """

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput())
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single = super().clean
        if isinstance(data, list | tuple):
            return [single(item, initial) for item in data]
        return [single(data, initial)] if data else []


def _body_field(label):
    return forms.CharField(
        label=label,
        max_length=MESSAGE_MAX_CHARACTERS,
        widget=forms.Textarea(attrs={"rows": 6}),
        strip=True,
        help_text=_(
            "Please do not put anything urgent here. Messages are read between "
            "sessions, not immediately."
        ),
    )


def _files_field():
    return MultipleFileField(
        required=False,
        label=_("Attach files"),
        help_text=_(
            "Up to %(limit)s files. They are encrypted, and only the person you "
            "are writing to can open them."
        )
        % {"limit": MESSAGE_MAX_ATTACHMENTS},
    )


class ThreadStartForm(forms.Form):
    subject = forms.CharField(
        max_length=200,
        label=_("What it is about"),
        help_text=_("A few words. The person you are writing to will see this."),
    )
    body = _body_field(_("Message"))
    files = _files_field()

    def __init__(self, *args, case=None, author=None, **kwargs):
        """The ``counselee`` field exists only for a counselor.

        Deleted rather than disabled when a counselee is writing: a disabled field
        is still submitted by a hand-built request, and who the conversation is
        with is what decides who can read it. The service refuses a mismatch too —
        this is the courtesy, that is the control.
        """
        super().__init__(*args, **kwargs)
        if author is not None and author.role == Role.COUNSELEE:
            return

        self.fields["counselee"] = forms.ModelChoiceField(
            # Current members only, and only of this case. The queryset is the
            # whole of the field's security: an id outside it is a validation
            # error rather than a conversation with somebody else's counselee.
            queryset=case.counselees if case is not None else None,
            label=_("Who it is with"),
            empty_label=None,
        )
        # Ordered so the counselee comes first: it is the question the counselor
        # answers before deciding what to write.
        self.order_fields(["counselee", "subject", "body", "files"])


class ReplyForm(forms.Form):
    body = _body_field(_("Reply"))
    files = _files_field()
