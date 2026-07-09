from django.apps import AppConfig


class BaseConfig(AppConfig):
    name = "base"

    def ready(self):
        from .scheduler import start_background_commands

        start_background_commands()
