"""Blockers, epics and milestones — blocked-by edges plus scope groupings.

New module per AGENTS.md lane rules — service semantics, REST surface,
board component actions, page POSTs, admin registrations and the audit
signal contract for the blocks/blocked_by many-to-many relationship plus
the epic/milestone groupings. Semantics covered:

* blocked stories are flagged red on the board with reasons;
* a blocked story cannot be accepted until its blockers clear;
* accepting (or rejecting) the blocker resolves its edge automatically;
* cycles are impossible; no self-blocks; edges are project-local;
* epics/milestones group tickets and roll up progress.
"""

import pytest
from django.core.exceptions import ValidationError
from django.test import RequestFactory
from django.urls import reverse

from tokamak import services
from tokamak.components.board import BoardView
from tokamak.models import Blocker, Epic, EventLog, Milestone, Ticket

from tests._utils import make_project


def _make_board(project):
    """Build a fresh ``BoardView`` bound to ``project`` (mirrors test_components)."""
    view = BoardView(component_name='board', component_id='test-board')
    view.setup(RequestFactory().get('/'))
    view.project_id = project.pk
    return view


def _blocked_flag(project):
    """Map of ticket pk -> is_blocked for all tickets in a project."""
    return {t.pk: t.is_blocked for t in project.tickets.all()}


# ---------------------------------------------------------------------------
# Service semantics
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_add_blocker_creates_edge_and_flags_blocked():
    """A single edge marks the target blocked and not the source."""
    project = make_project('Blk1')
    a = services.create_ticket(project, name='A')
    b = services.create_ticket(project, name='B')
    edge = services.add_blocker(a, b, reason='waiting on API')

    assert Blocker.objects.filter(source=a, target=b).count() == 1
    assert edge.reason == 'waiting on API'
    flags = _blocked_flag(project)
    assert flags[b.pk] is True and flags[a.pk] is False
    # a.blockers means "tickets this one blocks" -- that's B.
    b.refresh_from_db()
    a.refresh_from_db()
    assert [t.pk for t in b.blocked_by] == [a.pk]
    assert [t.pk for t in a.blockers] == [b.pk]
    assert list(b.blocker_reasons) == ['waiting on API']


@pytest.mark.django_db
def test_blocker_signals_and_audit_log():
    """Add/remove fire blocker_added/blocker_removed mirrored into EventLog."""
    project = make_project('Blk2')
    a = services.create_ticket(project, name='A')
    b = services.create_ticket(project, name='B')
    services.add_blocker(a, b)
    assert EventLog.objects.filter(name='blocker.added').exists()

    services.remove_blocker(a, b)
    assert not Blocker.objects.filter(source=a, target=b).exists()
    removed = EventLog.objects.filter(name='blocker.removed')
    assert removed.exists()
    assert removed.first().payload['reason'] == 'manual'


@pytest.mark.django_db
def test_self_block_and_cross_project_rejected():
    """Self-blocks and cross-project edges raise ValidationError."""
    project = make_project('Blk3')
    p2 = make_project('Blk3b')
    a = services.create_ticket(project, name='A')
    b = services.create_ticket(project, name='B')
    c = services.create_ticket(p2, name='C')

    with pytest.raises(ValidationError):
        services.add_blocker(a, a)
    with pytest.raises(ValidationError):
        services.add_blocker(a, c)


@pytest.mark.django_db
def test_duplicate_and_cycle_prevention():
    """Duplicates refused; A->B plus B->C refuses C->A (would cycle)."""
    project = make_project('Blk4')
    a = services.create_ticket(project, name='A')
    b = services.create_ticket(project, name='B')
    c = services.create_ticket(project, name='C')
    services.add_blocker(a, b)
    services.add_blocker(b, c)

    with pytest.raises(ValidationError):
        services.add_blocker(a, b)  # duplicate
    with pytest.raises(ValidationError):
        services.add_blocker(c, a)  # cycle


@pytest.mark.django_db
def test_blocked_ticket_cannot_be_accepted():
    """The acceptance guard refuses a blocked ticket."""
    project = make_project('Blk5')
    a = services.create_ticket(project, name='A', story_points=3)
    b = services.create_ticket(project, name='B', story_points=2)
    services.add_blocker(a, b)
    # walk B to finished
    services.transition_ticket(b, Ticket.State.STARTED)
    services.transition_ticket(b, Ticket.State.FINISHED)
    with pytest.raises(ValidationError):
        services.transition_ticket(b, Ticket.State.ACCEPTED)


