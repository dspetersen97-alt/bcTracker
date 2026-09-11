"""
The mail settings form.

The password field is the whole reason this is not a bare ModelForm: the stored
secret is sealed and cannot be read back into a form, so the field is
write-only. Left blank, it means "keep the password you already have" — which is
what an administrator correcting a typo in the From address expects, and the
opposite of what a plain ModelForm would do.
"""

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.core import mail
from apps.core.models import MailSettings


class MailSettingsForm(forms.ModelForm):
    password = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False),
        label=_("Password"),
        help_text=_(
            "For Google Workspace this must be an App Password, not the account "
            "password. Leave blank to keep the password already stored. It is "
            "encrypted with the ministry's master key and can never be read back "
            "out of this page."
        ),
    )

    class Meta:
        model = MailSettings
        fields = ["host", "port", "use_tls", "username", "from_email"]

    def clean_from_email(self):
        return self.cleaned_data["from_email"].strip()

    def clean(self):
        cleaned = super().clean()
        # A host with no credentials is not a configuration, it is a half-typed
        # one, and it would fail on the next invitation rather than here.
        if cleaned.get("host") and not cleaned.get("username"):
            self.add_error("username", _("A server needs the mailbox that signs in to it."))
        if cleaned.get("username") and not (cleaned.get("password") or self.instance.has_password):
            self.add_error("password", _("Enter the password for this mailbox."))
        return cleaned

    def save(self, commit=True, *, actor=None):
        row = super().save(commit=False)
        password = self.cleaned_data.get("password") or ""
        if password:
            # Sealed onto the row in memory; written by the save below, so the
            # ciphertext and the host it belongs to land together.
            mail.set_password(row, password)
        row.updated_by = actor
        if commit:
            row.save()
        return row
