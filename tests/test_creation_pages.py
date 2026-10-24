"""Dedicated creation pages for projects and tickets (md1 epic).

Plain request/response pages: markdown-first forms, chore-aware validation,
every write through the service layer. Editor chrome (toolbar/preview) is
asserted here too since it ships with these pages.
"""

import pytest

from tokamak import services
from tokamak.models import Project, Ticket
from tests._utils import make_project


def _signal_spy(request, signal):
    """Attach a recording receiver to ``signal`` for one test."""
    calls = []

    def spy(sender, **kwargs):
        calls.append(kwargs)

    signal.connect(spy, dispatch_uid='spy', weak=False)
    request.addfinalizer(lambda: signal.disconnect(dispatch_uid='spy'))
    return calls


# ---------------------------------------------------------------------------
# Project creation page
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_project_create_page_renders(client):
    resp = client.get('/projects/new/')
    assert resp.status_code == 200
    body = resp.content.decode()
    assert 'New project' in body
    assert 'data-md-editor' in body  # markdown editor textarea


@pytest.mark.django_db
def test_project_create_posts_through_service(client, request):
    from tokamak.signals import project_updated

    calls = _signal_spy(request, project_updated)
    resp = client.post(
        '/projects/new/',
        {'name': 'Markdown demo', 'description': '# Why\nBecause *reasons*.'},
    )
    assert resp.status_code == 302
    from tokamak.models import Project

    project = Project.objects.get(name='Markdown demo')
    assert resp.headers['location'] == f'/projects/{project.pk}/'
    assert len(calls) == 1


@pytest.mark.django_db
def test_project_create_requires_name(client):
    before = Project.objects.count()
    resp = client.post('/projects/new/', {'name': '   ', 'description': ''})
    assert resp.status_code == 200  # re-rendered with errors
    assert Project.objects.count() == before


# ---------------------------------------------------------------------------
# Ticket creation page
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_ticket_create_page_renders(client):
    project = make_project('Ticket form proj')
    resp = client.get(f'/projects/{project.pk}/tickets/new/')
    assert resp.status_code == 200
    body = resp.content.decode()
    assert 'New ticket' in body
    assert 'data-md-editor' in body
    assert 'Story points' in body


@pytest.mark.django_db
def test_ticket_create_initial_type_from_query(client):
    project = make_project('Query proj')
    resp = client.get(f'/projects/{project.pk}/tickets/new/', {'type': 'chore'})
    assert resp.status_code == 200
    assert 'value="chore" selected' in resp.content.decode()


@pytest.mark.django_db
def test_ticket_create_feature_with_points(client, request):
    from tokamak.signals import ticket_created

    project = make_project('Create proj')
    calls = _signal_spy(request, ticket_created)
    resp = client.post(
        f'/projects/{project.pk}/tickets/new/',
        {
            'name': 'Rich text tickets',
            'type': 'feature',
            'story_points': '8',
            'description': '- task one\n- [x] done bit\n',
        },
    )
    assert resp.status_code == 302
    ticket = Ticket.objects.get(name='Rich text tickets')
    assert ticket.type == Ticket.Type.FEATURE
    assert ticket.story_points == 8
    assert resp.headers['location'] == f'/tickets/{ticket.pk}/'
    assert len(calls) == 1


@pytest.mark.django_db
def test_ticket_create_chore_is_description_only(client):
    project = make_project('Chore proj')
    resp = client.post(
        f'/projects/{project.pk}/tickets/new/',
        {
            'type': 'chore',
            'name': '',
            'story_points': '5',
            'description': '**Upgrade** deps to the newest `django`.',
        },
    )
    assert resp.status_code == 302
    ticket = Ticket.objects.filter(project=project).first()
    assert ticket.type == Ticket.Type.CHORE
    # title derived from markdown-stripped description; points ignored
    assert ticket.name.startswith('Upgrade deps')
    assert ticket.story_points in (None, 1)  # automation seeds chores at 1


@pytest.mark.django_db
def test_ticket_create_chore_needs_description(client):
    project = make_project('Empty chore proj')
    resp = client.post(
        f'/projects/{project.pk}/tickets/new/',
        {'type': 'chore', 'name': '', 'story_points': '', 'description': ''},
    )
    assert resp.status_code == 200
    assert 'A chore needs a description' in resp.content.decode()
    assert Ticket.objects.filter(project=project).count() == 0


@pytest.mark.django_db
def test_ticket_create_feature_needs_name(client):
    project = make_project('Nameless proj')
    resp = client.post(
        f'/projects/{project.pk}/tickets/new/',
        {'type': 'feature', 'name': '', 'story_points': '3', 'description': 'desc'},
    )
    assert resp.status_code == 200
    assert 'Ticket name is required' in resp.content.decode()


@pytest.mark.django_db
def test_ticket_create_rejects_non_fibonacci(client):
    project = make_project('Fib proj')
    resp = client.post(
        f'/projects/{project.pk}/tickets/new/',
        {'type': 'bug', 'name': 'Boom', 'story_points': '4', 'description': ''},
    )
    assert resp.status_code == 200  # ChoiceField rejects 4 (not offered)
    assert Ticket.objects.filter(project=project).count() == 0


@pytest.mark.django_db
def test_ticket_create_missing_project_404(client):
    resp = client.post('/projects/9999/tickets/new/', {'type': 'feature'})
    assert resp.status_code == 404
