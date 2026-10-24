"""Drag-reorder within a display group: service, board action and REST.

New module per AGENTS.md §3 rule 3 (no edits to existing test files).
Covers the pluck-and-reinsert semantics, position compaction, group
boundaries (a reorder never changes workflow state), audit rows, and the
error paths (archived tickets, negative/non-integer targets, unknown
groups) through both the component action and /api/reorder/.
"""

import pytest
from django.core.exceptions import ValidationError

from tokamak import services
from tokamak.components.board import BoardView
from tokamak.models import EventLog, Ticket
from tokamak.reorder import group_for_state, reorder_ticket

from tests._utils import make_project


def _positions(project):
    """Map of ticket pk -> stored position."""
    return {t.pk: t.position for t in project.tickets.all()}


def _order(project, states):
    """Pks of the tickets in the given state group, ordered as the board does."""
    return [
        t.pk
        for t in project.tickets.filter(state__in=states).order_by(
            'position', 'pk', 'created_at'
        )
    ]


@pytest.mark.django_db
def test_reorder_moves_within_backlog_group():
    """Plucking the first backlog ticket and dropping it at index 2 rotates
    the group; positions compact to 0..n-1."""
    project = make_project('OrderA')
    a = services.create_ticket(project, name='A')
    b = services.create_ticket(project, name='B')
    c = services.create_ticket(project, name='C')
    d = services.create_ticket(project, name='D')
    assert _order(project, ('unstarted',)) == [a.pk, b.pk, c.pk, d.pk]

    reorder_ticket(a, 2)
    assert _order(project, ('unstarted',)) == [b.pk, c.pk, a.pk, d.pk]
    assert _positions(project) == {a.pk: 2, b.pk: 0, c.pk: 1, d.pk: 3}


@pytest.mark.django_db
def test_reorder_to_same_index_is_stable():
    """Dropping a ticket at its own index is a no-op for the group order."""
    project = make_project('OrderB')
    a = services.create_ticket(project, name='A')
    b = services.create_ticket(project, name='B')
    c = services.create_ticket(project, name='C')

    reorder_ticket(b, 1)
    assert _order(project, ('unstarted',)) == [a.pk, b.pk, c.pk]


@pytest.mark.django_db
def test_reorder_beyond_end_clamps_to_last():
    """A target index past the end of the group lands the ticket last."""
    project = make_project('OrderC')
    a = services.create_ticket(project, name='A')
    b = services.create_ticket(project, name='B')
    c = services.create_ticket(project, name='C')

    reorder_ticket(a, 99)
    assert _order(project, ('unstarted',)) == [b.pk, c.pk, a.pk]


@pytest.mark.django_db
def test_reorder_audits_into_event_log():
    """Every persisted move becomes exactly one ``ticket.reordered`` row."""
    project = make_project('Audit')
    a = services.create_ticket(project, name='A')
    b = services.create_ticket(project, name='B')

    reorder_ticket(a, 1)
    rows = EventLog.objects.filter(name='ticket.reordered', ticket=a)
    assert rows.count() == 1
    payload = rows.first().payload
    assert payload['group'] == 'backlog'
    assert payload['to'] == 1
    assert payload['order'] == [b.pk, a.pk]


@pytest.mark.django_db
def test_reorder_never_touches_state():
    """A reorder move is orthogonal to workflow state: the ticket keeps its
    state and no state-change events fire."""
    project = make_project('StateSafe')
    a = services.create_ticket(project, name='A')
    b = services.create_ticket(project, name='B')
    services.transition_ticket(b, Ticket.State.STARTED)

    reorder_ticket(a, 0)
    a.refresh_from_db()
    assert a.state == Ticket.State.UNSTARTED
    # no state-change event was ever recorded *for a* (b's own transition
    # to started legitimately fired one earlier)
    assert not EventLog.objects.filter(name='ticket.state_changed', ticket=a).exists()


