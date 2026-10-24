"""
Drag-reorder service: persist manual ordering of tickets within a project.

A separate module (rather than edits to ``services.py``) so the board+services
lane stays merge-friendly per AGENTS.md §3 rule 3. Like every other write,
the mutation is mirrored into ``EventLog`` — reorder is an append-only audit
event, not one of the five workflow signals (the payload kwarg contract there
is immutable and ordering is orthogonal to state).

Semantics: within a *display group* (see ``GROUPS``), tickets are ordered by
``Ticket.position`` ascending; a move plucks one ticket out of that group's
ordered list and re-inserts it at the target index, then positions are
compacted back to 0..n-1 for that group. Tickets outside the group keep
their positions untouched.
"""

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import EventLog, Ticket

# Display groups matching the linear stream board (STATE_RANK in
# tokamak/components/board.py): active work, in-review, backlog. Archived
# states never appear on the board and cannot be reordered there.
GROUPS = {
    'active': ('started',),
    'review': ('finished',),
    'backlog': ('unstarted',),
}


def group_for_state(state):
    """Which reorderable display group a state belongs to (or None)."""
    for group, states in GROUPS.items():
        if state in states:
            return group
    return None


def _group_tickets(project, group):
    """Tickets of ``project`` in display ``group``, ordered by position."""
    return list(
        project.tickets.filter(state__in=GROUPS[group]).order_by(
            'position', 'pk', 'created_at'
        )
    )


@transaction.atomic
def reorder_ticket(ticket, target_position, group=None):
    """
    Move ``ticket`` within its display group to index ``target_position``
    (0-based), re-ordering the other members of that group around it.

    ``group`` defaults to the ticket's own state group; passing another
    *known* group is rejected — a drag never changes workflow state, so the
    ticket always stays in its own group.
    """
    if group is None:
        group = group_for_state(ticket.state)
        if group is None:
            raise ValidationError({'position': 'Archived tickets cannot be reordered.'})
    elif group not in GROUPS:
        raise ValidationError({'group': f'Unknown ordering group: {group}'})
    if group_for_state(ticket.state) != group:
        raise ValidationError(
            {'group': 'Ticket does not belong to that ordering group.'}
        )

    try:
        target_position = int(target_position)
    except (TypeError, ValueError):
        raise ValidationError({'position': 'Target position must be an integer.'})
    if target_position < 0:
        raise ValidationError({'position': 'Target position cannot be negative.'})

    ordered = _group_tickets(ticket.project, group)
    # pluck the moved ticket out, then re-insert at the requested index
    members = [t for t in ordered if t.pk != ticket.pk]
    index = min(target_position, len(members))
    members.insert(index, ticket)

    for i, member in enumerate(members):
        Ticket.objects.filter(pk=member.pk).update(position=i)

    EventLog.objects.create(
        name='ticket.reordered',
        ticket=ticket,
        project=ticket.project,
        payload={
            'group': group,
            'to': target_position,
            'order': [t.pk for t in members],
        },
    )
    return ticket
