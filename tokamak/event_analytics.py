"""
Event analytics: read-only aggregation over the append-only ``EventLog``.

This module is the analytics lane for the admin surface (AGENTS.md §7,
task ``admin-event-analytics``). It provides:

* ``EventAnalytics`` — a proxy of ``EventLog`` so the admin can expose an
  aggregated report under its own object name without touching the existing
  ``EventLogAdmin`` registration.
* ``project_counts()`` / ``activity_feed()`` / ``project_rollup()`` —
  aggregate-only query helpers (``.values()`` + annotations; no per-row
  loops, no N+1 queries).

Read-only contract: nothing in this module writes to ``EventLog``; the audit
trail is append-only and written exclusively by the signal receivers.
"""

from datetime import timedelta

from django.db import models
from django.utils import timezone

from .models import EventLog


class EventAnalytics(EventLog):
    """Admin-facing proxy of :class:`~tokamak.models.EventLog`.

    A standard Django proxy: it shares ``EventLog``'s table (no schema of
    its own, so the audit trail stays a single append-only store) while
    giving the admin site its own registration, object name, and analytics
    view. The proxy adds no fields and no behavior to the audit rows.
    """

    class Meta:
        proxy = True
        app_label = 'tokamak'
        verbose_name = 'Event analytics'
        verbose_name_plural = 'Event analytics'
        default_permissions = ('view',)


def _scoped_qs(project, window_days):
    """One filtered base queryset shared by the aggregate helpers."""
    qs = EventLog.objects.all()
    if project is not None:
        qs = qs.filter(project_id=getattr(project, 'pk', project))
    if window_days is not None:
        qs = qs.filter(created_at__gte=timezone.now() - timedelta(days=window_days))
    return qs


def project_counts(project=None, *, window_days=None):
    """Count ``EventLog`` rows per event name, optionally filtered by project.

    Returns a list of dicts ``{'name': str, 'total': int}`` ordered by count
    (descending) then name — one ``GROUP BY`` query, no N+1 loops.

    :param project: a :class:`~tokamak.models.Project`, its primary key,
        or ``None`` for all projects.
    :param window_days: when given, restrict to events created within the
        last ``window_days`` days; ``None`` means no time restriction.
    """
    return list(
        _scoped_qs(project, window_days)
        .values('name')
        .annotate(total=models.Count('id'))
        .order_by('-total', 'name')
    )


def activity_feed(project=None, *, window_days=None, limit=25):
    """Most recent ``EventLog`` entries with their per-name totals.

    Exactly two queries (one for the rows, one shared totals query scoped
    identically), so it stays cheap even when the feed is long. Returns
    dicts with keys ``id``, ``name``, ``project_id``, ``ticket_id``,
    ``created_at``, ``total``, ``share`` (fraction of events in the same
    scope matching this row's name, 0..1) and ``share_pct``."""
    if limit <= 0:
        return []

    rows = list(_scoped_qs(project, window_days).order_by('-created_at')[:limit])
    if not rows:
        return []

    counts = {
        r['name']: r['total']
        for r in (
            _scoped_qs(project, window_days)
            .values('name')
            .annotate(total=models.Count('id'))
        )
    }
    grand = sum(counts.values()) or 1

    return [
        {
            'id': row.pk,
            'name': row.name,
            'project_id': row.project_id,
            'ticket_id': row.ticket_id,
            'created_at': row.created_at,
            'total': counts.get(row.name, 0),
            'share': counts.get(row.name, 0) / grand,
            'share_pct': round(counts.get(row.name, 0) / grand * 100, 1),
        }
        for row in rows
    ]


def project_rollup(limit=50):
    """Per-project event totals plus the top event names within each.

    Two aggregate queries (one per-project grouping, one project+name
    grouping) regardless of how many projects exist — no N+1 loops. Returns
    dicts ordered by total (descending) then name:

    ``{'project_id', 'project_name', 'total', 'top_names': [(name, count)]}``
    """
    rollups = list(
        EventLog.objects.exclude(project__isnull=True)
        .values('project_id', 'project__name')
        .annotate(total=models.Count('id'))
        .order_by('-total', 'project__name')[:limit]
    )
    if not rollups:
        return []

    per_name = {r['project_id']: [] for r in rollups}
    for r in (
        EventLog.objects.exclude(project__isnull=True)
        .values('project_id', 'name')
        .annotate(total=models.Count('id'))
        .order_by('project_id', '-total', 'name')
    ):
        bucket = per_name.get(r['project_id'])
        if bucket is not None and len(bucket) < 5:
            bucket.append((r['name'], r['total']))

    return [
        {
            'project_id': r['project_id'],
            'project_name': r['project__name'],
            'total': r['total'],
            'top_names': per_name[r['project_id']],
        }
        for r in rollups
    ]
