"""Ticket detail page + fusion chrome (favicon / splash).

New tests land here rather than in test_pages.py so the css+pages lane stays
append-only per AGENTS.md §3 rule 3.
"""

import pytest

from tokamak import services

from tests._utils import make_project


@pytest.mark.django_db
def test_ticket_detail_renders(client):
    """The ticket detail page shows name, description, chips and timeline."""
    project = make_project('Detail proj')
    ticket = services.create_ticket(
        project, name='Write some CSS', description='Dark theme everywhere.'
    )
    resp = client.get(f'/tickets/{ticket.pk}/')
    assert resp.status_code == 200
    body = resp.content.decode()
    assert 'Write some CSS' in body
    assert 'Dark theme everywhere.' in body
    assert 'Event timeline' in body


@pytest.mark.django_db
def test_ticket_detail_missing_is_404(client):
    """Unknown ticket ids 404 like any other detail view."""
    assert client.get('/tickets/9999/').status_code == 404


@pytest.mark.django_db
def test_board_links_to_ticket_pages(client):
    """Every board card carries a link to its ticket detail page."""
    project = make_project('Linked proj')
    ticket = services.create_ticket(project, name='Clickable')
    resp = client.get(f'/projects/{project.pk}/')
    assert resp.status_code == 200
    body = resp.content.decode()
    assert f'href="/tickets/{ticket.pk}/"' in body
    assert 'Clickable' in body


@pytest.mark.django_db
def test_ticket_detail_shows_state_events(client):
    """Transitions recorded by the service layer appear on the timeline."""
    project = make_project('Timeline proj')
    ticket = services.create_ticket(project, name='Moving')
    services.transition_ticket(ticket, 'started')
    body = client.get(f'/tickets/{ticket.pk}/').content.decode()
    assert 'ticket.state_changed' in body


@pytest.mark.django_db
def test_favicon_and_splash_in_base(client):
    """Every page ships the fusion favicon and splash overlay markup."""
    resp = client.get('/')
    body = resp.content.decode()
    assert 'img/favicon.svg' in body
    assert 'id="splash"' in body
    assert 'js/splash.js' in body


@pytest.mark.django_db
def test_ticket_detail_includes_memory_sessions(client):
    """Sessions attached to the ticket render with their monitor link."""
    project = make_project('Mem proj')
    ticket = services.create_ticket(project, name='Remember me')
    from tokamak.models import MemorySession

    MemorySession.objects.create(
        ticket=ticket,
        session_key='ws:ticket-1',
        mode='served',
        status='open',
        monitor_url='http://studio/ws/t1',
    )
    body = client.get(f'/tickets/{ticket.pk}/').content.decode()
    assert 'Memory sessions' in body
    assert 'http://studio/ws/t1' in body
