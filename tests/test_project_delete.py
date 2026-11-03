"""Project-level deletion audit (project-delete-audit).

``services.delete_project`` replaces the bare cascade: blocker edges clear
first with audit, ``project_deleted`` fires while the instance is readable so
the audit receiver can snapshot counts into the payload, and — because
``EventLog.project`` is SET_NULL — every surviving row keeps its payload with
a null project link. The history outlives even its top-level subject.
"""

from datetime import date

import pytest

from tokamak import services
from tokamak.models import Blocker, Epic, EventLog, Milestone, Project
from tokamak.signals import project_deleted

from tests._utils import make_iteration, make_project


def _populate(project):
    """Two tickets in a sprint, an epic, a milestone and one blocker edge."""
    it = make_iteration(project, 1, date(2026, 1, 5))
    a = services.create_ticket(project, name='Blocker', iteration=it)
    b = services.create_ticket(project, name='Blocked', iteration=it)
    epic = Epic.objects.create(project=project, name='Umbrella')
    milestone = Milestone.objects.create(
        project=project, name='v1', due_date=date(2026, 6, 1)
    )
    services.ticket_set_epic(b, epic)
    services.ticket_set_milestone(a, milestone)
    edge = services.add_blocker(a, b, reason='waiting')
    return a, b, it, epic, milestone, edge


@pytest.mark.django_db
def test_delete_project_snapshots_counts_in_payload(db):
    """The audit row records exactly what cascaded away."""
    project = make_project('Doomed')
    _populate(project)

    seen = []

    def recv(sender, project, user=None, **kwargs):
        seen.append((project.pk, project.name, project.tickets.count()))

    project_deleted.connect(recv, dispatch_uid='test_proj_del_signal')
    try:
        pk = services.delete_project(project)
    finally:
        project_deleted.disconnect(recv, dispatch_uid='test_proj_del_signal')

    # receivers see the live instance — counts readable before the cascade
    assert seen == [(pk, 'Doomed', 2)]
    assert not Project.objects.filter(pk=pk).exists()

    row = EventLog.objects.filter(name='project.deleted').first()
    assert row.project is None  # SET_NULL: the link dies, the row survives
    payload = row.payload
    assert payload['name'] == 'Doomed'
    assert payload['tickets'] == 2
    assert payload['iterations'] == 1
    assert payload['epics'] == 1
    assert payload['milestones'] == 1
    assert payload['blocker_edges'] == 1


@pytest.mark.django_db
def test_project_history_survives_deletion(db):
    """Rows written while the project lived persist after deletion."""
    project = make_project('Ghost')
    _populate(project)
    before = EventLog.objects.filter(project=project).count()
    assert before >= 5

    services.delete_project(project)

    surviving = EventLog.objects.filter(name='ticket.created')
    assert surviving.count() == 2
    assert all(row.project is None for row in surviving)


@pytest.mark.django_db
def test_edges_cleared_with_deleted_reason_neighbours_intact(db):
    """The doomed project's edges go with audit; a neighbour keeps its own."""
    doomed = make_project('Edges')
    da = services.create_ticket(doomed, name='DA')
    db_ = services.create_ticket(doomed, name='DB')
    services.add_blocker(da, db_, reason='waiting')

    neighbour = make_project('Neighbour')
    na = services.create_ticket(neighbour, name='NA')
    nb = services.create_ticket(neighbour, name='NB')
    services.add_blocker(na, nb)

    services.delete_project(doomed)

    assert Blocker.objects.filter(source__project=neighbour).count() == 1
    removed = EventLog.objects.filter(name='blocker.removed')
    reasons = [r.payload['reason'] for r in removed]
    assert reasons == ['deleted']  # only the doomed edge, audited once


@pytest.mark.django_db
def test_rest_delete_endpoint_uses_service(api_client):
    """DELETE /api/projects/<pk>/ returns 204 and writes the audit row."""
    project = make_project('REST del')
    services.create_ticket(project, name='By REST')
    resp = api_client.delete(f'/api/projects/{project.pk}/')
    assert resp.status_code == 204
    assert not Project.objects.filter(pk=project.pk).exists()
    row = EventLog.objects.filter(name='project.deleted').first()
    assert row.payload['name'] == 'REST del'
    assert row.payload['tickets'] == 1


@pytest.mark.django_db
def test_deleting_project_twice_is_not_found(api_client, db):
    """Second DELETE is a clean 404, never a silent success."""
    project = make_project('Twice')
    resp = api_client.delete(f'/api/projects/{project.pk}/')
    assert resp.status_code == 204
    resp = api_client.delete(f'/api/projects/{project.pk}/')
    assert resp.status_code == 404
