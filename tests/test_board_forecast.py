"""Board forecast strip (cutoff-surface-board).

The board header renders compact chips from ``services.estimate_cutoff`` via
the Unicorn component's lazy ``forecast()`` method: quiet placeholder when
there is no history to learn from, full cutoff/pessimistic/confidence chips
once velocity exists. The service dict stays the single source — the strip
renders exactly what the velocity panel shows in full.
"""

from datetime import date

import pytest
from django.test import RequestFactory

from tokamak import services
from tokamak.components.board import BoardView

from tests._utils import make_project, make_iteration


def _board(project):
    view = BoardView(component_name='board', component_id='test-board')
    view.setup(RequestFactory().get('/'))
    view.project_id = project.pk
    return view


@pytest.mark.django_db
def test_forecast_method_delegates_to_service_layer(db):
    """The component dict is byte-for-byte the service-layer forecast."""
    project = make_project('Comp forecast')
    story = services.create_ticket(project, name='Backlog story')
    services.estimate_ticket(story, 5)
    view = _board(project)
    assert view.forecast() == services.estimate_cutoff(project)
    assert view.forecast()['remaining_points'] == 5


@pytest.mark.django_db
def test_board_page_shows_quiet_chip_without_history(client):
    """No completed iterations: the strip shows remaining points and a
    dotted-honest cutoff placeholder instead of inventing dates."""
    project = make_project('Quiet forecast')
    services.create_ticket(project, name='Backlog item')
    content = client.get(f'/projects/{project.pk}/').content.decode()
    assert 'forecast-strip' in content
    assert '0 pts in backlog' in content  # unestimated backlog counts zero
    assert 'cutoff — no history yet' in content
    assert 'estimated cutoff' not in content


@pytest.mark.django_db
def test_board_page_shows_cutoff_pessimistic_and_confidence(client):
    """With two noisy completed sprints the strip carries the estimated
    cutoff, the pessimistic variant (velocity spread > 0.35) and the
    confidence score — same figures as ``estimate_cutoff``."""
    project = make_project('Noisy forecast')
    it1 = make_iteration(project, 1, date(2026, 1, 5))
    it2 = make_iteration(project, 2, date(2026, 1, 19))
    for iteration, points in ((it1, 8), (it2, 2)):
        story = services.create_ticket(
            project, name=f'Shiped {iteration.number}', iteration=iteration
        )
        services.estimate_ticket(story, points)
        for st in ('started', 'finished', 'accepted'):
            services.transition_ticket(story, st)
    backlog = services.create_ticket(project, name='Next up')
    services.estimate_ticket(backlog, 3)

    fc = services.estimate_cutoff(project)
    # sample stdev over two sprints: sqrt(((8-5)^2+(2-5)^2)/1) = sqrt(18);
    # spread = sqrt(18)/5 ~= 0.85 > 0.35 -> pessimistic one sprint later.
    assert fc['confidence'] == 0.15
    content = client.get(f'/projects/{project.pk}/').content.decode()
    assert 'forecast-strip' in content
    assert fc['estimated_cutoff'] in content
    assert fc['estimated_cutoff_pessimistic'] in content
    assert 'pessimistic' in content
    assert 'confidence 0.15' in content
    assert 'cutoff — no history yet' not in content
