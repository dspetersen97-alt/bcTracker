from django.apps import AppConfig
from django.urls import register_converter


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"
    verbose_name = "Core"

    def ready(self):
        # Registers the deploy checks for backups. Nothing else imports this
        # module — the checks framework finds them by having been registered.
        from apps.core import checks  # noqa: F401
        from apps.core.ids import PublicIdConverter

        # Every app's URLconf uses <publicid:...>, so the converter has to exist
        # before any of them are imported. It is registered here rather than in
        # config/urls.py because app registry ready() runs during setup and the
        # root URLconf is loaded lazily on the first resolve — and because Django
        # 5.1 deprecated registering the same converter name twice, so this needs
        # a place that runs exactly once per process. An app's ready() is that
        # place; a module imported by eight URLconfs is not.
        register_converter(PublicIdConverter, "publicid")