@pytest.mark.django_db
def test_reorder_scoped_to_own_group_only():
    """Reordering inside the active group leaves backlog positions alone."""
    project = make_project('Scoped')
    act1 = services.create_ticket(project, name='Act1')
    services.transition_ticket(act1, Ticket.State.STARTED)
    act2 = services.create_ticket(project, name='Act2')
    services.transition_ticket(act2, Ticket.State.STARTED)
    back1 = services.create_ticket(project, name='Back1')

    reorder_ticket(act1, 1)
    assert _order(project, ('started',)) == [act2.pk, act1.pk]
    # backlog ticket untouched: create-time position kept (groups are
    # independent; the compaction loop only rewrites members of the
    # active group)
    back1.refresh_from_db()
    before = back1.position
    reorder_ticket(act2, 0)
    back1.refresh_from_db()
    assert back1.position == before


@pytest.mark.django_db
def test_archived_tickets_cannot_reorder():
    """Accepted/rejected tickets are off-board; reorder refuses cleanly."""
    project = make_project('Archived')
    t = services.create_ticket(
        project,
        name='Done',
    )
    services.estimate_ticket(t, 3)
    for st in ('started', 'finished', 'accepted'):
        services.transition_ticket(t, st)

    with pytest.raises(ValidationError):
        reorder_ticket(t, 1)
    assert group_for_state(Ticket.State.ACCEPTED) is None


@pytest.mark.django_db
def test_negative_and_bogus_targets_rejected():
    """Negative indices and non-integer targets raise ValidationError."""
    project = make_project('Bogus')
    a = services.create_ticket(project, name='A')
    b = services.create_ticket(project, name='B')

    with pytest.raises(ValidationError):
        reorder_ticket(a, -3)
    with pytest.raises(ValidationError):
        reorder_ticket(a, 'second')


@pytest.mark.django_db
def test_explicit_wrong_group_rejected():
    """Naming a group the ticket does not belong to is refused."""
    project = make_project('WrongGroup')
    a = services.create_ticket(project, name='A')
    with pytest.raises(ValidationError):
        reorder_ticket(a, 1, group='active')


@pytest.mark.django_db
def test_board_component_reorder_action():
    """The Unicorn action persists order and clears any prior error."""
    from django.test import RequestFactory

    project = make_project('Component')
    view = BoardView(component_name='board', component_id='t-reorder')
    view.setup(RequestFactory().get('/'))
    view.project_id = project.pk

    a = services.create_ticket(project, name='A')
    b = services.create_ticket(project, name='B')
    c = services.create_ticket(project, name='C')

    view.reorder(a.id, 2)
    assert view.error == ''
    assert _order(project, ('unstarted',)) == [b.pk, c.pk, a.pk]

    view.reorder(a.id, 'not-an-int')
    assert view.error != ''


@pytest.mark.django_db
def test_stream_reflects_manual_order(board_project=None):
    """``stream()`` rows honour manual positions inside each state group."""
    from django.test import RequestFactory

    project = make_project('Stream')
    view = BoardView(component_name='board', component_id='t-stream')
    view.setup(RequestFactory().get('/'))
    view.project_id = project.pk

    a = services.create_ticket(project, name='A')
    b = services.create_ticket(project, name='B')
    reorder_ticket(b, 0)
    rows = [r['ticket'].pk for r in view.stream()['rows']]
    assert rows == [b.pk, a.pk]


@pytest.mark.django_db
def test_rest_reorder_endpoint(api_client):
    """POST /api/reorder/ persists the move and returns the new group order."""
    project = make_project('RestA')
    a = services.create_ticket(project, name='A')
    b = services.create_ticket(project, name='B')

    resp = api_client.post(
        '/api/reorder/',
        {'ticket': a.pk, 'position': 1},
        format='json',
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data['ok'] is True
    assert data['group'] == 'backlog'
    assert data['order'] == [b.pk, a.pk]


@pytest.mark.django_db
def test_rest_reorder_errors(api_client):
    """Unknown ticket / missing position answer 400; archived tickets too."""
    project = make_project('RestB')
    t = services.create_ticket(project, name='Live')

    resp = api_client.post(
        '/api/reorder/', {'ticket': 999999, 'position': 1}, format='json'
    )
    assert resp.status_code == 400

    resp = api_client.post('/api/reorder/', {'ticket': t.pk}, format='json')
    assert resp.status_code == 400

    services.estimate_ticket(t, 1)
    for st in ('started', 'finished', 'accepted'):
        services.transition_ticket(t, st)
    resp = api_client.post(
        '/api/reorder/', {'ticket': t.pk, 'position': 0}, format='json'
    )
    assert resp.status_code == 400
