"""Drive the Unicorn board component methods directly, as a browser click would.

The ``board`` component is instantiated with a fresh request and set up so
each test gets an isolated instance bound to its own project. Signals fired
by the service layer must still appear in ``EventLog`` because components
never bypass it.
"""

import pytest
from django.test import RequestFactory

from tokamak import services
from tokamak.components.board import BoardView
from tokamak.models import EventLog, Ticket

from tests._utils import make_project


def _make_board(project):
    """Build a fresh ``BoardView`` set up against ``project``."""
    view = BoardView(component_name='board', component_id='test-board')
    view.setup(RequestFactory().get('/'))
    view.project_id = project.pk
    return view


@pytest.fixture
def board_project(db):
    """A project used for the Unicorn component tests."""
    return make_project('Board UI')


@pytest.mark.django_db
def test_add_ticket_and_stream(board_project):
    """Typing a name and clicking Add creates a ticket in ``unstarted``."""
    view = _make_board(board_project)
    view.new_ticket_name = 'Ticket from UI'
    view.add_ticket()
    assert view.error == ''
    rows = view.stream()['rows']
    assert len(rows) == 1
    assert rows[0]['ticket'].name == 'Ticket from UI'
    assert rows[0]['state'] == Ticket.State.UNSTARTED


@pytest.mark.django_db
def test_stream_orders_started_top_then_in_review(board_project):
    """The linear stream floats started work to the top, then in-review,
    then the backlog — no kanban columns."""
    view = _make_board(board_project)
    backlog = services.create_ticket(board_project, name='Backlog item')
    active = services.create_ticket(board_project, name='Active item')
    review = services.create_ticket(board_project, name='Review item')
    services.transition_ticket(active, Ticket.State.STARTED)
    services.transition_ticket(review, Ticket.State.STARTED)
    services.estimate_ticket(review, 3)
    services.transition_ticket(review, Ticket.State.FINISHED)

    order = [r['state'] for r in view.stream()['rows']]
    assert order == [
        Ticket.State.STARTED,
        Ticket.State.FINISHED,
        Ticket.State.UNSTARTED,
    ]


@pytest.mark.django_db
def test_sprint_line_splits_the_stream(board_project):
    """Without history the placeholder line (7 pts, dotted) cuts between
    the tickets that fit and what's coming next."""
    view = _make_board(board_project)
    for pts in (3, 3, 5):
        t = services.create_ticket(board_project, name=f'B{pts}')
        services.estimate_ticket(t, pts)

    stream = view.stream()
    assert stream['line']['points'] == 7.0
    assert stream['line']['dotted'] is True
    assert stream['line']['index'] == 2  # 3 + 3 fit; the 5 spills past
    flags = [r['above_line'] for r in stream['rows']]
    assert flags == [True, True, False]


@pytest.mark.django_db
def test_done_tickets_leave_board_for_archive(board_project):
    """Accepted and rejected tickets disappear from the stream but count
    toward the done archive."""
    view = _make_board(board_project)
    accepted = services.create_ticket(board_project, name='Delivered')
    services.estimate_ticket(accepted, 3)
    for st in ('started', 'finished', 'accepted'):
        services.transition_ticket(accepted, st)
    rejected = services.create_ticket(board_project, name='Spiked')
    services.estimate_ticket(rejected, 1)
    for st in ('started', 'finished', 'rejected'):
        services.transition_ticket(rejected, st)
    live = services.create_ticket(board_project, name='Still here')

    row_names = [r['ticket'].name for r in view.stream()['rows']]
    assert row_names == ['Still here']
    assert view.done_count() == 2
    assert live.name in row_names


@pytest.mark.django_db
def test_set_points_and_advance_fire_signals(board_project):
    """Inline point-setting and the advance button both persist state and
    emit signals."""
    view = _make_board(board_project)
    ticket = services.create_ticket(board_project, name='A')
    view.set_points(ticket.id, 5)
    ticket.refresh_from_db()
    assert ticket.story_points == 5

    view.advance(ticket.id)
    ticket.refresh_from_db()
    assert ticket.state == Ticket.State.STARTED
    assert EventLog.objects.filter(name='ticket.state_changed', ticket=ticket).exists()


@pytest.mark.django_db
def test_move_to_walks_transitions(board_project):
    """``move_to`` on an estimated feature walks every hop and emits a
    signal per hop."""
    view = _make_board(board_project)
    ticket = services.create_ticket(board_project, name='A')
    services.estimate_ticket(ticket, 3)
    view.move_to(ticket.id, 'accepted')
    ticket.refresh_from_db()
    assert ticket.state == Ticket.State.ACCEPTED
    assert view.error == ''
    hops = EventLog.objects.filter(name='ticket.state_changed', ticket=ticket).count()
    assert hops == 3


@pytest.mark.django_db
def test_move_to_stops_at_failing_hop_with_error(board_project):
    """Unestimated feature: walk stops at ``finished`` because accepting
    requires an estimate; the friendly error surfaces in the UI."""
    view = _make_board(board_project)
    ticket = services.create_ticket(board_project, name='A')  # no points
    view.move_to(ticket.id, 'accepted')
    ticket.refresh_from_db()
    assert ticket.state == Ticket.State.FINISHED
    assert 'Estimate a ticket before accepting' in view.error
