"""Burndown series math and the velocity component's chart geometry.

Three shapes of history are exercised: no completed iterations at all,
exactly one closed iteration, and a noisy multi-sprint window (scope churn,
carry-over debt, tickets that never burn). The component methods mirror the
pure-math module so both halves of the panel stay in sync.
"""

from datetime import date, timedelta

import pytest
from django.test import RequestFactory

from tokamak import burndown, services
from tokamak.components.velocity import VelocityView

from tests._utils import make_iteration, make_project


def _complete(it, points_per_ticket=5):
    """Accept every estimated ticket in ``it`` through the full workflow."""
    for t in list(it.tickets.all()):
        if t.story_points is None:
            services.estimate_ticket(t, points_per_ticket)
        for st in ('started', 'finished', 'accepted'):
            services.transition_ticket(t, st)


def _seed_one_closed_iteration(project):
    """One closed iteration (10 committed / 5 accepted) + a backlog item."""
    today = date.today()
    it = make_iteration(project, 1, today - timedelta(days=30))
    first = services.create_ticket(project, name='Done', iteration=it)
    services.estimate_ticket(first, 5)
    for st in ('started', 'finished', 'accepted'):
        services.transition_ticket(first, st)
    second = services.create_ticket(project, name='Carried over', iteration=it)
    services.estimate_ticket(second, 5)
    services.transition_ticket(second, 'started')
    backlog = services.create_ticket(project, name='Backlog item')
    services.estimate_ticket(backlog, 3)
    return it


def _seed_noisy_history(project):
    """Three closed iterations with scope churn and unaccepted debt."""
    today = date.today()
    # Iteration 1: committed 8 (5+3), only the 5-pointer accepted.
    it1 = make_iteration(project, 1, today - timedelta(days=45))
    a = services.create_ticket(project, name='Burn A', iteration=it1)
    services.estimate_ticket(a, 5)
    for st in ('started', 'finished', 'accepted'):
        services.transition_ticket(a, st)
    b = services.create_ticket(project, name='Burn B', iteration=it1)
    services.estimate_ticket(b, 3)
    # Iteration 2: committed 10 (5+5), both accepted.
    it2 = make_iteration(project, 2, today - timedelta(days=25))
    c = services.create_ticket(project, name='Burn C', iteration=it2)
    services.estimate_ticket(c, 5)
    d = services.create_ticket(project, name='Burn D', iteration=it2)
    services.estimate_ticket(d, 5)
    for t in (c, d):
        for st in ('started', 'finished', 'accepted'):
            services.transition_ticket(t, st)
    # Iteration 3 (closed): committed 13 (8+5+unestimated creep), nothing
    # ever accepted; the late scope addition still rides on the committed line.
    it3 = make_iteration(project, 3, today - timedelta(days=20))
    e = services.create_ticket(project, name='Burn E', iteration=it3)
    services.estimate_ticket(e, 8)
    f = services.create_ticket(project, name='Burn F', iteration=it3)
    services.estimate_ticket(f, 5)
    g = services.create_ticket(project, name='Scope creep', iteration=it3)
    return [it1, it2, it3]


def _make_view(project):
    view = VelocityView(component_name='velocity', component_id='test-velocity')
    view.setup(RequestFactory().get('/'))
    view.project_id = project.pk
    return view


# ---------------------------------------------------------------------------
# Pure series math (tokamak/burndown.py)
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_burndown_empty_history():
    """No completed iterations: empty rows and a zeroed summary."""
    fresh = make_project('Empty burndown')
    data = burndown.burndown_series(fresh)
    assert data['rows'] == []
    summary = data['summary']
    assert summary['iterations'] == 0
    assert summary['scope_points'] == 0
    assert summary['delivered_points'] == 0
    assert summary['hit_rate'] is None
    assert summary['best'] == 0
    assert summary['worst'] == 0


@pytest.mark.django_db
def test_remaining_empty_history():
    """The remaining line has the same empty contract, with a safe axis max."""
    fresh = make_project('Empty burn line')
    rem = burndown.remaining_series(fresh)
    assert rem['rows'] == []
    assert rem['start_points'] == 0
    assert rem['end_points'] == 0
    assert rem['max_points'] == 1


