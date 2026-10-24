"""
Domain services: the single entry point for every state change.

The Unicorn components, the DRF API and management commands all call these
functions, so Django signals fire consistently no matter where the change
originated (human UI click, REST call from an automation, admin edit via the
ticket.save() hook).
"""

import math
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .models import Project, Ticket
from .signals import (
    project_updated,
    ticket_created,
    ticket_estimated,
    ticket_iteration_changed,
    ticket_state_changed,
)

# Allowed board transitions (agile-style: forward one step, back one step,
# reject from finished/accepted, recover a rejected ticket to started).
ALLOWED_TRANSITIONS = {
    Ticket.State.UNSTARTED: {Ticket.State.STARTED},
    Ticket.State.STARTED: {Ticket.State.FINISHED, Ticket.State.UNSTARTED},
    Ticket.State.FINISHED: {
        Ticket.State.ACCEPTED,
        Ticket.State.STARTED,
        Ticket.State.REJECTED,
    },
    Ticket.State.ACCEPTED: {Ticket.State.REJECTED, Ticket.State.FINISHED},
    Ticket.State.REJECTED: {Ticket.State.STARTED},
}


def _fibonacci():
    return set(settings.TOKAMAK['FIBONACCI_POINTS'])


def _chore_title(description):
    """Derive a chore's display title from its description (markdown-free)."""
    from .markdown import excerpt  # lazy: rendering helper, not core math

    return excerpt(description, limit=120) or 'Chore'


@transaction.atomic
def create_project(name, description=''):
    """Create a project and fire ``project_updated`` like every other write."""
    if not (name or '').strip():
        raise ValidationError({'name': 'Project name is required.'})
    project = Project.objects.create(name=name.strip(), description=description or '')
    project_updated.send(sender=Project, project=project)
    return project


# ---------------------------------------------------------------------------
# Mutations (each one fires the matching signal)
# ---------------------------------------------------------------------------
@transaction.atomic
def create_ticket(
    project,
    *,
    name,
    description='',
    type=Ticket.Type.FEATURE,
    story_points=None,
    iteration=None,
    external_ref='',
):
    if type not in Ticket.Type.values:
        raise ValidationError({'type': f'Unknown type: {type}'})

    if type == Ticket.Type.CHORE:
        # A chore is nothing but a description. The dedicated creation page
        # hides the title field and always requires prose; direct callers
        # (automation, API) may still name one instead -- but never neither.
        # Chores never carry a manual estimate.
        if not (description or '').strip() and not (name or '').strip():
            raise ValidationError({'description': 'A chore needs a description.'})
        story_points = None
        if not (name or '').strip():
            name = _chore_title(description)
    elif not (name or '').strip():
        raise ValidationError({'name': 'Ticket name is required.'})

    if story_points is not None:
        points = int(story_points)
        if points not in _fibonacci():
            raise ValidationError(
                {
                    'story_points': f'{points} is not a Fibonacci estimate: '
                    f'{sorted(_fibonacci())}'
                }
            )
        story_points = points

    last = project.tickets.order_by('-position').first()
    ticket = Ticket.objects.create(
        project=project,
        name=name.strip(),
        description=description,
        type=type,
        story_points=story_points,
        iteration=iteration,
        external_ref=external_ref,
        position=(last.position + 1) if last else 1,
    )
    ticket_created.send(sender=Ticket, ticket=ticket)
    return ticket


@transaction.atomic
def transition_ticket(ticket, new_state, user=None):
    """Move a ticket to ``new_state``, enforcing the board workflow."""
    old_state = ticket.state
    if new_state == old_state:
        return ticket
    if new_state not in Ticket.State.values:
        raise ValidationError({'state': f'Unknown state: {new_state}'})
    if new_state not in ALLOWED_TRANSITIONS[old_state]:
        raise ValidationError(
            {
                'state': f'Cannot move from {old_state} to {new_state}. '
                f'Allowed: {sorted(ALLOWED_TRANSITIONS[old_state])}'
            }
        )
    if (
        new_state == Ticket.State.ACCEPTED
        and ticket.story_points is None
        and ticket.type != Ticket.Type.CHORE
    ):
        raise ValidationError(
            {'story_points': 'Estimate a ticket before accepting it.'}
        )

    if new_state == Ticket.State.ACCEPTED:
        # Guard: a blocked story cannot be accepted; resolve the
        # blockers first (or accept whatever is blocking it).
        assert_acceptable(ticket)

    ticket.state = new_state
    ticket.completed_at = (
        timezone.now()
        if new_state in (Ticket.State.ACCEPTED, Ticket.State.REJECTED)
        else None
    )
    ticket.save(update_fields=['state', 'completed_at', 'updated_at'])

    if new_state == Ticket.State.ACCEPTED:
        # Accepting the cause clears every edge it pointed at.
        clear_blockers_on_acceptance(ticket, user=user)

    ticket_state_changed.send(
        sender=Ticket,
        ticket=ticket,
        old_state=old_state,
        new_state=new_state,
        user=user,
    )
    return ticket


