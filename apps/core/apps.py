from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"
    verbose_name = "Core"

    def ready(self):
        # Registers the deploy checks for backups. Nothing else imports this
        # module — the checks framework finds them by having been registered.
        from apps.core import checks  # noqa: F401
