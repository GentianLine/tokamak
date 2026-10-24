"""REST API surface: viewsets, action endpoints, discovery, filters.

Every endpoint exercised here mirrors a UI or service-layer action; the
signals fired by the service layer must therefore appear in ``EventLog``
exactly as they do for direct calls.
"""

import pytest
from django.urls import reverse

from tokamak import services
from tokamak.models import EventLog, Ticket

from tests._utils import make_project


def _body(resp):
    """Decode a DRF JSON response."""
    return resp.json()


@pytest.fixture
def api_project(db):
    """A project created for the REST suite."""
    return make_project('API proj')


@pytest.mark.django_db
def test_create_ticket_via_api_fires_signals(api_client, api_project):
    """``POST /api/projects/<id>/tickets/`` creates a ticket and emits
    ``ticket.created`` into the audit log."""
    url = reverse('project-ticket-list', args=[api_project.pk])
    resp = api_client.post(url, {'name': 'From API', 'type': 'feature'}, format='json')
    assert resp.status_code == 201
    ticket_id = _body(resp)['id']
    assert EventLog.objects.filter(name='ticket.created', ticket_id=ticket_id).exists()


@pytest.mark.django_db
def test_transition_and_estimate_actions(api_client, api_project):
    """``transition`` and ``estimate`` actions accept legal payloads and
    reject non-Fibonacci points with HTTP 400."""
    ticket = services.create_ticket(api_project, name='A')
    resp = api_client.post(
        reverse('ticket-transition', args=[ticket.pk]),
        {'state': 'started'},
        format='json',
    )
    assert resp.status_code == 200
    assert _body(resp)['state'] == 'started'

    bad = api_client.post(
        reverse('ticket-estimate', args=[ticket.pk]), {'points': 4}, format='json'
    )
    assert bad.status_code == 400  # not Fibonacci
    ok = api_client.post(
        reverse('ticket-estimate', args=[ticket.pk]), {'points': 8}, format='json'
    )
    assert ok.status_code == 200


@pytest.mark.django_db
def test_illegal_transition_returns_400(api_client, api_project):
    """Skipping straight to ``accepted`` returns HTTP 400 with a message."""
    ticket = services.create_ticket(api_project, name='A')
    resp = api_client.post(
        reverse('ticket-transition', args=[ticket.pk]),
        {'state': 'accepted'},
        format='json',
    )
    assert resp.status_code == 400


@pytest.mark.django_db
def test_transition_choices_discovery(api_client, api_project):
    """``GET /api/tickets/<id>/transition/`` advertises the allowed next states."""
    ticket = services.create_ticket(api_project, name='A')
    resp = api_client.get(reverse('ticket-transition', args=[ticket.pk]))
    assert resp.status_code == 200
    assert _body(resp)['allowed'] == ['started']


@pytest.mark.django_db
def test_board_and_cutoff_endpoints(api_client, api_project):
    """The board and cutoff endpoints return columns plus forecast payload."""
    t = services.create_ticket(api_project, name='A')
    services.estimate_ticket(t, 3)
    resp = api_client.get(reverse('project-board', args=[api_project.pk]))
    assert resp.status_code == 200
    data = _body(resp)
    assert len(data['columns']['unstarted']) == 1
    assert data['forecast']['remaining_points'] == 3
    cutoff = api_client.get(reverse('project-cutoff', args=[api_project.pk]))
    assert 'estimated_cutoff' in _body(cutoff)


@pytest.mark.django_db
def test_events_endpoint_filters(api_client, api_project):
    """``GET /api/events/?project=..&name=..`` narrows to matching rows."""
    ticket = services.create_ticket(api_project, name='A')
    services.transition_ticket(ticket, Ticket.State.STARTED)
    resp = api_client.get(
        '/api/events/?project=%s&name=ticket.state_changed' % api_project.pk
    )
    rows = _body(resp)['results']
    assert len(rows) == 1
    assert rows[0]['payload']['new'] == 'started'
