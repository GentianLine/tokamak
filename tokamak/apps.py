from django.apps import AppConfig


class TokamakConfig(AppConfig):
    name = 'tokamak'

    def ready(self):
        from . import signals

        signals.register_receivers()
