"""
Forms for contributing to the knowledge base.

The file allowlist, the size limit and the ``accept`` attribute come from
``apps.documents.forms`` rather than being restated here: one service layer accepts
one set of file types, and a second copy of that list is the first thing that would
drift out of step with it.
"""

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.documents.forms import accept_attribute, checked_upload, size_limit_mb
from apps.knowledge.models import Resource, ResourceComment, ResourceKind


def LinkField(**kwargs):  # noqa: N802 — a field factory, named like the field it makes
    """The link field, defined once for the two forms that offer it.

    ``assume_scheme="https"`` for the reason ``MeetingLinkField`` names it in
    apps/scheduling/forms.py: what a counselor pastes is often ``example.org/article``,
    and completing that is better than refusing it. Django 6 makes https the default;
    saying it now means the completion is not silently http until then.
    """
    return forms.URLField(
        required=False,
        max_length=500,
        assume_scheme="https",
        label=_("Link"),
        help_text=_("A link to the article, video or shop page, if there is one."),
        **kwargs,
    )


class ResourceForm(forms.ModelForm):
    """Adding something to the shelf: a file, a link, or both.

    One form for the two rather than a choice of two forms, because a counselor
    thinking "I want to share this handout" should not have to answer "is it a file or
    a link?" before they can start. The clean below insists on one of them, which is
    the same rule the ``knowledge_resource_has_a_file_or_a_link`` constraint states in
    the database.
    """

    file = forms.FileField(
        label=_("File"),
        required=False,
        widget=forms.ClearableFileInput(attrs={"accept": accept_attribute()}),
        help_text=_("PDF, Word (stored as PDF), Excel, a web page, or a photo. Up to %(limit)s MB.")
        % {"limit": size_limit_mb()},
    )
    url = LinkField()

    class Meta:
        model = Resource
        fields = ["title", "summary", "topics", "kind", "url"]
        widgets = {"summary": forms.Textarea(attrs={"rows": 4})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # A handout is the commonest contribution, and "Other" as the opening value
        # is how a shelf ends up unsearchable by kind.
        self.fields["kind"].initial = ResourceKind.HANDOUT

    def clean_file(self):
        upload = self.cleaned_data.get("file")
        if not upload:
            return upload
        return checked_upload(upload)

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("url") and not cleaned.get("file"):
            # Raised against the form rather than one field: neither of the two is the
            # one that is missing, and blaming "File" would read as an instruction to
            # upload something when a link was the intention.
            raise forms.ValidationError(
                _("Add a file or a link — a title on its own is not something anyone can open.")
            )
        return cleaned


class ResourceEditForm(forms.ModelForm):
    """Relabelling one. The file is not among the fields, on purpose.

    A new version of a handout is a new resource: replacing the bytes under an
    existing title would silently change what colleagues who commented underneath
    were talking about, and the recorded hash would describe something that no longer
    exists. The link *is* editable, because a moved article is still the same article.
    """

    url = LinkField()

    class Meta:
        model = Resource
        fields = ["title", "summary", "topics", "kind", "url"]
        widgets = {"summary": forms.Textarea(attrs={"rows": 4})}


class CommentForm(forms.ModelForm):
    class Meta:
        model = ResourceComment
        fields = ["body"]
        widgets = {
            "body": forms.Textarea(
                attrs={"rows": 3, "placeholder": _("What did you use this for, and how did it go?")}
            )
        }
        labels = {"body": _("Add a note for the other counselors")}
