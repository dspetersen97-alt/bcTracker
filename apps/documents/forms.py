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
from apps.documents.models import Document, DocumentKind, DocumentTemplate, Visibility


def accept_attribute() -> str:
    return ",".join(sorted(ALLOWED_EXTENSIONS))


def size_limit_mb() -> int:
    return settings.DOCUMENT_MAX_BYTES // 1024 // 1024


# These three are public — and imported by apps/knowledge/forms.py — for the reason
# ``checked_upload`` below exists at all: every form in this application that takes a
# file has to offer and refuse exactly the same set, because one service layer is
# behind all of them. A knowledge base form with its own copy of the allowlist would
# be the first place the two could disagree.


def checked_upload(upload):
    """The cheap refusals, shared by every form in this module that takes a file.

    One function rather than one per form: the library's upload form and a
    counselee's upload form must not drift into accepting different things, because
    the service layer behind both of them accepts exactly one set.
    """
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
            _("That file is too large. The limit is %(limit)s MB.") % {"limit": size_limit_mb()}
        )
    return upload


class DocumentUploadForm(forms.Form):
    file = forms.FileField(
        label=_("File"),
        widget=forms.ClearableFileInput(attrs={"accept": accept_attribute()}),
        # Word is named as being stored as a PDF because that is a surprise
        # otherwise: the file in the list is not the file that was chosen.
        help_text=_("PDF, Word (stored as PDF), Excel, a web page, or a photo. Up to %(limit)s MB.")
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
        return checked_upload(self.cleaned_data["file"])


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


class DocumentTemplateUploadForm(forms.Form):
    """Adding a form or handout to the library.

    ``name`` is required here where a document's ``title`` is optional, because a
    library is read by name: a shelf of ``PDI-v3-FINAL.pdf`` is a shelf nobody can
    search. Left blank the service would fall back to the filename, and this is the
    one place worth insisting instead.
    """

    file = forms.FileField(
        label=_("File"),
        widget=forms.ClearableFileInput(attrs={"accept": accept_attribute()}),
        help_text=_("PDF, Word (stored as PDF), Excel, a web page, or a photo. Up to %(limit)s MB.")
        % {"limit": size_limit_mb()},
    )
    name = forms.CharField(
        max_length=200,
        label=_("Name"),
        help_text=_("What a counselor will look for it under."),
    )
    description = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=_("What it is for, and when to use it. Searched along with the name."),
    )
    kind = forms.ChoiceField(choices=DocumentKind.choices, initial=DocumentKind.HANDOUT)

    def clean_file(self):
        return checked_upload(self.cleaned_data["file"])


class DocumentTemplateEditForm(forms.ModelForm):
    """The label on the shelf, and nothing else.

    The file itself is not replaceable, for the same reason a document's is not: the
    recorded hash describes what was stored, and a template swapped underneath its
    name would silently change what every counselor thought they were handing out.
    A new version is a new template, and the old one is withdrawn.
    """

    class Meta:
        model = DocumentTemplate
        fields = ["name", "description", "kind"]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}


class UseTemplateForm(forms.Form):
    """Copying a template onto a case: what to call it, and who may read it.

    The name is prefilled with the template's and is editable, which is the whole
    of what "use" means here — the copy is a document on that case from the moment
    it is made, so calling it "Homework — week two" rather than "Weekly worksheet"
    is the counselor's to decide.
    """

    name = forms.CharField(
        max_length=200,
        label=_("Name on the case"),
        help_text=_("What this copy will be called. The template keeps its own name."),
    )
    visibility = forms.ChoiceField(
        choices=Visibility.choices,
        initial=Visibility.CASE_SHARED,
        label=_("Who can see this"),
        help_text=_(
            "Shared with the case is the usual answer: a handout nobody on the "
            "case can open has not been handed out."
        ),
    )
