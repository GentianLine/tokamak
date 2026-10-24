"""
Built-in automated processors — the extension points for future automation.

Every receiver here reacts only to signals from ``tokamak.signals``, which is
the contract: an external automation (triage bot, import sync, CI hook) can
do exactly the same things by connecting to the same signals, or by driving
the REST API (which routes through ``tokamak.services`` and therefore fires
the same signals).
"""

from django.dispatch import receiver
from django.utils import timezone

from .models import EventLog, Ticket
from .signals import ticket_created, ticket_state_changed


@receiver(ticket_state_changed)
def auto_assign_active_iteration(sender, ticket, old_state, new_state, **kwargs):
    """
    Triage convenience: when work starts on an un-scheduled ticket, pull it
    into the currently active iteration automatically.
    """
    if new_state != Ticket.State.STARTED or ticket.iteration_id is not None:
        return
    today = timezone.localdate()
    active = ticket.project.iterations.filter(
        start_date__lte=today, end_date__gte=today
    ).first()
    if active:
        from .services import assign_iteration

        assign_iteration(ticket, active)


@receiver(ticket_created)
def auto_estimate_chore(sender, ticket, **kwargs):
    """
    Automation rule: chores created without an estimate default to 1 point so
    they never block velocity math. (Placeholder for a real estimator.)
    """
    if ticket.type == Ticket.Type.CHORE and ticket.story_points is None:
        from .services import estimate_ticket

        estimate_ticket(ticket, 1)


@receiver(ticket_state_changed)
def flag_unestimated_acceptance(sender, ticket, old_state, new_state, **kwargs):
    """
    Safety net: if an automation force-accepts a chore-less ticket without
    points (chores are allowed by the validator), leave an explicit event so
    reporting can exclude/repair it later.
    """
    if new_state == Ticket.State.ACCEPTED and ticket.story_points is None:
        EventLog.objects.create(
            name='ticket.accepted_without_points',
            ticket=ticket,
            project=ticket.project,
            payload={'ticket': ticket.pk},
        )


def mirror_to_memory(event) -> None:
    """clm-mem bridge hook: feed one event-shaped object into memory sessions.

    Used by the live receivers below (registered only when TOKAMAK_MEMORY
    opts in). No-ops unless ``mirror='live'``; never raises, so a broken or
    absent optional dependency can't fail board writes.
    """
    from . import memory

    if memory.get_config()['mirror'] != 'live':
        return
    try:
        memory.mirror_event(event)
    except Exception:  # noqa: S110 — memory is best-effort by contract
        pass


@receiver(ticket_state_changed)
def auto_ticket_sessions(sender, ticket, old_state, new_state, user=None, **kwargs):
    """clm-mem session lifecycle on the board: tickets opening start a chat
    session; leaving the active lane ends it. Gated by TOKAMAK_MEMORY
    ['ticket_sessions']; best-effort so memory never fails a board write."""
    from . import memory as bridge

    if not bridge.sessions_enabled():
        return
    cfg = bridge.get_config()
    start_states = set(cfg.get('session_start_states', ['started']))
    end_states = set(
        cfg.get('session_end_states', ['finished', 'accepted', 'rejected'])
    )
    from . import services
    from .models import MemorySession

    try:
        if new_state in start_states:
            services.open_ticket_session(ticket, user=user)
        elif new_state in end_states or new_state == Ticket.State.UNSTARTED:
            reason = (
                f'ticket:{new_state}'
                if new_state != Ticket.State.UNSTARTED
                else 'withdrawn'
            )
            open_sessions = MemorySession.objects.filter(
                ticket=ticket, status=MemorySession.Status.OPEN
            )
            for sess in open_sessions:
                services.close_ticket_session(sess, reason=reason)
    except Exception:  # noqa: S110 — memory is best-effort by contract
        pass
