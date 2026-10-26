"""
Custom signals fired when project/ticket state changes.

Automated processors should connect to these (see ``automation.py`` for
examples) rather than patching views, so the behaviour is identical whether a
change comes from the Unicorn UI, the REST API, the admin or a management
command — they all funnel through ``tokamak.services`` which sends them.
"""

import django.dispatch

# --- Custom signals ---------------------------------------------------------
# Fired after a ticket moves between board states.
#    providing: ticket, old_state, new_state, user (may be None)
ticket_state_changed = django.dispatch.Signal()

# Fired after a story-point estimate is set or changed.
#    providing: ticket, old_points, new_points
ticket_estimated = django.dispatch.Signal()

# Fired after a ticket is added to / removed from an iteration.
#    providing: ticket, old_iteration, new_iteration
ticket_iteration_changed = django.dispatch.Signal()

# Fired after any ticket is created.
ticket_created = django.dispatch.Signal()

# Fired after a project is created / renamed / archived.
#    providing: project
project_updated = django.dispatch.Signal()

# --- Blocker graph signals (tokamak/services.py::add_blocker/remove_blocker)
# The payload kwarg names below are part of the public contract (AGENTS.md
# §1): a new pair of signals, not a rename of the five existing ones.

# Fired after an edge is added to the blocks/blocked_by relationship.
#    providing: blocker (the Blocker row; source blocks target)
blocker_added = django.dispatch.Signal()

# Fired after an edge is removed from the blocks/blocked_by relationship.
#    providing: blocker (a detached Blocker instance, no longer persisted),
#               reason ('manual' | 'accepted' | 'cycle')
blocker_removed = django.dispatch.Signal()

# --- Thread signals (tokamak/services.py::add_comment/edit_comment/
# delete_comment)
# The thread is the conversation around a ticket; like every other mutation,
# it flows through the service layer so UI, REST and automation all fire the
# same three events.

# Fired after a comment lands on a ticket thread (human or system).
#    providing: comment
comment_added = django.dispatch.Signal()

# Fired after a live comment's body is edited.
#    providing: comment, old_body, new_body
comment_updated = django.dispatch.Signal()

# Fired after a comment is soft-deleted (row kept, body hidden).
#    providing: comment, reason ('manual' | ...)
comment_removed = django.dispatch.Signal()


