"""Service-layer workflow rules and custom signal contract.

Covers: legal/illegal transitions, Fibonacci estimate enforcement, the
transition-path graph walker, and the built-in automation receivers
(chore auto-estimate, active-siteration pull-in).
"""

from datetime import date, timedelta

import pytest
from django.core.exceptions import ValidationError

from tokamak import services
from tokamak.models import EventLog, Ticket
from tokamak.signals import ticket_state_changed

from tests._utils import make_iteration, make_project


def _connect_once(uid):
    """Return (received_list, receiver) for one-shot signal capture."""
    received = []

    def receiver(sender, **kwargs):
        received.append(kwargs)

    ticket_state_changed.connect(receiver, dispatch_uid=uid)
    return received, receiver


@pytest.mark.django_db
def test_transition_fires_custom_signal_and_audit_log():
    """A legal ``unstarted -> started`` transition emits the custom signal
    exactly once with the contract payload and is mirrored into EventLog."""
    project = make_project()
    received, receiver = _connect_once('test_sig')
    try:
        ticket = services.create_ticket(project, name='A')
        services.transition_ticket(ticket, Ticket.State.STARTED)
    finally:
        ticket_state_changed.disconnect(dispatch_uid='test_sig', receiver=receiver)

    assert len(received) == 1
    assert received[0]['old_state'] == 'unstarted'
    assert received[0]['new_state'] == 'started'
    ticket.refresh_from_db()
    assert ticket.state == Ticket.State.STARTED
    assert EventLog.objects.filter(ticket=ticket, name='ticket.state_changed').exists()


@pytest.mark.django_db
def test_illegal_transition_rejected():
    """Skipping straight to ``accepted`` violates the workflow graph."""
    project = make_project()
    ticket = services.create_ticket(project, name='A')
    with pytest.raises(ValidationError):
        services.transition_ticket(ticket, Ticket.State.ACCEPTED)


@pytest.mark.django_db
def test_accept_requires_estimate():
    """An unestimated feature cannot hop from ``finished`` to ``accepted``."""
    project = make_project()
    ticket = services.create_ticket(project, name='A')
    services.transition_ticket(ticket, Ticket.State.STARTED)
    services.transition_ticket(ticket, Ticket.State.FINISHED)
    with pytest.raises(ValidationError):
        services.transition_ticket(ticket, Ticket.State.ACCEPTED)


@pytest.mark.django_db
def test_estimate_must_be_fibonacci_and_fires_signal():
    """Only Fibonacci values are accepted; a legal estimate emits
    ``ticket.estimated`` into the audit log."""
    project = make_project()
    ticket = services.create_ticket(project, name='A')
    with pytest.raises(ValidationError):
        services.estimate_ticket(ticket, 4)
    services.estimate_ticket(ticket, 5)
    ticket.refresh_from_db()
    assert ticket.story_points == 5
    assert EventLog.objects.filter(name='ticket.estimated', payload__new=5).exists()


@pytest.mark.django_db
def test_automation_chore_auto_estimated():
    """Signal receiver auto-estimates unestimated chores at 1 point."""
    project = make_project()
    chore = services.create_ticket(project, name='Cleanup', type=Ticket.Type.CHORE)
    assert chore.story_points == 1


@pytest.mark.django_db
def test_automation_starts_pull_into_active_iteration():
    """Starting an unscheduled ticket pulls it into the active iteration."""
    project = make_project()
    today = date.today()
    active = make_iteration(project, 1, today - timedelta(days=3))
    ticket = services.create_ticket(project, name='A')
    services.transition_ticket(ticket, Ticket.State.STARTED)
    ticket.refresh_from_db()
    assert ticket.iteration == active


@pytest.mark.django_db
def test_transition_path_walks_graph():
    """``transition_path`` returns the shortest legal multi-hop route."""
    path = services.transition_path('unstarted', 'accepted')
    assert path == ['started', 'finished', 'accepted']
    # accepted -> unstarted requires walking back through the graph
    back = services.transition_path('accepted', 'unstarted')
    assert len(back) == 3
    assert back[-1] == Ticket.State.UNSTARTED
