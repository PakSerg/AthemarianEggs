from django.apps import AppConfig


class PromotionConfig(AppConfig):
    name = 'promotion'

    def ready(self):
        from . import signals  # noqa: F401