@pytest.mark.django_db
def test_accepting_blocker_clears_edge_and_unblocks():
    """Accepting the blocker auto-resolves its edges (reason 'accepted')."""
    project = make_project('Blk6')
    a = services.create_ticket(project, name='A', story_points=3)
    b = services.create_ticket(project, name='B', story_points=2)
    services.add_blocker(a, b)

    services.transition_ticket(a, Ticket.State.STARTED)
    services.transition_ticket(a, Ticket.State.FINISHED)
    services.transition_ticket(a, Ticket.State.ACCEPTED)

    assert not Blocker.objects.filter(source=a).exists()
    log = EventLog.objects.filter(name='blocker.removed')
    assert log.exists() and log.first().payload['reason'] == 'accepted'
    assert _blocked_flag(project)[b.pk] is False


@pytest.mark.django_db
def test_rejected_blocker_does_not_hold_target():
    """A rejected blocker stops counting; target becomes acceptable even
    though the edge row stays (archive semantics)."""
    project = make_project('Blk7')
    a = services.create_ticket(project, name='A', story_points=3)
    b = services.create_ticket(project, name='B', story_points=2)
    services.add_blocker(a, b)

    services.transition_ticket(a, Ticket.State.STARTED)
    services.transition_ticket(a, Ticket.State.FINISHED)
    services.transition_ticket(a, Ticket.State.REJECTED)

    assert Blocker.objects.filter(source=a, target=b).exists()
    assert _blocked_flag(project)[b.pk] is False
    services.transition_ticket(b, Ticket.State.STARTED)
    services.transition_ticket(b, Ticket.State.FINISHED)
    services.transition_ticket(b, Ticket.State.ACCEPTED)
    assert b.state == 'accepted'


@pytest.mark.django_db
def test_remove_blocker_is_idempotent():
    """Removing a non-existent edge returns None without raising."""
    project = make_project('Blk8')
    a = services.create_ticket(project, name='A')
    b = services.create_ticket(project, name='B')
    assert services.remove_blocker(a, b) is None


# ---------------------------------------------------------------------------
# Epics + milestones
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_epic_membership_and_rollup():
    """Tickets join an epic; rollup counts members/points per epic."""
    project = make_project('Epic1')
    epic = services.create_epic(project, 'Auth')
    a = services.create_ticket(project, name='A', story_points=3)
    b = services.create_ticket(project, name='B', story_points=5)
    services.ticket_set_epic(a, epic)
    services.ticket_set_epic(b, epic)

    roll = services.epic_rollup(project)
    assert len(roll) == 1 and roll[0]['total'] == 2
    assert roll[0]['points'] == 8

    services.ticket_set_epic(a, None)
    assert a.epic is None


@pytest.mark.django_db
def test_epic_audit_rows():
    """Membership changes are mirrored into EventLog."""
    project = make_project('Epic2')
    epic = services.create_epic(project, 'Auth')
    a = services.create_ticket(project, name='A')
    services.ticket_set_epic(a, epic)
    assert EventLog.objects.filter(name='ticket.epic_changed', ticket=a).exists()


@pytest.mark.django_db
def test_cross_project_epic_refused():
    """An epic from another project cannot hold a ticket."""
    p1 = make_project('EpicX')
    p2 = make_project('EpicY')
    epic = services.create_epic(p2, 'Other')
    a = services.create_ticket(p1, name='A')
    with pytest.raises(ValidationError):
        services.ticket_set_epic(a, epic)


@pytest.mark.django_db
def test_milestone_membership_and_progress():
    """Milestones derive progress from accepted members; complete when all
    attached tickets are accepted (and at least one exists)."""
    project = make_project('Ms1')
    ms = services.create_milestone(project, 'Beta')
    a = services.create_ticket(project, name='A', story_points=3)
    b = services.create_ticket(project, name='B', story_points=2)
    services.ticket_set_milestone(a, ms)
    services.ticket_set_milestone(b, ms)

    prog = services.milestone_progress(ms)
    assert prog['total'] == 2 and prog['accepted'] == 0
    assert not prog['complete']

    services.transition_ticket(a, Ticket.State.STARTED)
    services.transition_ticket(a, Ticket.State.FINISHED)
    services.transition_ticket(a, Ticket.State.ACCEPTED)
    prog = services.milestone_progress(ms)
    assert prog['accepted'] == 1 and prog['accepted_points'] == 3

    services.ticket_set_milestone(b, None)
    prog = services.milestone_progress(ms)
    assert prog['complete'] is True