@pytest.mark.django_db
def test_burndown_single_iteration():
    """One closed iteration: committed 10 / accepted 5, ideal equals the mean."""
    project = make_project('Single burndown')
    _seed_one_closed_iteration(project)
    data = burndown.burndown_series(project)
    assert len(data['rows']) == 1
    row = data['rows'][0]
    assert row['number'] == 1
    assert row['committed'] == 10
    assert row['accepted'] == 5
    assert row['unaccepted'] == 5
    assert row['ideal'] == 5.0
    assert row['ratio'] == 0.5
    summary = data['summary']
    assert summary['iterations'] == 1
    assert summary['scope_points'] == 10
    assert summary['delivered_points'] == 5
    assert summary['hit_rate'] == 0.5
    assert summary['best'] == 5
    assert summary['worst'] == 5


@pytest.mark.django_db
def test_remaining_single_iteration():
    """One closed iteration: the ideal line is pinned at the opening scope."""
    project = make_project('Single burn line')
    _seed_one_closed_iteration(project)
    rem = burndown.remaining_series(project)
    assert len(rem['rows']) == 1
    row = rem['rows'][0]
    assert row['remaining'] == 5
    assert row['ideal_remaining'] == 10.0
    assert rem['start_points'] == 10
    assert rem['end_points'] == 5


@pytest.mark.django_db
def test_burndown_noisy_history():
    """A noisy window: per-row scope/delivery, mean-scaled ideal pace."""
    project = make_project('Noisy burndown')
    _seed_noisy_history(project)
    data = burndown.burndown_series(project)
    assert len(data['rows']) == 3
    assert [r['number'] for r in data['rows']] == [1, 2, 3]

    expected = {
        1: {'committed': 8, 'accepted': 5, 'unaccepted': 3},
        2: {'committed': 10, 'accepted': 10, 'unaccepted': 0},
        3: {'committed': 13, 'accepted': 0, 'unaccepted': 13},
    }
    for row in data['rows']:
        want = expected[row['number']]
        assert row['committed'] == want['committed']
        assert row['accepted'] == want['accepted']
        assert row['unaccepted'] == want['unaccepted']
        assert row['ideal'] == pytest.approx(5.0)
        # ratio is stored rounded to 2dp (round-half-even on .625)
        assert row['ratio'] == round(want['accepted'] / want['committed'], 2)

    summary = data['summary']
    assert summary['scope_points'] == 31
    assert summary['delivered_points'] == 15
    assert summary['hit_rate'] == pytest.approx(0.484)
    assert summary['best'] == 10
    assert summary['worst'] == 0
    assert summary['ideal_hits'] == 2


@pytest.mark.django_db
def test_remaining_noisy_history():
    """The burn line descends from the opening scope toward the closing debt."""
    project = make_project('Noisy burn line')
    _seed_noisy_history(project)
    rem = burndown.remaining_series(project)
    assert [r['remaining'] for r in rem['rows']] == [3, 0, 13]
    assert rem['start_points'] == 8
    assert rem['end_points'] == 13
    assert rem['max_points'] == 13
    # Ideal walk: linear from the first committed scope down to zero.
    ideal = [r['ideal_remaining'] for r in rem['rows']]
    assert ideal[0] == pytest.approx(8.0)
    assert ideal[-1] == pytest.approx(0.0)


@pytest.mark.django_db
def test_burndown_respects_limit_window():
    """The series window keeps the most recent iterations, oldest first."""
    project = make_project('Windowed')
    today = date.today()
    for n in range(1, 6):
        it = make_iteration(project, n, today - timedelta(days=20 * (7 - n)))
        t = services.create_ticket(project, name=f'w{n}', iteration=it)
        services.estimate_ticket(t, 3)
        for st in ('started', 'finished', 'accepted'):
            services.transition_ticket(t, st)
    data = burndown.burndown_series(project, limit=2)
    assert [r['number'] for r in data['rows']] == [4, 5]


@pytest.mark.django_db
def test_burndown_active_iteration_excluded():
    """In-flight sprints stay out of the default series (completed only)."""
    project = make_project('Active excluded')
    today = date.today()
    closed = make_iteration(project, 1, today - timedelta(days=30))
    _complete(closed)
    make_iteration(project, 2, today - timedelta(days=5))  # active, not done
    data = burndown.burndown_series(project)
    assert [r['number'] for r in data['rows']] == [1]


