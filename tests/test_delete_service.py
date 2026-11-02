"""Delete service layer (delete-ticket-service).

``services.delete_ticket`` replaces the board's direct ORM delete: blocker
edges involving the ticket clear first (audited as ``blocker_removed`` with
reason ``deleted``), then ``ticket_deleted`` fires so the audit row lands,
then the row goes. EventLog rows referencing a deleted ticket survive via
SET_NULL — history outlives the history's subject.
"""

import pytest
from django.conf import settings
from django.test import RequestFactory
from django.test.utils import override_settings

from tokamak import services, webhooks
from tokamak.components.board import BoardView
from tokamak.models import Blocker, EventLog, Ticket
from tokamak.signals import ticket_deleted

from tests._utils import make_project


def _board(project):
    view = BoardView(component_name='board', component_id='test-board')
    view.setup(RequestFactory().get('/'))
    view.project_id = project.pk
    return view


@pytest.mark.django_db
def test_delete_ticket_fires_signal_and_audit(db):
    """The signal sees the live instance; the audit row outlives it."""
    project = make_project('Del')
    ticket = services.create_ticket(project, name='Bye', story_points=3)

    seen = []

    def recv(sender, ticket, user=None, **kwargs):
        seen.append((ticket.pk, ticket.name))

    live_pk = ticket.pk
    ticket_deleted.connect(recv, dispatch_uid='test_delete_signal')
    try:
        pk = services.delete_ticket(ticket)
    finally:
        ticket_deleted.disconnect(recv, dispatch_uid='test_delete_signal')

    assert seen == [(live_pk, 'Bye')]  # receivers see the live instance
    assert not Ticket.objects.filter(pk=pk).exists()
    rows = list(EventLog.objects.filter(name='ticket.deleted'))
    assert len(rows) == 1
    row = rows[0]
    assert row.ticket is None  # SET_NULL: the link dies, the row survives
    assert row.project == project
    assert row.payload['name'] == 'Bye'
    assert row.payload['points'] == 3


@pytest.mark.django_db
def test_prior_history_survives_ticket_deletion(db):
    """Audit rows written while the ticket lived persist after deletion."""
    project = make_project('Ghost')
    ticket = services.create_ticket(project, name='Haunted')
    services.estimate_ticket(ticket, 2)
    services.transition_ticket(ticket, 'started')
    before = EventLog.objects.filter(ticket=ticket).count()
    assert before >= 3  # created, estimated, state_changed

    services.delete_ticket(ticket)

    surviving = EventLog.objects.filter(project=project).exclude(name='ticket.deleted')
    assert surviving.count() == before
    assert all(row.ticket is None for row in surviving)


@pytest.mark.django_db
def test_blocker_edges_cleared_with_deleted_reason(db):
    """Edges on both sides of the deleted ticket go away, each audited."""
    project = make_project('Edges')
    a = services.create_ticket(project, name='A')
    b = services.create_ticket(project, name='B')
    c = services.create_ticket(project, name='C')
    services.add_blocker(a, b, reason='waiting on A')
    services.add_blocker(b, c, reason='waiting on B')

    services.delete_ticket(b)  # middle of the chain disappears

    assert Blocker.objects.count() == 0
    removed = EventLog.objects.filter(name='blocker.removed')
    assert sorted(r.payload['reason'] for r in removed) == ['deleted', 'deleted']
    assert not Ticket.objects.get(pk=a.pk).is_blocked
    assert not c.is_blocked  # incoming edge (b -> c) gone with b


@pytest.mark.django_db
def test_board_component_delete_goes_through_service(db):
    """The Unicorn delete button now leaves audit behind."""
    project = make_project('Board del')
    ticket = services.create_ticket(project, name='ClickDelete')
    view = _board(project)

    view.delete_ticket(str(ticket.pk))
    assert not Ticket.objects.filter(pk=ticket.pk).exists()
    assert EventLog.objects.filter(name='ticket.deleted').count() == 1
    assert view.error == ''


@pytest.mark.django_db
def test_board_component_missing_id_sets_error(db):
    """Deleting something that never existed is a clean error, not a crash."""
    project = make_project('Board del miss')
    view = _board(project)
    view.delete_ticket(42_424)
    assert view.error == 'No such ticket.'


@pytest.mark.django_db
def test_rest_delete_endpoint_uses_service(api_client):
    """DELETE /api/tickets/<pk>/ returns 204 and writes the audit row."""
    project = make_project('REST del')
    ticket = services.create_ticket(project, name='Via REST')
    resp = api_client.delete(f'/api/tickets/{ticket.pk}/')
    assert resp.status_code == 204
    assert not Ticket.objects.filter(pk=ticket.pk).exists()
    rows = EventLog.objects.filter(name='ticket.deleted')
    assert rows.count() == 1
    assert rows[0].payload['name'] == 'Via REST'


@pytest.mark.django_db
def test_fanout_covers_ticket_deleted(db, monkeypatch):
    """With fan-out enabled and a delivery mocked, the deleted event mirrors."""
    webhooks.reset_ledger()
    captured = []

    def fake_deliver(cfg, target, event):
        captured.append(event)
        return {'url': target['url'], 'status': 'delivered'}

    monkeypatch.setattr(webhooks, '_deliver', fake_deliver)

    tokamak_cfg = dict(settings.TOKAMAK)
    tokamak_cfg['WEBHOOKS'] = {
        'enabled': True,
        'targets': [{'url': 'http://example.invalid/hook'}],
    }
    with override_settings(TOKAMAK=tokamak_cfg):
        project = make_project('Fan del')
        ticket = services.create_ticket(project, name='Mirrored bye')
        services.delete_ticket(ticket)
        row = EventLog.objects.filter(name='ticket.deleted').first()
        results = webhooks.fanout_for_event(row)

    assert [e.name for e in captured] == ['ticket.deleted']
    assert results[0]['status'] == 'delivered'
    assert 'ticket_deleted' in webhooks.SIGNAL_EVENT_NAMES
    webhooks.reset_ledger()