def register_receivers():
    """Connect the built-in receivers. Called from AppConfig.ready()."""
    from . import automation
    from .signals import (
        project_updated,
        ticket_created,
        ticket_estimated,
        ticket_iteration_changed,
        ticket_state_changed,
    )

    # -- audit trail: every custom signal becomes an EventLog row ------------
    def log_ticket_state(sender, ticket, old_state, new_state, user=None, **kwargs):
        from .models import EventLog

        EventLog.objects.create(
            name='ticket.state_changed',
            ticket=ticket,
            project=ticket.project,
            payload={
                'old': old_state,
                'new': new_state,
                'user': getattr(user, 'username', None),
                'type': ticket.type,
            },
        )

    def log_ticket_estimated(sender, ticket, old_points, new_points, **kwargs):
        from .models import EventLog

        EventLog.objects.create(
            name='ticket.estimated',
            ticket=ticket,
            project=ticket.project,
            payload={'old': old_points, 'new': new_points},
        )

    def log_ticket_iteration(sender, ticket, old_iteration, new_iteration, **kwargs):
        from .models import EventLog

        EventLog.objects.create(
            name='ticket.iteration_changed',
            ticket=ticket,
            project=ticket.project,
            payload={
                'old': getattr(old_iteration, 'number', None),
                'new': getattr(new_iteration, 'number', None),
            },
        )

    def log_ticket_created(sender, ticket, **kwargs):
        from .models import EventLog

        EventLog.objects.create(
            name='ticket.created',
            ticket=ticket,
            project=ticket.project,
            payload={'name': ticket.name, 'type': ticket.type},
        )

    def log_project_updated(sender, project, **kwargs):
        from .models import EventLog

        EventLog.objects.create(
            name='project.updated',
            project=project,
            payload={'name': project.name, 'archived': project.archived},
        )

    def log_blocker_added(sender, blocker, **kwargs):
        from .models import EventLog

        EventLog.objects.create(
            name='blocker.added',
            ticket=blocker.source,
            project=blocker.source.project,
            payload={
                'source': blocker.source_id,
                'target': blocker.target_id,
                'reason': blocker.reason,
            },
        )

    def log_comment_added(sender, comment, **kwargs):
        from .models import EventLog

        EventLog.objects.create(
            name='comment.added',
            ticket=comment.ticket,
            project=comment.ticket.project,
            payload={
                'comment': comment.pk,
                'author': comment.author,
                'kind': comment.kind,
                'body_head': comment.body[:200],
            },
        )

    def log_comment_updated(sender, comment, old_body='', new_body='', **kwargs):
        from .models import EventLog

        EventLog.objects.create(
            name='comment.updated',
            ticket=comment.ticket,
            project=comment.ticket.project,
            payload={
                'comment': comment.pk,
                'old_head': old_body[:200],
                'new_head': new_body[:200],
            },
        )

    def log_comment_removed(sender, comment, reason='manual', **kwargs):
        from .models import EventLog

        EventLog.objects.create(
            name='comment.removed',
            ticket=comment.ticket,
            project=comment.ticket.project,
            payload={'comment': comment.pk, 'reason': reason},
        )

    def log_blocker_removed(sender, blocker, reason='manual', **kwargs):
        from .models import EventLog

        EventLog.objects.create(
            name='blocker.removed',
            ticket=blocker.source,
            project=blocker.source.project,
            payload={
                'source': blocker.source_id,
                'target': blocker.target_id,
                'reason': reason,
                'edge_reason': blocker.reason,
            },
        )

    # -- built-in automation hooks -------------------------------------------
    ticket_state_changed.connect(
        automation.auto_assign_active_iteration,
        dispatch_uid='auto_assign_active_iteration',
    )
    ticket_created.connect(
        automation.auto_estimate_chore, dispatch_uid='auto_estimate_chore'
    )
    ticket_state_changed.connect(
        automation.flag_unestimated_acceptance,
        dispatch_uid='flag_unestimated_acceptance',
    )

    # -- audit (connect last so automation tweaks are captured) --------------
    ticket_state_changed.connect(log_ticket_state, dispatch_uid='log_ticket_state')
    ticket_estimated.connect(log_ticket_estimated, dispatch_uid='log_ticket_estimated')
    blocker_added.connect(log_blocker_added, dispatch_uid='log_blocker_added')
    blocker_removed.connect(log_blocker_removed, dispatch_uid='log_blocker_removed')
    ticket_iteration_changed.connect(
        log_ticket_iteration, dispatch_uid='log_ticket_iteration'
    )
    ticket_created.connect(log_ticket_created, dispatch_uid='log_ticket_created')
    project_updated.connect(log_project_updated, dispatch_uid='log_project_updated')
    comment_added.connect(log_comment_added, dispatch_uid='log_comment_added')
    comment_updated.connect(log_comment_updated, dispatch_uid='log_comment_updated')
    comment_removed.connect(log_comment_removed, dispatch_uid='log_comment_removed')

    # -- live clm-mem mirror (opt-in; receiver no-ops unless TOKAMAK_MEMORY
    #    says mirror='live', so connecting unconditionally is safe) ----------
    def live_mirror_state(sender, ticket, old_state, new_state, user=None, **kwargs):
        from . import automation
        from .memory import MirrorRecord

        automation.mirror_to_memory(
            MirrorRecord(
                'ticket.state_changed',
                ticket.project_id,
                {
                    'old': old_state,
                    'new': new_state,
                    'user': getattr(user, 'username', None),
                    'type': ticket.type,
                },
                ticket=ticket,
            )
        )

    def live_mirror_estimated(sender, ticket, old_points, new_points, **kwargs):
        from . import automation
        from .memory import MirrorRecord

        automation.mirror_to_memory(
            MirrorRecord(
                'ticket.estimated',
                ticket.project_id,
                {'old': old_points, 'new': new_points},
                ticket=ticket,
            )
        )

    def live_mirror_iteration(sender, ticket, old_iteration, new_iteration, **kwargs):
        from . import automation
        from .memory import MirrorRecord

        automation.mirror_to_memory(
            MirrorRecord(
                'ticket.iteration_changed',
                ticket.project_id,
                {
                    'old': getattr(old_iteration, 'number', None),
                    'new': getattr(new_iteration, 'number', None),
                },
                ticket=ticket,
            )
        )

    def live_mirror_created(sender, ticket, **kwargs):
        from . import automation
        from .memory import MirrorRecord

        automation.mirror_to_memory(
            MirrorRecord(
                'ticket.created',
                ticket.project_id,
                {'name': ticket.name, 'type': ticket.type},
                ticket=ticket,
            )
        )

    def live_mirror_project(sender, project, **kwargs):
        from . import automation
        from .memory import MirrorRecord

        automation.mirror_to_memory(
            MirrorRecord(
                'project.updated',
                project.id,
                {'name': project.name, 'archived': project.archived},
            )
        )

    ticket_state_changed.connect(live_mirror_state, dispatch_uid='live_mirror_state')
    ticket_estimated.connect(
        live_mirror_estimated, dispatch_uid='live_mirror_estimated'
    )
    ticket_iteration_changed.connect(
        live_mirror_iteration, dispatch_uid='live_mirror_iteration'
    )
    ticket_created.connect(live_mirror_created, dispatch_uid='live_mirror_created')
    project_updated.connect(live_mirror_project, dispatch_uid='live_mirror_project')
