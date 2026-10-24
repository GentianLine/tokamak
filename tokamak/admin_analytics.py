"""
Custom admin view: event analytics per project and per event-name counts.

The report is served from the ``EventAnalytics`` proxy registration (see
``tokamak/admin.py``) at ``/admin/tokamak/eventanalytics/``: the proxy's
changelist *is* the analytics dashboard, while the raw audit listing stays
on the untouched ``EventLogAdmin``.

Read-only by construction: the view only runs aggregate queries against
``EventLog`` and never writes to the append-only audit trail; the
registration disables add/change/delete actions entirely.
"""

from django.contrib import admin
from django.template.response import TemplateResponse

from .event_analytics import (
    activity_feed,
    project_counts,
    project_rollup,
)


class EventAnalyticsAdmin(admin.ModelAdmin):
    """Read-only analytics registration for the ``EventAnalytics`` proxy.

    The changelist renders an aggregated dashboard instead of a raw row
    list: per-event-name counts (optionally filtered by project and time
    window), a per-project rollup, and a recent-activity feed. All figures
    come from grouped aggregate queries -- no per-row loops anywhere.
    """

    list_display = ('created_at', 'name', 'project', 'ticket')
    ordering = ('-created_at',)

    # The audit trail is append-only: offer no write actions at all.
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        """Serve the analytics dashboard in place of a plain row listing.

        Raw audit rows remain available via the untouched ``EventLogAdmin``;
        this proxy exists only to carry the aggregated report.
        """
        return self.analytics_view(request)

    def analytics_view(self, request):
        """Render per-project / per-event-name counts plus recent activity.

        Query params: ``project`` (project id filter), ``days`` (window in
        days), ``limit`` (feed length, clamped to 1..100).
        """
        from .models import Project

        project = None
        raw_project = request.GET.get('project')
        if raw_project and raw_project.isdigit():
            project = Project.objects.filter(pk=int(raw_project)).first()

        raw_days = request.GET.get('days')
        window_days = int(raw_days) if raw_days and raw_days.isdigit() else None

        try:
            limit = max(1, min(int(request.GET.get('limit', '25')), 100))
        except (TypeError, ValueError):
            limit = 25

        counts = project_counts(project, window_days=window_days)
        feed = activity_feed(project, window_days=window_days, limit=limit)
        rollup = [] if project is not None else project_rollup()

        context = {
            **self.admin_site.each_context(request),
            'opts': self.opts,
            'project': project,
            'projects': list(Project.objects.all().values('id', 'name')),
            'counts': counts,
            'feed': feed,
            'rollup': rollup,
            'total_events': sum(c['total'] for c in counts),
            'distinct_names': len(counts),
            'window_days': window_days,
            'limit': limit,
            'title': 'Event analytics',
        }
        return TemplateResponse(request, 'admin/tokamak/event_analytics.html', context)
