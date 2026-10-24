"""Edge-case coverage for the service layer workflow rules.

Targeted gaps from the test-coverage-lift task: the rejected->started
recovery path and cross-project iteration assignment (plus their direct
collaterals: same-state no-ops, unknown states, unassigning, signal
payloads). New file per AGENTS.md §3 rule 3; existing modules untouched.
"""

from datetime import date, timedelta

import pytest
from django.core.exceptions import ValidationError

from tokamak import services
from tokamak.models import EventLog, Ticket

from tests._utils import make_iteration, make_project


def _walk(ticket, states):
    """Transition a ticket through ``states`` one hop at a time."""
    for st in states:
        services.transition_ticket(ticket, st)


@pytest.mark.django_db
def test_rejected_to_started_recovery_persists_and_audits():
    """A rejected ticket can be recovered straight to ``started``; the
    transition persists and lands in the audit log."""
    project = make_project('Recovery')
    ticket = services.create_ticket(project, name='Spiked spike')
    _walk(ticket, ('started', 'finished', 'rejected'))
    assert ticket.state == Ticket.State.REJECTED

    services.transition_ticket(ticket, Ticket.State.STARTED)
    ticket.refresh_from_db()
    assert ticket.state == Ticket.State.STARTED
    row = (
        EventLog.objects.filter(name='ticket.state_changed', ticket=ticket)
        .order_by('-id')
        .first()
    )
    assert row.payload['old'] == 'rejected'
    assert row.payload['new'] == 'started'


@pytest.mark.django_db
def test_rejected_clears_completion_timestamp_on_recovery():
    """``completed_at`` is stamped on rejection and cleared again when the
    ticket recovers to ``started``."""
    project = make_project('Timestamps')
    ticket = services.create_ticket(project, name='Clockwork')
    _walk(ticket, ('started', 'finished', 'rejected'))
    assert ticket.completed_at is not None

    services.transition_ticket(ticket, Ticket.State.STARTED)
    ticket.refresh_from_db()
    assert ticket.completed_at is None


@pytest.mark.django_db
def test_rejected_recovery_carries_estimate():
    """A recovered rejected feature keeps its estimate and can be accepted
    again without re-estimating (rejected -> started -> finished -> accepted)."""
    project = make_project('Carry')
    ticket = services.create_ticket(project, name='Twice-delivered')
    services.estimate_ticket(ticket, 3)
    _walk(ticket, ('started', 'finished', 'accepted'))
    assert ticket.completed_at is not None

    # reject from accepted, then recover and re-deliver
    services.transition_ticket(ticket, Ticket.State.REJECTED)
    ticket.refresh_from_db()
    assert ticket.story_points == 3
    _walk(ticket, ('started', 'finished', 'accepted'))
    assert ticket.state == Ticket.State.ACCEPTED


@pytest.mark.django_db
def test_rejected_state_matrix():
    """From ``rejected`` the only legal move is ``started``; every other
    target raises and leaves state untouched."""
    project = make_project('Matrix')
    ticket = services.create_ticket(project, name='Locked out')
    _walk(ticket, ('started', 'finished', 'rejected'))
    # Same-state moves are no-ops by contract (see the noop test below), so
    # only genuinely different targets must raise from ``rejected``.
    for target in (
        Ticket.State.UNSTARTED,
        Ticket.State.FINISHED,
        Ticket.State.ACCEPTED,
    ):
        with pytest.raises(ValidationError):
            services.transition_ticket(ticket, target)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REJECTED


@pytest.mark.django_db
def test_cross_project_assignment_rejected():
    """Assigning a ticket to an iteration owned by a *different* project is
    refused and the ticket keeps its old (empty) iteration."""
    home = make_project('Home')
    other = make_project('Other')
    it = make_iteration(other, 1, date.today() - timedelta(days=3))
    ticket = services.create_ticket(home, name='Drifter')

    with pytest.raises(ValidationError) as exc:
        services.assign_iteration(ticket, it)
    assert 'another project' in str(exc.value)
    ticket.refresh_from_db()
    assert ticket.iteration is None
    assert (
        EventLog.objects.filter(name='ticket.iteration_changed', ticket=ticket).count()
        == 0
    )


