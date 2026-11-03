"""Amendment flows for descriptions (md-edit-pages).

Ticket descriptions gain a service function + ``ticket_updated`` signal and
an inline editor on the ticket detail page; project descriptions get a POST
route through the existing ``save_project`` with an editor on the board page.
"""

import pytest

from tokamak import services
from tokamak.markdown import MAX_INPUT_CHARS
from tokamak.models import EventLog
from tokamak.signals import ticket_updated

from tests._utils import make_project


@pytest.mark.django_db
def test_update_ticket_description_service(db):
    """Service write: field saved, signal payload + audit row land."""
    project = make_project('Ament')
    ticket = services.create_ticket(project, name='Prose', description='v1')

    seen = []

    def recv(sender, ticket, field, old_value, new_value, user=None, **kwargs):
        seen.append((field, old_value, new_value))

    ticket_updated.connect(recv, dispatch_uid='test_upd_signal')
    try:
        services.update_ticket_description(ticket, 'v2 with **markdown**')
    finally:
        ticket_updated.disconnect(recv, dispatch_uid='test_upd_signal')

    assert seen == [('description', 'v1', 'v2 with **markdown**')]
    ticket.refresh_from_db()
    assert ticket.description == 'v2 with **markdown**'
    row = EventLog.objects.filter(name='ticket.updated').first()
    assert row.payload['field'] == 'description'
    assert row.payload['old'] == 'v1'
    assert row.payload['new'].startswith('v2')


@pytest.mark.django_db
def test_update_rejects_oversized_prose(db):
    """Longer than the renderer guard is refused, not silently truncated."""
    from django.core.exceptions import ValidationError

    project = make_project('Big prose')
    ticket = services.create_ticket(project, name='T', description='small')
    with pytest.raises(ValidationError):
        services.update_ticket_description(ticket, 'x' * (MAX_INPUT_CHARS + 1))
    ticket.refresh_from_db()
    assert ticket.description == 'small'


@pytest.mark.django_db
def test_detail_page_shows_amend_editor(client):
    """The ticket detail page carries the widget-bound textarea prefilled."""
    project = make_project('Detail edit')
    ticket = services.create_ticket(
        project, name='Editable', description='raw text here'
    )
    content = client.get(f'/tickets/{ticket.pk}/').content.decode()
    assert 'data-md-editor' in content
    assert 'action" value="update_description"' in content or (
        'name="description"' in content and 'raw text here' in content
    )


@pytest.mark.django_db
def test_detail_post_update_flow(client):
    """POSTing the amendment form updates through services + audits."""
    project = make_project('Detail post')
    ticket = services.create_ticket(project, name='Amended', description='old words')
    resp = client.post(
        f'/tickets/{ticket.pk}/',
        {'action': 'update_description', 'description': '# new words'},
    )
    assert resp.status_code == 302
    assert resp.headers['location'] == f'/tickets/{ticket.pk}/'
    ticket.refresh_from_db()
    assert ticket.description == '# new words'
    rows = EventLog.objects.filter(name='ticket.updated')
    assert rows.count() == 1


@pytest.mark.django_db
def test_description_route_rejects_get(client):
    """The amendment POST route is not a browsing surface."""
    project = make_project('Route')
    resp = client.get(f'/projects/{project.pk}/description/')
    assert resp.status_code == 302
    assert resp.headers['location'] == f'/projects/{project.pk}/'


@pytest.mark.django_db
def test_board_page_shows_project_amend(client):
    """The board page carries the project description editor."""
    project = make_project(
        'Board amend',
    )
    content = client.get(f'/projects/{project.pk}/').content.decode()
    assert '/description/' in content
    assert 'project description' in content


@pytest.mark.django_db
def test_project_description_post(client):
    """Project amendments go through save_project and audit as project.updated."""
    project = make_project('Desc post')
    resp = client.post(
        f'/projects/{project.pk}/description/',
        {'description': 'body **md** with a link'},
    )
    assert resp.status_code == 302
    project.refresh_from_db()
    assert project.description == 'body **md** with a link'
    assert EventLog.objects.filter(name='project.updated').count() >= 1
