"""Admin event analytics: per-project / per-event-name counts.

Covers the read-only analytics surface added by task admin-event-analytics:
the ``EventAnalytics`` proxy, its aggregate query helpers, and the custom
admin dashboard at ``/admin/tokamak/eventanalytics/``. The audit trail is
append-only; these tests seed it directly (test data setup) but assert the
admin view never writes to it.
"""

from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from tokamak import event_analytics as analytics
from tokamak.admin import EventLogAdmin
from tokamak.admin_analytics import EventAnalyticsAdmin
from tokamak.event_analytics import EventAnalytics
from tokamak.models import EventLog, Project

from tests._utils import make_project


def seed(project=None, name='ticket.created', count=1):
    """Create ``count`` audit rows with the given event name."""
    return [
        EventLog.objects.create(name=name, project=project, payload={})
        for _ in range(count)
    ]


@pytest.fixture
def admin_user(db):
    return User.objects.create_superuser(
        username='root', password='pw', email='root@example.com'
    )


@pytest.fixture
def admin_client(client, admin_user):
    client.force_login(admin_user)
    return client


# ---------------------------------------------------------------------------
# Aggregate helpers
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_project_counts_zero_events(db):
    """No events means empty aggregates -- nothing raises, nothing renders."""
    assert analytics.project_counts() == []
    assert analytics.activity_feed() == []
    assert analytics.project_rollup() == []


@pytest.mark.django_db
def test_project_counts_per_name(db):
    """Counts group by event name and respect the project filter."""
    alpha = make_project('Alpha')
    beta = make_project('Beta')
    seed(alpha, 'ticket.created', 3)
    seed(alpha, 'ticket.estimated', 2)
    seed(beta, 'ticket.created', 1)

    assert analytics.project_counts() == [
        {'name': 'ticket.created', 'total': 4},
        {'name': 'ticket.estimated', 'total': 2},
    ]
    assert analytics.project_counts(alpha) == [
        {'name': 'ticket.created', 'total': 3},
        {'name': 'ticket.estimated', 'total': 2},
    ]
    # primary keys work as filter argument too
    assert analytics.project_counts(beta.pk) == [
        {'name': 'ticket.created', 'total': 1},
    ]


@pytest.mark.django_db
def test_activity_feed_totals_and_share(db):
    """The feed is newest-first with per-name totals and share percentages."""
    project = make_project('Feed proj')
    old = seed(project, 'ticket.created', 2)
    new = seed(project, 'ticket.estimated', 1)

    feed = analytics.activity_feed()
    assert [row['name'] for row in feed] == [
        'ticket.estimated',
        'ticket.created',
        'ticket.created',
    ]
    assert feed[0]['total'] == 1
    assert feed[0]['share_pct'] == pytest.approx(33.3, abs=0.1)
    assert feed[1]['total'] == 2
    assert {row['id'] for row in feed} == {e.pk for e in old + new}


@pytest.mark.django_db
def test_activity_feed_limit(db):
    """``limit=0`` short-circuits; a positive limit caps the feed length."""
    project = make_project('Cap proj')
    seed(project, 'ticket.created', 5)

    assert analytics.activity_feed(limit=0) == []
    assert len(analytics.activity_feed(limit=2)) == 2


@pytest.mark.django_db
def test_window_days_excludes_old_events(db):
    """A time window excludes events older than it without any row loops."""
    project = make_project('Window proj')
    seed(project, 'ticket.created', 2)
    stale = seed(project, 'ticket.legacy', 3)
    EventLog.objects.filter(pk__in=[e.pk for e in stale]).update(
        created_at=timezone.now() - timedelta(days=10)
    )

    assert analytics.project_counts(window_days=5) == [
        {'name': 'ticket.created', 'total': 2},
    ]
    assert len(analytics.activity_feed(window_days=5)) == 2


@pytest.mark.django_db
def test_project_rollup(db):
    """Per-project rollups carry totals and the top event names."""
    alpha = make_project('Alpha')
    beta = make_project('Beta')
    seed(alpha, 'ticket.created', 3)
    seed(alpha, 'ticket.estimated', 2)
    seed(beta, 'ticket.created', 1)

    rollup = analytics.project_rollup()
    assert [(r['project_name'], r['total']) for r in rollup] == [
        ('Alpha', 5),
        ('Beta', 1),
    ]
    assert rollup[0]['top_names'] == [
        ('ticket.created', 3),
        ('ticket.estimated', 2),
    ]


