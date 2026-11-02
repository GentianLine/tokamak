from django.apps import AppConfig


class TokamakConfig(AppConfig):
    name = 'tokamak'

    def ready(self):
        from . import signals

        signals.register_receivers()

        # New lanes register their receivers here rather than editing other
        # modules (AGENTS.md §3): webhook fan-out connects itself only when
        # TOKAMAK.WEBHOOKS opts in, so the baseline signal graph is intact.
        from . import webhooks

        webhooks.register_fanout()