# ---------------------------------------------------------------------------
# Velocity component: computed methods (never properties) + SVG geometry
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_component_burndown_matches_module():
    """The component method delegates to the module without reinterpreting."""
    project = make_project('Component bd')
    _seed_one_closed_iteration(project)
    view = _make_view(project)
    assert view.burndown() == burndown.burndown_series(project)
    assert view.remaining() == burndown.remaining_series(project)


@pytest.mark.django_db
def test_component_burndown_without_project():
    """No project bound: empty shapes, not crashes (Unicorn hydration-safe)."""
    view = VelocityView(component_name='velocity', component_id='v-empty')
    view.setup(RequestFactory().get('/'))
    assert view.burndown()['rows'] == []
    assert view.remaining()['rows'] == []
    assert view.burndown_svg()['empty'] is True


@pytest.mark.django_db
def test_component_burndown_svg_empty_history():
    """No history: the geometry reports an empty chart with a viewBox."""
    fresh = make_project('SVG empty')
    view = _make_view(fresh)
    svg = view.burndown_svg()
    assert svg['empty'] is True
    assert svg['bars'] == []
    assert svg['ideal_line'] is None
    assert svg['burn_line'] is None
    assert svg['view_box'].startswith('0 0 ')


@pytest.mark.django_db
def test_component_burndown_svg_single_iteration():
    """One iteration: a bar pair per sprint, a burn point, no ideal segment."""
    project = make_project('SVG single')
    _seed_one_closed_iteration(project)
    view = _make_view(project)
    svg = view.burndown_svg()
    assert svg['empty'] is False
    assert len(svg['bars']) == 1
    bar = svg['bars'][0]
    assert bar['committed'] == 10 and bar['accepted'] == 5
    # Accepted burns inside the committed scope band (top-left of both).
    assert bar['scope_y'] < bar['deliv_y']
    assert bar['scope_h'] > bar['deliv_h'] > 0
    assert svg['burn_line'].count(' ') == 0  # one point => no separator
    assert svg['ideal_line'] is None


@pytest.mark.django_db
def test_component_burndown_svg_noisy_history():
    """Noisy window: three bars, a multi-point burn line and ideal pace."""
    project = make_project('SVG noisy')
    _seed_noisy_history(project)
    view = _make_view(project)
    svg = view.burndown_svg()
    assert len(svg['bars']) == 3
    assert svg['burn_line'].count(' ') == 2
    assert svg['ideal_line'] is not None
    assert svg['ideal_line'].count(' ') == 2
    # Geometry stays inside the viewBox and above the axis labels.
    for bar in svg['bars']:
        assert 46 <= bar['x'] + 18 <= 506
        assert bar['scope_y'] >= 12
        assert bar['scope_y'] + bar['scope_h'] <= 190


@pytest.mark.django_db
def test_component_burndown_svg_axis_ticks():
    """The axis divides the max scope into evenly spaced labelled ticks."""
    project = make_project('SVG axis')
    _seed_noisy_history(project)
    view = _make_view(project)
    svg = view.burndown_svg()
    assert len(svg['axis']) == 5
    labels = [t['label'] for t in svg['axis']]
    ys = [t['y'] for t in svg['axis']]
    assert labels[0] == 0 and labels[-1] == 13
    assert ys == sorted(ys, reverse=True)  # top of chart is the max value


@pytest.mark.django_db
def test_velocity_page_renders_burndown(client):
    """The project page carries the burndown panel through a full render."""
    project = make_project('Render bd')
    _seed_noisy_history(project)
    content = client.get(f'/projects/{project.pk}/').content.decode()
    assert 'Burndown' in content
    assert 'bd-svg' in content
    assert 'committed scope' in content


@pytest.mark.django_db
def test_velocity_page_burndown_empty(client):
    """A brand-new project renders the empty-state copy, not a broken SVG."""
    fresh = make_project('Render bd empty')
    content = client.get(f'/projects/{fresh.pk}/').content.decode()
    assert 'Burndown' in content
    assert 'No burndown yet' in content
