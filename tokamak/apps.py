from django.apps import AppConfig


class TokamakConfig(AppConfig):
    name = 'tokamak'

    def ready(self):
        from . import signals

        signals.register_receivers()
        # Thread automation connects its receivers on import; every one of
        # them no-ops unless TOKAMAK_THREADS opts in (AGENTS.md §1).
        from . import thread_automation  # noqa: F401
