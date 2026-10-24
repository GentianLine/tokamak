"""Velocity averages and backlog cutoff projection.

Two completed iterations (10 pts, 8 pts accepted) establish a mean velocity
of 9.0; a current iteration ending today + 13 days anchors the projection.
"""

from datetime import date, timedelta

import pytest

from tokamak import services

from tests._utils import make_iteration, make_project


def _seed_history(project):
    """Populate two completed iterations (10 and 8 accepted points) plus a
    running iteration; return the projected last-end date."""
    today = date.today()
    # Two completed iterations: 10 and 8 accepted points.
    for n, pts in [(1, 10), (2, 8)]:
        it = make_iteration(project, n, today - timedelta(days=14 * (3 - n)))
        remaining = pts
        while remaining:
            chunk = min(5, remaining)
            t = services.create_ticket(project, name=f'T{pts}-{chunk}', iteration=it)
            services.estimate_ticket(t, chunk)
            for st in ('started', 'finished', 'accepted'):
                services.transition_ticket(t, st)
            remaining -= chunk
    last_end = today + timedelta(days=13)
    make_iteration(project, 3, today, length=14)
    project.iterations.filter(number=3).update(end_date=last_end)
    return last_end


@pytest.mark.django_db
def test_average_velocity():
    """``average_velocity`` returns the mean accepted points per iteration."""
    project = make_project()
    _seed_history(project)
    assert services.average_velocity(project) == pytest.approx(9.0)


@pytest.mark.django_db
def test_cutoff_projection():
    """A 15-point backlog at velocity 9 lands on iteration +2 from last end."""
    project = make_project()
    last_end = _seed_history(project)
    for name in ('a', 'b', 'c'):
        t = services.create_ticket(project, name=name)
        services.estimate_ticket(t, 5)  # 15 backlog points
    forecast = services.estimate_cutoff(project)
    assert forecast['remaining_points'] == 15
    assert forecast['average_velocity'] == 9.0
    assert forecast['iterations_needed'] == 2  # ceil(15/9)
    assert forecast['estimated_cutoff'] == (
        (last_end + timedelta(days=14 * 2)).isoformat()
    )


@pytest.mark.django_db
def test_sprint_line_fallback_is_dotted_seven():
    """No history at all: the line is a dotted placeholder parked at 7 pts."""
    fresh = make_project('Fresh board')
    line = services.sprint_line(fresh)
    assert line['points'] == 7.0
    assert line['dotted'] is True
    assert line['history_sprints'] == 0


@pytest.mark.django_db
def test_sprint_line_one_sprint_is_still_dotted():
    """A single completed sprint is less than two sprints of data: the line
    stays dotted at the 7-point placeholder instead of trusting one point."""
    project = make_project()
    today = date.today()
    it = make_iteration(project, 1, today - timedelta(days=20))
    t = services.create_ticket(project, name='hist', iteration=it)
    services.estimate_ticket(t, 5)
    for st in ('started', 'finished', 'accepted'):
        services.transition_ticket(t, st)
    line = services.sprint_line(project)
    assert line['points'] == 7.0
    assert line['dotted'] is True
    assert line['history_sprints'] == 1


@pytest.mark.django_db
def test_sprint_line_tracks_average_velocity():
    """Two sprints of history (10 + 8 accepted) put a solid line at the
    mean velocity of 9 pts/sprint."""
    project = make_project()
    _seed_history(project)
    line = services.sprint_line(project)
    assert line['points'] == pytest.approx(9.0)
    assert line['dotted'] is False
    assert line['history_sprints'] == 2


@pytest.mark.django_db
def test_no_history_no_cutoff():
    """With no completed iterations, velocity and cutoff are both ``None``."""
    empty = make_project('Fresh')
    t = services.create_ticket(empty, name='x')
    services.estimate_ticket(t, 3)
    forecast = services.estimate_cutoff(empty)
    assert forecast['estimated_cutoff'] is None
    assert forecast['average_velocity'] is None
