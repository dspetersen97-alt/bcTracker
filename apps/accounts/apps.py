from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.accounts"
    verbose_name = "Accounts"

    def ready(self):
        # ``checks`` registers the deploy checks, so a Google sign-in that is
        # configured too partially to work says so rather than just not appearing.
        # ``lockout`` connects the receiver that audits a login lockout; it is
        # imported here because nothing else imports it — django-axes reaches the
        # response callable by dotted path, so the module would never load.
        from apps.accounts import checks, lockout  # noqa: F401
