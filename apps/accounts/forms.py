"""Authentication forms."""

from django import forms
from django.contrib.auth import password_validation
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _


class EmailAuthenticationForm(AuthenticationForm):
    """Password login, keyed on email.

    AuthenticationForm calls its first field ``username`` regardless of
    USERNAME_FIELD, so this only relabels it. The error message stays Django's
    single "email address and password didn't match": saying which of the two was
    wrong would confirm whether an address has an account here.
    """

    username = forms.EmailField(
        label=_("Email address"),
        widget=forms.EmailInput(attrs={"autofocus": True, "autocomplete": "username"}),
    )

    error_messages = {
        **AuthenticationForm.error_messages,
        "invalid_login": _("That email address and password didn't match an active account."),
    }


class MagicLinkRequestForm(forms.Form):
    """Ask for a passwordless login link."""

    email = forms.EmailField(
        label=_("Email address"),
        widget=forms.EmailInput(attrs={"autofocus": True, "autocomplete": "email"}),
    )


class TOTPCodeForm(forms.Form):
    """A six-digit authenticator code.

    Validation of the code itself is not here: it needs the device, and doing it
    in the view keeps the throttling and audit decisions in one place.
    """

    code = forms.CharField(
        label=_("Authentication code"),
        min_length=6,
        max_length=6,
        strip=True,
        widget=forms.TextInput(
            attrs={
                "autofocus": True,
                "autocomplete": "one-time-code",
                "inputmode": "numeric",
                "pattern": "[0-9]*",
            }
        ),
    )

    def clean_code(self):
        code = self.cleaned_data["code"]
        if not code.isdigit():
            raise ValidationError(_("Enter the six digits from your authenticator app."))
        return code


class SetPasswordForm(forms.Form):
    """Choose a first or replacement password.

    Django ships SetPasswordForm already; this one exists because ours is used
    from a token flow where there is no logged-in user to bind to, and because
    the validators need the user for the similarity check.
    """

    new_password1 = forms.CharField(
        label=_("New password"),
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password", "autofocus": True}),
        help_text=_("At least 12 characters. A passphrase of a few words is ideal."),
    )
    new_password2 = forms.CharField(
        label=_("Confirm new password"),
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )

    def __init__(self, user, *args, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned = super().clean()
        first = cleaned.get("new_password1")
        second = cleaned.get("new_password2")
        if first and second and first != second:
            self.add_error("new_password2", _("The two passwords didn't match."))
        if first:
            # Passing the user runs UserAttributeSimilarityValidator, which is
            # what stops someone using their own email address as a password.
            password_validation.validate_password(first, self.user)
        return cleaned

    def save(self):
        self.user.set_password(self.cleaned_data["new_password1"])
        self.user.save(update_fields=["password", "updated_at"])
        return self.user