@pytest.mark.django_db
def test_same_project_assignment_succeeds_and_audits():
    """The mirror-image positive control: same-project assignment works and
    fires ``ticket.iteration_changed`` into the audit log."""
    project = make_project('Same')
    it = make_iteration(project, 7, date.today() - timedelta(days=3))
    ticket = services.create_ticket(project, name='Local')

    result = services.assign_iteration(ticket, it)
    assert result is ticket or result.pk == ticket.pk
    ticket.refresh_from_db()
    assert ticket.iteration == it
    row = (
        EventLog.objects.filter(name='ticket.iteration_changed', ticket=ticket)
        .order_by('-id')
        .first()
    )
    assert row.payload['new'] == 7 and row.payload['old'] is None


@pytest.mark.django_db
def test_cross_project_assignment_via_rest_returns_400(api_client):
    """POST /api/tickets/<id>/assign/ with another project's iteration is
    an HTTP 400, not a server-side integrity violation."""
    from django.urls import reverse

    api_project = make_project('REST proj')
    other = make_project('REST other')
    it = make_iteration(other, 1, date.today() - timedelta(days=3))
    ticket = services.create_ticket(api_project, name='Remote drifter')

    resp = api_client.post(
        reverse('ticket-assign', args=[ticket.pk]),
        {'iteration': it.pk},
        format='json',
    )
    assert resp.status_code == 400
    ticket.refresh_from_db()
    assert ticket.iteration is None


@pytest.mark.django_db
def test_transition_to_current_state_is_noop():
    """Transitioning a ticket to the state it already holds persists nothing
    new and emits no signal."""
    project = make_project('Noop')
    ticket = services.create_ticket(project, name='Idle')
    before = EventLog.objects.filter(name='ticket.state_changed').count()

    result = services.transition_ticket(ticket, Ticket.State.UNSTARTED)
    assert result.state == Ticket.State.UNSTARTED
    after = EventLog.objects.filter(name='ticket.state_changed').count()
    assert after == before


@pytest.mark.django_db
def test_transition_to_unknown_state_rejected():
    """A state outside the choices raises and does not move the ticket."""
    project = make_project('Unknown')
    ticket = services.create_ticket(project, name='Confused')
    with pytest.raises(ValidationError):
        services.transition_ticket(ticket, 'flying')
    ticket.refresh_from_db()
    assert ticket.state == Ticket.State.UNSTARTED


@pytest.mark.django_db
def test_assign_iteration_none_unassigns_and_audits():
    """Passing ``None`` pulls a ticket back out of its iteration and records
    the old->new=None change."""
    project = make_project('Unassign')
    it = make_iteration(project, 2, date.today() - timedelta(days=3))
    ticket = services.create_ticket(project, name='Scheduled', iteration=it)

    services.assign_iteration(ticket, None)
    ticket.refresh_from_db()
    assert ticket.iteration is None
    row = (
        EventLog.objects.filter(name='ticket.iteration_changed', ticket=ticket)
        .order_by('-id')
        .first()
    )
    assert row.payload['old'] == 2 and row.payload['new'] is None


@pytest.mark.django_db
def test_assign_iteration_same_iteration_is_noop():
    """Re-assigning a ticket to the iteration it already sits in is silent."""
    project = make_project('Silent')
    it = make_iteration(project, 5, date.today() - timedelta(days=3))
    ticket = services.create_ticket(project, name='Once', iteration=it)
    before = EventLog.objects.filter(name='ticket.iteration_changed').count()

    services.assign_iteration(ticket, it)
    after = EventLog.objects.filter(name='ticket.iteration_changed').count()
    assert after == before
