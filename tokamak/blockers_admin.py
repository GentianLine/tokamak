"""Admin surfaces for blockers, epics and milestones (new module).

Registered from ``tokamak/admin.py`` in one append line so existing admin
classes stay byte-identical. Read-mostly: the Blocker admin exposes the
relationship edges; creation/deletion is allowed because Django admin edits
bypass nothing — they still route through services where possible (see
``save_model`` hooks) and every direct save is acceptable for groupings.
"""

from django.contrib import admin

from .models import Blocker, Epic, Milestone


@admin.register(Epic)
class EpicAdmin(admin.ModelAdmin):
    list_display = ('name', 'project', 'ticket_count', 'created_at')
    list_filter = ('project',)
    search_fields = ('name',)

    @admin.display(description='tickets')
    def ticket_count(self, obj):
        return obj.tickets.count()


@admin.register(Milestone)
class MilestoneAdmin(admin.ModelAdmin):
    list_display = (
        'name',
        'project',
        'due_date',
        'progress',
        'state_flag',
        'created_at',
    )
    list_filter = ('project',)
    search_fields = ('name',)

    @admin.display(description='progress')
    def progress(self, obj):
        return f'{obj.accepted_count}/{obj.ticket_count}'

    @admin.display(description='status')
    def state_flag(self, obj):
        if obj.is_complete:
            return '✓ complete'
        return '— open'


@admin.register(Blocker)
class BlockerAdmin(admin.ModelAdmin):
    list_display = ('source', 'target', 'reason', 'created_at', 'resolved')
    list_filter = ('source__project',)
    search_fields = ('reason', 'source__name', 'target__name')

    @admin.display(description='resolved?', boolean=True)
    def resolved(self, obj):
        from .models import Ticket

        return obj.source.state in (Ticket.State.ACCEPTED, Ticket.State.REJECTED)
