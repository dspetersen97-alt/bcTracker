"""
The mail settings form.

Two fields here are not the model's own, and both exist for a reason:

  * **the password is write-only.** The stored secret is sealed and cannot be read
    back into a form, so left blank it means "keep the password you already have"
    — which is what an administrator correcting a typo in the From address
    expects, and the opposite of what a plain ModelForm would do.
  * **encryption is one choice rather than two checkboxes.** The model stores
    Django's ``use_tls`` and ``use_ssl`` separately, and setting both raises
    inside the SMTP backend — a 500 on the next invitation rather than an error on
    this page. Offering the pair as one choice makes that state unreachable, and
    makes "neither" unreachable too: there is no option here for sending a mailbox
    password across the internet in the clear.
"""

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.core import mail
from apps.core.models import MailSettings


class MailSettingsForm(forms.ModelForm):
    #: The two ways a provider offers encrypted SMTP, named as the providers name
    #: them rather than as Django's settings do.
    STARTTLS = "starttls"
    IMPLICIT_TLS = "ssl"

    #: The port each one is offered on, which is the same everywhere it matters
    #: and is worth checking because the wrong pairing does not fail — it hangs
    #: until the socket times out, and the administrator is told nothing useful.
    CONVENTIONAL_PORTS = {587: STARTTLS, 465: IMPLICIT_TLS}

    #: For error messages, where the full option labels would read as nonsense.
    SHORT_NAMES = {STARTTLS: _("STARTTLS"), IMPLICIT_TLS: _("SSL/TLS")}

    encryption = forms.ChoiceField(
        choices=[
            (STARTTLS, _("STARTTLS on port 587 — Google Workspace, and Zoho's default")),
            (IMPLICIT_TLS, _("SSL/TLS on port 465 — offered by Zoho and most others")),
        ],
        widget=forms.RadioSelect,
        label=_("Encryption"),
        help_text=_(
            "Whichever your provider documents. Both are encrypted the whole way; "
            "there is deliberately no unencrypted option."
        ),
    )
    password = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False),
        label=_("Password"),
        help_text=_(
            "An app password, not the account's own: Google Workspace refuses the "
            "account password outright, and Zoho needs one whenever two-factor "
            "authentication is on. Leave blank to keep the password already "
            "stored. It is encrypted with the ministry's master key and can never "
            "be read back out of this page."
        ),
    )

    class Meta:
        model = MailSettings
        fields = ["host", "port", "username", "from_email"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["encryption"].initial = (
            self.IMPLICIT_TLS if self.instance.use_ssl else self.STARTTLS
        )

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
        expected = self.CONVENTIONAL_PORTS.get(cleaned.get("port"))
        if expected and cleaned.get("encryption") and cleaned["encryption"] != expected:
            # Refused rather than corrected: guessing which of the two the
            # administrator meant would be a guess about the provider.
            self.add_error(
                "encryption",
                _(
                    "Port %(port)s is normally %(expected)s. Check what your provider "
                    "documents — the wrong pairing does not fail, it hangs."
                )
                % {"port": cleaned["port"], "expected": self.SHORT_NAMES[expected]},
            )
        return cleaned

    def save(self, commit=True, *, actor=None):
        row = super().save(commit=False)
        row.use_ssl = self.cleaned_data.get("encryption") == self.IMPLICIT_TLS
        row.use_tls = not row.use_ssl
        password = self.cleaned_data.get("password") or ""
        if password:
            # Sealed onto the row in memory; written by the save below, so the
            # ciphertext and the host it belongs to land together.
            mail.set_password(row, password)
        row.updated_by = actor
        if commit:
            row.save()
        return row
