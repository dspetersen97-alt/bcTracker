from django.apps import AppConfig


class DocumentsConfig(AppConfig):
    name = "apps.documents"
    verbose_name = "Documents"

    def ready(self):
        # Importing registers the deploy checks. A missing master key or disabled
        # virus scanning then stops the container from starting, rather than being
        # discovered when someone opens a document.
        from apps.documents import checks  # noqa: F401