@transaction.atomic
def estimate_ticket(ticket, points):
    """Assign a story-point estimate (must be Fibonacci). Fires ticket_estimated."""
    old_points = ticket.story_points
    if points is not None:
        points = int(points)
        if points not in _fibonacci():
            raise ValidationError(
                {
                    'story_points': f'{points} is not a Fibonacci estimate: '
                    f'{sorted(_fibonacci())}'
                }
            )
    ticket.story_points = points
    ticket.save(update_fields=['story_points', 'updated_at'])
    if old_points != points:
        ticket_estimated.send(
            sender=Ticket, ticket=ticket, old_points=old_points, new_points=points
        )
    return ticket


@transaction.atomic
def assign_iteration(ticket, iteration):
    """Put a ticket into (or back out of, with None) an iteration."""
    old_iteration = ticket.iteration
    if old_iteration == iteration:
        return ticket
    if iteration is not None and iteration.project_id != ticket.project_id:
        raise ValidationError({'iteration': 'Iteration belongs to another project.'})
    ticket.iteration = iteration
    ticket.save(update_fields=['iteration', 'updated_at'])
    ticket_iteration_changed.send(
        sender=Ticket,
        ticket=ticket,
        old_iteration=old_iteration,
        new_iteration=iteration,
    )
    return ticket


def save_project(project, *, name=None, description=None, archived=None):
    changed = False
    if name is not None and name != project.name:
        project.name = name
        changed = True
    if description is not None and description != project.description:
        project.description = description
        changed = True
    if archived is not None and archived != project.archived:
        project.archived = archived
        changed = True
    if changed:
        project.save()
    project_updated.send(sender=type(project), project=project)
    return project


# ---------------------------------------------------------------------------
# Planning / estimation math
# ---------------------------------------------------------------------------
def backlog_points(project):
    """Story points still to be delivered (everything not accepted/rejected)."""
    agg = project.tickets.exclude(
        state__in=(Ticket.State.ACCEPTED, Ticket.State.REJECTED)
    ).aggregate(total=Sum('story_points'))
    return agg['total'] or 0


