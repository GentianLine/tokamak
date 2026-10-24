from django.contrib import admin

from .admin_analytics import EventAnalyticsAdmin
from .blockers_admin import BlockerAdmin, EpicAdmin, MilestoneAdmin  # noqa: F401
from .event_analytics import EventAnalytics
from .models import Blocker, Epic, EventLog, Iteration, Milestone, Project, Ticket


class TicketInline(admin.TabularInline):
    model = Ticket
    extra = 0
    fields = ('name', 'type', 'state', 'story_points', 'iteration')


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ('name', 'archived', 'created_at')
    inlines = [TicketInline]


@admin.register(Iteration)
class IterationAdmin(admin.ModelAdmin):
    list_display = (
        'project',
        'number',
        'start_date',
        'end_date',
        'accepted_points',
        'committed_points',
    )
    list_filter = ('project',)


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = (
        'name',
        'project',
        'type',
        'state',
        'story_points',
        'iteration',
        'updated_at',
    )
    list_filter = ('project', 'state', 'type')
    search_fields = ('name', 'description')


@admin.register(EventLog)
class EventLogAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'name', 'project', 'ticket')
    list_filter = ('name', 'project')
    readonly_fields = [f.name for f in EventLog._meta.fields]


# Blocker/epic/milestone admins live in their own module so this file stays
# merge-friendly; importing it registers all three. See blockers_admin.py.
from .blockers_admin import BlockerAdmin, EpicAdmin, MilestoneAdmin  # noqa: E402,F401

# The analytics proxy carries the aggregated event dashboard. Registering
# it here keeps every admin surface discoverable from one module while the
# existing registrations above stay byte-identical (task note: no behavior
# change to existing admin classes). See tokamak/admin_analytics.py.
admin.site.register(EventAnalytics, EventAnalyticsAdmin)
