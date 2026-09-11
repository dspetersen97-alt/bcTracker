from django.apps import AppConfig


class SchedulingConfig(AppConfig):
    name = "apps.scheduling"
    verbose_name = "Scheduling"

    def ready(self):
        # Importing registers the deploy checks: a Google integration switched on
        # without a hosted-domain restriction stops the container from starting.
        from apps.scheduling import checks  # noqa: F401
