"""
Upload and edit forms.

The form does the cheap refusals — extension, declared size — so a file that was
never going to be accepted is rejected before ``store_document`` reads it. The
authoritative checks are still in the service layer, because a form is one caller
and the rule belongs where the bytes are.
"""

from django import forms
from django.conf import settings
from django.utils.translation import gettext_lazy as _

from apps.documents.filetypes import ALLOWED_EXTENSIONS
from apps.documents.models import Document, DocumentKind, Visibility


def _accept_attribute() -> str:
    return ",".join(sorted(ALLOWED_EXTENSIONS))


class DocumentUploadForm(forms.Form):
    file = forms.FileField(
        label=_("File"),
        widget=forms.ClearableFileInput(attrs={"accept": _accept_attribute()}),
        help_text=_("PDF, Word, Excel, or a photo. Up to %(limit)s MB.")
        % {"limit": settings.DOCUMENT_MAX_BYTES // 1024 // 1024},
    )
    title = forms.CharField(
        max_length=200,
        required=False,
        help_text=_("Optional. Left blank, the file's own name is used."),
    )
    description = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    kind = forms.ChoiceField(choices=DocumentKind.choices, initial=DocumentKind.OTHER)
    visibility = forms.ChoiceField(
        choices=Visibility.choices,
        initial=Visibility.PRIVATE,
        required=False,
        label=_("Who can see this"),
    )

    def __init__(self, *args, may_choose_visibility=False, **kwargs):
        super().__init__(*args, **kwargs)
        if not may_choose_visibility:
            # Removed rather than disabled. A disabled field is still submitted by
            # a hand-built request, and only the counselor decides what the whole
            # case sees — see documents.share_document.
            del self.fields["visibility"]

    def clean_file(self):
        upload = self.cleaned_data["file"]
        extension = ("." + upload.name.rsplit(".", 1)[-1].lower()) if "." in upload.name else ""
        if extension not in ALLOWED_EXTENSIONS:
            raise forms.ValidationError(
                _("“%(ext)s” files are not accepted. Allowed: %(allowed)s.")
                % {
                    "ext": extension or upload.name,
                    "allowed": ", ".join(sorted(ALLOWED_EXTENSIONS)),
                }
            )
        if upload.size and upload.size > settings.DOCUMENT_MAX_BYTES:
            raise forms.ValidationError(
                _("That file is too large. The limit is %(limit)s MB.")
                % {"limit": settings.DOCUMENT_MAX_BYTES // 1024 // 1024}
            )
        return upload


class DocumentEditForm(forms.ModelForm):
    """Labels only.

    Visibility is absent on purpose: changing who can read a file goes through
    documents:share, which has its own permission and its own audit verb. Folding
    it into a general edit form would make a disclosure look like a typo fix in
    the trail.
    """

    class Meta:
        model = Document
        fields = ["title", "description", "kind"]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}