@pytest.mark.django_db
def test_helpers_do_not_loop_per_row(db, django_assert_num_queries):
    """Aggregates run as GROUP BY queries: 1 for counts, 2 for feed/rollup."""
    projects = [make_project(f'Bulk {i}') for i in range(4)]
    for project in projects:
        seed(project, 'ticket.created', 3)

    with django_assert_num_queries(1):
        analytics.project_counts()
    with django_assert_num_queries(2):
        analytics.activity_feed(limit=5)
    with django_assert_num_queries(2):
        analytics.project_rollup()


# ---------------------------------------------------------------------------
# Registration shape (read-only, existing registrations untouched)
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_registrations(db):
    """The proxy is registered with its own admin class; EventLogAdmin stays."""
    from django.contrib import admin

    registry = admin.site._registry
    assert EventAnalytics in registry
    assert isinstance(registry[EventAnalytics], EventAnalyticsAdmin)
    assert isinstance(registry[EventLog], EventLogAdmin)


@pytest.mark.django_db
def test_proxy_shares_eventlog_table(db):
    """The proxy has no schema of its own -- it reads the same audit store."""
    assert EventAnalytics._meta.proxy is True
    assert EventAnalytics._meta.db_table == EventLog._meta.db_table
    project = make_project('Proxy proj')
    seed(project, 'ticket.created', 2)
    assert EventAnalytics.objects.count() == 2


# ---------------------------------------------------------------------------
# Dashboard view
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_dashboard_requires_login(client):
    """Anonymous visitors are bounced to the admin login."""
    resp = client.get('/admin/tokamak/eventanalytics/')
    assert resp.status_code == 302
    assert '/admin/login/' in resp.url


@pytest.mark.django_db
def test_dashboard_renders_counts(admin_client):
    """The dashboard shows per-event-name totals for seeded events."""
    project = make_project('Dash proj')
    seed(project, 'ticket.created', 3)
    seed(project, 'ticket.estimated', 2)

    resp = admin_client.get('/admin/tokamak/eventanalytics/')
    assert resp.status_code == 200
    body = resp.content.decode()
    assert 'ticket.created' in body
    assert 'ticket.estimated' in body
    assert 'Event analytics' in body


@pytest.mark.django_db
def test_dashboard_zero_events_renders_gracefully(admin_client):
    """With no events at all the page still renders a friendly empty state."""
    resp = admin_client.get('/admin/tokamak/eventanalytics/')
    assert resp.status_code == 200
    assert 'No events recorded' in resp.content.decode()


@pytest.mark.django_db
def test_dashboard_project_filter(admin_client):
    """The ?project= filter restricts counts to one project's events."""
    alpha = make_project('Filter Alpha')
    beta = make_project('Filter Beta')
    seed(alpha, 'ticket.created', 3)
    seed(beta, 'ticket.estimated', 2)

    resp = admin_client.get('/admin/tokamak/eventanalytics/', {'project': alpha.pk})
    assert resp.status_code == 200
    body = resp.content.decode()
    assert 'ticket.created' in body
    assert 'ticket.estimated' not in body


@pytest.mark.django_db
def test_dashboard_never_writes_audit_trail(admin_client):
    """GET on the analytics page is strictly read-only for EventLog."""
    project = make_project('ReadOnly proj')
    seed(project, 'ticket.created', 2)
    before = EventLog.objects.count()

    admin_client.get('/admin/tokamak/eventanalytics/')
    admin_client.get('/admin/tokamak/eventanalytics/', {'project': project.pk})
    assert EventLog.objects.count() == before


@pytest.mark.django_db
def test_dashboard_denies_write_actions(admin_client):
    """The append-only audit offers no add/change/delete surface at all."""
    add = admin_client.get('/admin/tokamak/eventanalytics/add/')
    assert add.status_code == 403


@pytest.mark.django_db
def test_eventlog_listing_still_works(admin_client):
    """The untouched raw EventLog changelist still lists audit rows."""
    project = make_project('Raw proj')
    seed(project, 'ticket.created', 1)

    resp = admin_client.get('/admin/tokamak/eventlog/')
    assert resp.status_code == 200
    assert 'ticket.created' in resp.content.decode()
