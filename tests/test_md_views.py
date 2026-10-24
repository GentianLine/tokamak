"""Tests for the editor's AJAX endpoints (tokamak/md_views.py)."""

import pytest

from tokamak.models import Ticket
from tests._utils import make_project


@pytest.mark.django_db
def test_preview_renders_gfm(client):
    resp = client.post('/markdown/preview/', {'text': '**bold** and `code`'})
    assert resp.status_code == 200
    html = resp.json()['html']
    assert '<strong>bold</strong>' in html
    assert '<code>code</code>' in html


@pytest.mark.django_db
def test_preview_resolves_ticket_refs(client):
    project = make_project('Preview proj')
    ticket = Ticket.objects.create(project=project, name='Ref target', description='')
    resp = client.post('/markdown/preview/', {'text': f'see #{ticket.pk}'})
    html = resp.json()['html']
    assert f'href="/tickets/{ticket.pk}/"' in html


@pytest.mark.django_db
def test_preview_escapes_raw_html(client):
    resp = client.post(
        '/markdown/preview/', {'text': '<b>raw</b> <script>evil()</script>'}
    )
    html = resp.json()['html']
    assert '<b>' not in html
    assert '&lt;script&gt;' in html


@pytest.mark.django_db
def test_preview_requires_post(client):
    assert client.get('/markdown/preview/').status_code == 405


@pytest.mark.django_db
def test_suggest_matches_ticket_names(client):
    project = make_project('Suggest proj')
    Ticket.objects.create(project=project, name='Login flow polish', description='')
    Ticket.objects.create(project=project, name='Logout bug', description='')
    resp = client.get('/markdown/suggest/', {'kind': 'ticket', 'q': 'login'})
    items = resp.json()['items']
    labels = [i['label'] for i in items]
    assert any('Login flow polish' == l for l in labels)
    assert all('Logout bug' != l for l in labels)


@pytest.mark.django_db
def test_suggest_numeric_matches_exact_pk(client):
    project = make_project('Numeric proj')
    ticket = Ticket.objects.create(project=project, name='Numbered', description='')
    resp = client.get('/markdown/suggest/', {'q': str(ticket.pk)})
    refs = [i['ref'] for i in resp.json()['items']]
    assert f'#{ticket.pk}' in refs


@pytest.mark.django_db
def test_suggest_projects(client):
    project = make_project('Project naming')
    resp = client.get('/markdown/suggest/', {'kind': 'project', 'q': 'naming'})
    items = resp.json()['items']
    assert items and items[0]['ref'] == f'project:{project.pk}'


@pytest.mark.django_db
def test_suggest_empty_query_yields_nothing(client):
    resp = client.get('/markdown/suggest/', {'q': ''})
    assert resp.json()['items'] == []