def iteration_length_days(project):
    """Median historical sprint length, falling back to the project default."""
    lengths = sorted(
        (it.end_date - it.start_date).days + 1  # inclusive sprint span
        for it in project.iterations.all()
        if it.end_date >= it.start_date
    )
    if not lengths:
        return settings.TOKAMAK['DEFAULT_ITERATION_LENGTH_DAYS']
    return lengths[len(lengths) // 2]


def average_velocity(project):
    """
    Mean points *accepted* per completed iteration (the classic sprint
    velocity). Returns None when there is no history to learn from.
    """
    completed = [it for it in project.iterations.all() if it.is_completed]
    if not completed:
        return None
    total = sum(it.accepted_points for it in completed)
    return total / len(completed)


def velocity_series(project, limit=10):
    """Per-iteration accepted points, oldest first (for the burn-up chart)."""
    iterations = [it for it in project.iterations.all() if it.is_completed]
    iterations.sort(key=lambda it: it.number)
    return [
        {
            'number': it.number,
            'end_date': it.end_date.isoformat(),
            'points': it.accepted_points,
            'committed': it.committed_points,
        }
        for it in iterations[-limit:]
    ]


# Fallback capacity used for the board's sprint line when the project has
# fewer than two sprints of history to learn a real velocity from.
SPRINT_LINE_FALLBACK_POINTS = 7


def sprint_line(project):
    """Capacity line for the linear stream board.

    The line marks how much work fits in one iteration: it sits at the mean
    accepted points per completed iteration (the average velocity). When the
    project has fewer than two sprints of data there is nothing to learn
    from, so the line falls back to ``SPRINT_LINE_FALLBACK_POINTS`` and is
    flagged ``dotted`` so the UI can render it as a dotted placeholder.
    """
    completed = [it for it in project.iterations.all() if it.is_completed]
    result = {
        'points': float(SPRINT_LINE_FALLBACK_POINTS),
        'dotted': True,
        'basis': f'no history ({len(completed)} of 2 sprints)',
        'history_sprints': len(completed),
    }
    if len(completed) >= 2:
        velocity = average_velocity(project)
        if velocity:
            result = {
                'points': round(velocity, 1),
                'dotted': False,
                'basis': f'avg over {len(completed)} sprints',
                'history_sprints': len(completed),
            }
    return result


def transition_path(from_state, to_state):
    """Shortest legal sequence of transitions between two states (BFS).

    Returns a list of states ending in ``to_state``, or None if unreachable.
    Used by drag-and-drop so the UI can walk a ticket through intermediate
    states — every hop still fires ``ticket_state_changed``.
    """
    if from_state == to_state:
        return []
    queue = [[from_state]]
    seen = {from_state}
    while queue:
        path = queue.pop(0)
        for nxt in ALLOWED_TRANSITIONS[path[-1]]:
            if nxt in seen:
                continue
            candidate = path + [nxt]
            if nxt == to_state:
                return candidate[1:]
            seen.add(nxt)
            queue.append(candidate)
    return None


def estimate_cutoff(project, as_of=None):
    """
    Forecast when the backlog will be done using historical velocity.

    Returns a dict with remaining points, average velocity, projected number
    of iterations and the projected cutoff date (end of the iteration in
    which the backlog is exhausted). ``None`` velocity means: not enough
    history yet — the UI shows "get a couple of sprints of data first".
    """
    velocity = average_velocity(project)
    remaining = backlog_points(project)
    length = iteration_length_days(project)
    today = as_of or timezone.localdate()

    result = {
        'remaining_points': remaining,
        'average_velocity': None if velocity is None else round(velocity, 2),
        'iteration_length_days': length,
        'iterations_needed': None,
        'estimated_cutoff': None,
        'confidence': None,
    }
    if velocity is None or velocity <= 0:
        return result

    iterations_needed = max(1, math.ceil(remaining / velocity))

    # Anchor the projection on the last known iteration end (or today).
    last = project.iterations.order_by('-end_date').first()
    anchor = max(last.end_date, today) if last else today
    cutoff = anchor + timedelta(days=length * iterations_needed)

    # Confidence: how stable the velocity has been (stdev / mean).
    series = [row['points'] for row in velocity_series(project)]
    if len(series) >= 2:
        mean = sum(series) / len(series)
        variance = sum((p - mean) ** 2 for p in series) / (len(series) - 1)
        stdev = math.sqrt(variance)
        spread = stdev / mean if mean else 1.0
        result['confidence'] = round(max(0.0, 1.0 - spread), 2)
        # Pessimistic cutoff: one sprint of slack when velocity is noisy.
        pessimistic_iters = iterations_needed + (1 if spread > 0.35 else 0)
        result['estimated_cutoff_pessimistic'] = (
            anchor + timedelta(days=length * pessimistic_iters)
        ).isoformat()

    result['iterations_needed'] = iterations_needed
    result['estimated_cutoff'] = cutoff.isoformat()
    return result


# ---------------------------------------------------------------------------
# Ticket memory sessions (clm-mem bridge; see INTEGRATION.md "Session lifecycle")
#
# Lifecycle contract:
#   OPEN  -> opened when work starts on the ticket (or explicitly via REST/CLI)
#   ENDED -> closed exactly once, by reason: 'manual', 'ticket:<state>',
#            'withdrawn' (back to unstarted), or 'reopen' (replacing a stale
#            attempt). Sessions never outlive their ticket.
# On end: live long/short memory offloads into the ticket-scoped archive
# (memory handoff across attempts) and a distilled summary flows back into
# MemorySession.summary + an EventLog row. Nothing mutates Ticket state —
# that stays the job of the workflow functions above, from UI/API/admin/bots.
# ---------------------------------------------------------------------------
@transaction.atomic
def open_ticket_session(ticket, *, user=None, monitor_url='', session_key=''):
    """Open a clm-mem session for one ticket attempt; idempotent per ticket:
    any still-OPEN session on the same ticket is closed with reason 'reopen'."""
    from . import memory as bridge
    from .models import EventLog, MemorySession

    if not bridge.sessions_enabled():
        raise ValidationError({'ticket_sessions': 'bridge sessions are disabled'})

    stale = MemorySession.objects.filter(
        ticket=ticket, status=MemorySession.Status.OPEN
    )
    for old in stale:
        _end_memory_session(old, reason='reopen')

    cfg = bridge.get_config()
    key = session_key or bridge.session_key(ticket.id)
    url = monitor_url
    if not url:
        link = bridge.monitor_url_for(cfg['mode'], cfg.get('backend_base_url', ''))
        url = f"{link}{key}" if link else ''
    sess = MemorySession.objects.create(
        ticket=ticket,
        session_key=key,
        mode=cfg['mode'],
        monitor_url=url,
        payload={'user': getattr(user, 'username', None)},
    )
    if not url:
        # local mode: monitoring is the bridge snapshot route + mirrored files
        sess.monitor_url = f"/api/sessions/{sess.pk}/snapshot/"
        sess.save(update_fields=['monitor_url'])
    # Seed continuity: pull previous attempts' archived facts into the window.
    try:
        harness = bridge.ticket_harness(ticket)
        seeded = bridge.seed_from_archive(harness)
        if seeded:
            sess.payload['seeded_blocks'] = seeded
            sess.save()
    except Exception:
        # seeding is best-effort (a served daemon may be unreachable); the
        # session still opens.
        pass
    EventLog.objects.create(
        name='session.opened',
        ticket=ticket,
        project=ticket.project,
        payload={
            'session': sess.id,
            'key': key,
            'seeded': sess.payload.get('seeded_blocks', 0),
        },
    )
    return sess


@transaction.atomic
def close_ticket_session(session, *, reason='manual', summary=None):
    """End one session: archive live memory, distill the summary, audit-log."""
    from .models import MemorySession

    if session.status == MemorySession.Status.ENDED:
        return session  # idempotent
    _end_memory_session(session, reason=reason, summary=summary)
    return MemorySession.objects.get(pk=session.pk)


def _end_memory_session(session, *, reason='', summary=None):
    """Shared teardown used by both service entry points (called in-txn)."""
    from . import memory as bridge
    from .models import EventLog, MemorySession

    ticket = session.ticket
    counts = {}
    try:
        harness = bridge.ticket_harness(ticket)
        if summary is None and bridge.get_config().get('session_distill_model'):
            summary = bridge.model_distill(harness, ticket.name) or None
        if summary is None:
            summary = bridge.distill_summary(harness, ticket.name)
        offloaded = bridge.archive_live(harness)
        counts = {
            'offloaded': offloaded,
            'archive': getattr(harness, 'archive_size', lambda: 0)(),
        }
        # End-of-lifecycle: served branches are dropped server-side (DELETE
        # /v1/clm/sessions/<id>); local harnesses just release the cache.
        bridge.drop_ticket_branch(ticket.id)
    except Exception:
        # served backends may be unreachable; the DB lifecycle still ends.
        summary = summary or ''

    session.summary = summary or ''
    session.status = MemorySession.Status.ENDED
    session.end_reason = reason or 'manual'
    session.ended_at = timezone.now()
    session.payload = {**session.payload, **counts}
    session.save()
    EventLog.objects.create(
        name='session.ended',
        ticket=ticket,
        project=ticket.project,
        payload={
            'session': session.id,
            'reason': session.end_reason,
            'summary_head': session.summary[:200],
            **counts,
        },
    )


def latest_session_summary(ticket):
    """Most recent MemorySession for a ticket (open or ended) or None."""
    from .models import MemorySession

    return MemorySession.objects.filter(ticket=ticket).order_by('-started_at').first()


# ---------------------------------------------------------------------------
# Blockers, epics and milestones — concept trio
#
# Blockers are a real many-to-many relationship between tickets: an edge
# means "ticket A blocks ticket B" (B is blocked_by A). Every graph mutation
# lives here so the UI, the REST API and automation all get identical
# validation + signals (AGENTS.md section 1). Rules enforced here:
#   * no self-blocks, both tickets in the same project;
#   * the graph stays acyclic (a cycle would deadlock acceptance);
#   * accepting a blocker clears its edge automatically (resolve it by
#     resolving the cause), while accepting a blocked ticket is refused.
# Epics/membership and milestone membership are plain per-project groupings;
# their mutations are mirrored into EventLog (append-only audit, same
# pattern as reorder.py -- no new workflow signals beyond the blocker pair).
# ---------------------------------------------------------------------------
def _edge_exists(a, b):
    from .models import Blocker

    return Blocker.objects.filter(source=a, target=b).exists()


def _reachable(src_pk, dst_pk, project_id):
    """True if a directed path exists from ticket pk src_pk to dst_pk."""
    from .models import Blocker

    seen = {src_pk}
    frontier = [src_pk]
    while frontier:
        nxt_ids = list(
            Blocker.objects.filter(
                source_id__in=frontier, target__project_id=project_id
            ).values_list('target_id', flat=True)
        )
        if dst_pk in nxt_ids:
            return True
        new_frontier = [pk for pk in nxt_ids if pk not in seen]
        seen.update(new_frontier)
        frontier = new_frontier
    return False


@transaction.atomic
def add_blocker(source, target, reason=''):
    """Declare that ``source`` blocks ``target`` (with an optional short
    reason). Raises ValidationError on self-blocks, cross-project pairs,
    duplicate edges, or any edge that would close a cycle."""
    from .models import Blocker

    if source == target:
        raise ValidationError({'blocker': 'A ticket cannot block itself.'})
    if source.project_id != target.project_id:
        raise ValidationError({'blocker': 'Blockers are project-local.'})
    if _edge_exists(source, target):
        raise ValidationError({'blocker': 'That blocker already exists.'})
    # Cycle prevention: adding source->target closes a cycle iff a path
    # target -> ... -> source already exists (or the reverse edge does).
    if _edge_exists(target, source) or _reachable(
        target.pk, source.pk, source.project_id
    ):
        raise ValidationError({'blocker': 'That blocker would create a cycle.'})

    blocker = Blocker.objects.create(
        source=source, target=target, reason=(reason or '').strip()[:300]
    )
    from .signals import blocker_added

    blocker_added.send(sender=Blocker, blocker=blocker)
    return blocker


@transaction.atomic
def remove_blocker(source, target, *, user=None):
    """Manually resolve the edge (fires blocker_removed with reason
    'manual'). Returns None when no such edge exists."""
    from .models import Blocker
    from .signals import blocker_removed

    edge = Blocker.objects.filter(source=source, target=target).first()
    if edge is None:
        return None
    edge.delete()
    blocker_removed.send(sender=Blocker, blocker=edge, reason='manual')
    return edge


def blockers_of(ticket):
    """The tickets ``ticket`` blocks (its outgoing edges)."""
    return ticket.blockers


def blocked_by(ticket):
    """The tickets blocking ``ticket`` (its incoming edges)."""
    return ticket.blocked_by


def is_blocked(ticket):
    """Blocked flag: any unresolved incoming blocker."""
    return ticket.is_blocked


@transaction.atomic
def clear_blockers_on_acceptance(ticket, *, user=None):
    """Accepting a ticket resolves every edge it points at (it was the
    cause). Returns the list of cleared edges. Called from
    transition_ticket; also exposed for idempotent automation."""
    from .models import Blocker
    from .signals import blocker_removed

    if ticket.state != Ticket.State.ACCEPTED:
        return []
    cleared = []
    for edge in Blocker.objects.filter(source=ticket):
        edge.delete()
        blocker_removed.send(sender=Blocker, blocker=edge, reason='accepted')
        cleared.append(edge)
    return cleared


def assert_acceptable(ticket):
    """Guard used by transition_ticket: refuse to accept a ticket that
    still carries unresolved blockers (finish the cause first).

    Only live edges count; edges whose blocker already sits accepted or
    rejected on the archive are considered resolved."""
    from .models import Blocker

    blocked = (
        Blocker.objects.filter(target=ticket)
        .exclude(source__state__in=(Ticket.State.ACCEPTED, Ticket.State.REJECTED))
        .exists()
    )
    if blocked:
        raise ValidationError(
            {
                'blocker': f'Ticket #{ticket.pk} is blocked; resolve its blockers '
                'or accept the blocking tickets first.'
            }
        )


# ---------------------------------------------------------------------------
# Epics + milestones (grouping membership services)
# ---------------------------------------------------------------------------
@transaction.atomic
def create_epic(project, name):
    """Create a named epic for ``project``."""
    from .models import Epic

    if not (name or '').strip():
        raise ValidationError({'name': 'Epic name is required.'})
    return Epic.objects.create(project=project, name=name.strip()[:200])


@transaction.atomic
def ticket_set_epic(ticket, epic):
    """Attach ``ticket`` to an epic (or detach with None). Cross-project
    epics are refused; mirrored into EventLog."""
    if epic is not None and epic.project_id != ticket.project_id:
        raise ValidationError({'epic': 'Epic belongs to another project.'})
    old = ticket.epic
    if old == epic:
        return ticket
    ticket.epic = epic
    ticket.save(update_fields=['epic', 'updated_at'])
    from .models import EventLog

    EventLog.objects.create(
        name='ticket.epic_changed',
        ticket=ticket,
        project=ticket.project,
        payload={
            'old': getattr(old, 'name', None),
            'new': getattr(epic, 'name', None),
        },
    )
    return ticket


@transaction.atomic
def create_milestone(project, name, due_date=None):
    """Create a date-targeted milestone (PT release-style marker)."""
    from .models import Milestone

    if not (name or '').strip():
        raise ValidationError({'name': 'Milestone name is required.'})
    return Milestone.objects.create(
        project=project, name=name.strip()[:200], due_date=due_date
    )


@transaction.atomic
def ticket_set_milestone(ticket, milestone):
    """Attach ``ticket`` to a milestone (or detach with None). Cross-project
    milestones are refused; mirrored into EventLog."""
    if milestone is not None and milestone.project_id != ticket.project_id:
        raise ValidationError({'milestone': 'Milestone belongs to another project.'})
    old = ticket.milestone
    if old == milestone:
        return ticket
    ticket.milestone = milestone
    ticket.save(update_fields=['milestone', 'updated_at'])
    from .models import EventLog

    EventLog.objects.create(
        name='ticket.milestone_changed',
        ticket=ticket,
        project=ticket.project,
        payload={
            'old': getattr(old, 'name', None),
            'new': getattr(milestone, 'name', None),
        },
    )
    return ticket


def milestone_progress(milestone):
    """Progress facts for one milestone (derived, never stored)."""
    tickets = list(milestone.tickets.all())
    accepted = [t for t in tickets if t.state == Ticket.State.ACCEPTED]
    rejected = [t for t in tickets if t.state == Ticket.State.REJECTED]
    return {
        'milestone': milestone,
        'total': len(tickets),
        'accepted': len(accepted),
        'rejected': len(rejected),
        'complete': bool(tickets)
        and len(accepted) + len(rejected) == len(tickets)
        and len(accepted) > 0,
        'accepted_points': sum(t.story_points or 0 for t in accepted),
    }


def epic_model():
    """The Epic model, exposed for pages that get-or-create by name."""
    from .models import Epic

    return Epic


def milestone_model():
    """The Milestone model, exposed for pages that get-or-create by name."""
    from .models import Milestone

    return Milestone


def project_milestones(project):
    """All milestones for ``project`` with derived progress."""
    return [milestone_progress(m) for m in project.milestones.all()]


def epic_rollup(project):
    """Per-epic rollup: member tickets plus delivered-vs-open points."""
    from .models import Epic

    rows = []
    for epic in Epic.objects.filter(project=project):
        members = list(epic.tickets.all())
        rows.append(
            {
                'epic': epic,
                'total': len(members),
                'accepted': sum(1 for t in members if t.state == Ticket.State.ACCEPTED),
                'open': sum(
                    1
                    for t in members
                    if t.state
                    in (
                        Ticket.State.UNSTARTED,
                        Ticket.State.STARTED,
                        Ticket.State.FINISHED,
                    )
                ),
                'points': sum(t.story_points or 0 for t in members),
            }
        )
    return rows