@pytest.mark.django_db
def test_cross_project_milestone_refused():
    """A milestone from another project cannot hold a ticket."""
    p1 = make_project('MsX')
    p2 = make_project('MsY')
    ms = services.create_milestone(p2, 'Beta')
    a = services.create_ticket(p1, name='A')
    with pytest.raises(ValidationError):
        services.ticket_set_milestone(a, ms)


@pytest.mark.django_db
def test_epic_and_milestone_membership_coexist():
    """Epic and milestone fields are independent columns; both may be set."""
    project = make_project('EM1')
    epic = services.create_epic(project, 'Auth')
    ms = services.create_milestone(project, 'Beta')
    a = services.create_ticket(project, name='A')
    services.ticket_set_epic(a, epic)
    services.ticket_set_milestone(a, ms)
    assert (a.epic_id, a.milestone_id) == (epic.pk, ms.pk)


# ---------------------------------------------------------------------------
# REST surface
# ---------------------------------------------------------------------------
@pytest.fixture
def blk_project(db):
    return make_project('REST-blk')


@pytest.mark.django_db
def test_rest_blockers_round_trip(api_client, blk_project):
    """POST /tickets/<target>/block/ then GET blockers reflects the edge."""
    a = services.create_ticket(blk_project, name='A')
    b = services.create_ticket(blk_project, name='B')
    url = reverse('ticket-block', args=[b.pk])
    resp = api_client.post(url, {'by': a.pk, 'reason': 'waiting'}, format='json')
    assert resp.status_code == 201
    body = resp.json()
    assert body['source'] == a.pk and body['target'] == b.pk

    got = api_client.get(reverse('ticket-blockers', args=[b.pk])).json()
    assert got['is_blocked'] is True
    assert got['blocked_by'][0]['id'] == a.pk
    assert got['blocks'] == []


@pytest.mark.django_db
def test_rest_delete_block_resolves(api_client, blk_project):
    """DELETE /tickets/<target>/block/ removes the edge."""
    a = services.create_ticket(blk_project, name='A')
    b = services.create_ticket(blk_project, name='B')
    services.add_blocker(a, b)
    resp = api_client.delete(
        reverse('ticket-block', args=[b.pk]), {'by': a.pk}, format='json'
    )
    assert resp.status_code == 200
    assert not Blocker.objects.filter(source=a, target=b).exists()


@pytest.mark.django_db
def test_rest_project_blockers_view(api_client, blk_project):
    """GET /projects/<pk>/blockers/ lists live edges and blocked tickets."""
    a = services.create_ticket(blk_project, name='A')
    b = services.create_ticket(blk_project, name='B')
    services.add_blocker(a, b, reason='dep')
    resp = api_client.get(reverse('project-blockers', args=[blk_project.pk]))
    assert resp.status_code == 200
    body = resp.json()
    assert len(body['edges']) == 1
    assert body['blocked_tickets'][0]['id'] == b.pk


@pytest.mark.django_db
def test_rest_epics_and_milestones_crud(api_client, blk_project):
    """Epic + milestone CRUD with project filter."""
    resp = api_client.post(
        reverse('epic-list'),
        {'project': blk_project.pk, 'name': 'Auth'},
        format='json',
    )
    assert resp.status_code == 201
    epic_id = resp.json()['id']

    ms = api_client.post(
        reverse('milestone-list'),
        {'project': blk_project.pk, 'name': 'Beta'},
        format='json',
    )
    assert ms.status_code == 201

    filtered = api_client.get(reverse('epic-list'), {'project': blk_project.pk})
    names = [e['name'] for e in filtered.json()['results']]
    assert 'Auth' in names


@pytest.mark.django_db
def test_rest_blocker_viewset_create_and_list(api_client, blk_project):
    """/blockers/ create honors service validation; list filters by target."""
    a = services.create_ticket(blk_project, name='A')
    b = services.create_ticket(blk_project, name='B')
    resp = api_client.post(
        reverse('blocker-list'),
        {'source': a.pk, 'target': b.pk, 'reason': 'x'},
        format='json',
    )
    assert resp.status_code == 201
    listed = api_client.get(reverse('blocker-list'), {'target': b.pk}).json()['results']
    assert len(listed) == 1


