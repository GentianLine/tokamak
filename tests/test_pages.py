"""Full page renders: index listing, project board and done archive.

These exercise the URL routing, template inheritance, and Unicorn script
injection end-to-end via the Django test client.
"""

import pytest

from tokamak import services

from tests._utils import make_project


@pytest.mark.django_db
def test_pages_render(client):
    """The project index lists projects; the detail page renders tickets,
    forecast panel, and Unicorn hooks."""
    project = make_project('UI proj')
    services.create_ticket(project, name='Visible ticket')
    resp = client.get('/')
    assert 'Projects' in resp.content.decode()
    detail = client.get(f'/projects/{project.pk}/')
    assert detail.status_code == 200
    content = detail.content.decode()
    assert 'Visible ticket' in content
    assert 'Backlog forecast' in content  # velocity panel present
    assert 'unicorn' in content  # unicorn scripts/hooks


@pytest.mark.django_db
def test_board_is_linear_and_hides_done(client):
    """The board renders one linear stream: live work shows, delivered work
    is archived off it, and the sprint line cuts through."""
    project = make_project('Linear proj')
    services.create_ticket(project, name='Live work')
    done = services.create_ticket(project, name='Delivered story')
    services.estimate_ticket(done, 3)
    for st in ('started', 'finished', 'accepted'):
        services.transition_ticket(done, st)

    content = client.get(f'/projects/{project.pk}/').content.decode()
    assert 'Live work' in content
    assert 'Delivered story' not in content  # gone to the archive
    assert 'sprint line' in content.lower()
    assert 'sprint-line dotted' in content  # placeholder: no history yet
    assert f'/projects/{project.pk}/done/' in content  # archive link


@pytest.mark.django_db
def test_done_archive_page(client):
    """The archive page lists accepted and rejected tickets grouped by
    iteration, with the board link back."""
    from datetime import date

    from tests._utils import make_iteration

    project = make_project('Archive proj')
    it = make_iteration(project, 1, date(2026, 1, 5))
    accepted = services.create_ticket(project, name='Shipped feature', iteration=it)
    services.estimate_ticket(accepted, 5)
    for st in ('started', 'finished', 'accepted'):
        services.transition_ticket(accepted, st)
    rejected = services.create_ticket(project, name='Dead spike', iteration=it)
    services.estimate_ticket(rejected, 8)
    for st in ('started', 'finished', 'rejected'):
        services.transition_ticket(rejected, st)

    resp = client.get(f'/projects/{project.pk}/done/')
    assert resp.status_code == 200
    content = resp.content.decode()
    assert 'Shipped feature' in content
    assert 'Dead spike' in content
    assert 'Iteration #1' in content
    assert 'state-rejected' in content
    assert f'/projects/{project.pk}/' in content  # back to board link
