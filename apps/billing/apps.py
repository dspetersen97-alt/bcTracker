from django.apps import AppConfig


class BillingConfig(AppConfig):
    name = "apps.billing"
    verbose_name = "Billing"

    def ready(self):
        # Registers the deploy checks. Imported here rather than at module scope
        # so importing the app config does not pull in settings access.
        from apps.billing import checks  # noqa: F401