@pytest.mark.django_db
def test_rest_cycle_returns_400(api_client, blk_project):
    """A cycle attempt through REST yields HTTP 400."""
    a = services.create_ticket(blk_project, name='A')
    b = services.create_ticket(blk_project, name='B')
    api_client.post(
        reverse('blocker-list'),
        {'source': a.pk, 'target': b.pk},
        format='json',
    )
    resp = api_client.post(
        reverse('blocker-list'),
        {'source': b.pk, 'target': a.pk},
        format='json',
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Board component + page POSTs
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_board_component_flags_blocked_rows():
    """The stream marks blocked tickets with reasons and picker options."""
    project = make_project('BoardBlk')
    a = services.create_ticket(project, name='A')
    b = services.create_ticket(project, name='B')
    services.add_blocker(a, b, reason='dep')

    view = _make_board(project)
    stream = view.stream()
    rows = {r['ticket'].pk: r for r in stream['rows']}
    assert rows[b.pk]['blocked'] is True
    assert rows[b.pk]['blocker_reasons'] == ['dep']
    assert [x['id'] for x in rows[b.pk]['blocking']] == [a.pk]
    assert rows[a.pk]['candidates']  # A can still be blocked by B

    grouping = view.grouping()
    assert grouping['epics'] == [] and grouping['milestones'] == []


@pytest.mark.django_db
def test_board_component_block_actions():
    """add_blocker/remove_blocker component actions go through services."""
    project = make_project('BoardBlk2')
    a = services.create_ticket(project, name='A')
    b = services.create_ticket(project, name='B')

    view = _make_board(project)
    view.add_blocker(b.pk, a.pk)
    assert Blocker.objects.filter(source=a, target=b).exists()
    assert view.error == ''

    view.remove_blocker(b.pk, a.pk)
    assert not Blocker.objects.filter(source=a, target=b).exists()


@pytest.mark.django_db
def test_board_component_error_surfaces_cycle():
    """A cycle attempt from the board sets the friendly error string."""
    project = make_project('BoardBlk3')
    a = services.create_ticket(project, name='A')
    b = services.create_ticket(project, name='B')
    view = _make_board(project)
    view.add_blocker(b.pk, a.pk)
    view.add_blocker(a.pk, b.pk)  # would cycle
    assert 'cycle' in view.error.lower()


@pytest.mark.django_db
def test_ticket_page_post_actions(client):
    """The detail page POST flow adds blockers and sets epic/milestone."""
    from django.test import Client

    client = Client()
    project = make_project('PageBlk')
    a = services.create_ticket(project, name='A')
    b = services.create_ticket(project, name='B')

    resp = client.post(
        f'/tickets/{b.pk}/',
        {'action': 'add_blocker', 'by': str(a.pk), 'reason': 'dep'},
    )
    assert resp.status_code == 302 or resp.status_code == 200
    assert Blocker.objects.filter(source=a, target=b).exists()

    resp = client.post(
        f'/tickets/{b.pk}/',
        {'action': 'set_epic', 'epic': 'Auth'},
    )
    b.refresh_from_db()
    assert b.epic is not None and b.epic.name == 'Auth'

    resp = client.post(
        f'/tickets/{b.pk}/',
        {'action': 'set_milestone', 'milestone': 'Beta'},
    )
    b.refresh_from_db()
    assert b.milestone is not None and b.milestone.name == 'Beta'

    # Detach epic (blank value)
    resp = client.post(
        f'/tickets/{b.pk}/',
        {'action': 'set_epic', 'epic': ''},
    )
    b.refresh_from_db()
    assert b.epic is None


@pytest.mark.django_db
def test_ticket_page_blocks_acceptance_flow(client):
    """Blocked ticket page shows the flag context after adding a blocker."""
    from django.test import Client

    client = Client()
    project = make_project('PageBlk2')
    a = services.create_ticket(project, name='A', story_points=3)
    b = services.create_ticket(project, name='B', story_points=2)
    services.add_blocker(a, b, reason='dep')

    resp = client.get(f'/tickets/{b.pk}/')
    assert resp.status_code == 200
    assert 'blocked' in resp.content.decode().lower()


# ---------------------------------------------------------------------------
# Admin smoke
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_admin_registrations_exist():
    """The three new models are registered with the admin site."""
    from django.contrib import admin as admin_module

    from tokamak.models import Blocker, Epic, Milestone

    assert admin_module.site.is_registered(Epic)
    assert admin_module.site.is_registered(Milestone)
    assert admin_module.site.is_registered(Blocker)
