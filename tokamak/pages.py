"""HTML page views beyond the Unicorn components (new module, merge-friendly).

Currently hosts the ticket detail surface that the board links to. It reads
only — every mutation stays in ``tokamak/services.py`` per project convention;
the actions on the detail card navigate back to the board where the reactive
Unicorn component performs the actual transition.

Routes (registered in ``tokamak/urls.py``):
    GET|POST /projects/new/              project_create   name='project-create'
    GET|POST /projects/<pk>/tickets/new/ ticket_create    name='ticket-create'
    GET /tickets/<pk>/                   ticket_detail    name='ticket-detail'
    GET /projects/<pk>/done/             project_done     name='project-done'

Creation pages are plain request/response (no Unicorn): the markdown editor
widget talks to them, and every write still lands through ``services``.
"""

from urllib.parse import quote_plus

from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import ensure_csrf_cookie

from . import services
from .models import EventLog, Project, Ticket


@ensure_csrf_cookie
def ticket_detail(request, pk):
    """Render a single ticket with its description and event timeline.

    POST here performs the blocker/grouping mutations for this surface
    (add_blocker / remove_blocker / set_epic / set_milestone); every write
    still lands through ``tokamak/services.py``."""
    ticket = get_object_or_404(
        Ticket.objects.select_related(
            'project', 'iteration', 'epic', 'milestone'
        ).prefetch_related(
            'events', 'incoming_blockers__source', 'outgoing_blockers__target'
        ),
        pk=pk,
    )

    if request.method == 'POST':
        return ticket_detail_action(request, ticket)

    events = list(ticket.events.order_by('-created_at')[:30])
    blocking = [
        b
        for b in ticket.incoming_blockers.select_related('source')
        if b.source.state not in (Ticket.State.ACCEPTED, Ticket.State.REJECTED)
    ]
    context = {
        'ticket': ticket,
        'project': ticket.project,
        'events': events,
        'sessions': ticket.memory_sessions.order_by('-started_at')[:5],
        'blocking': blocking,
        'blocked_by_me': [
            b.target for b in ticket.outgoing_blockers.select_related('target')
        ],
        'candidate_blockers': [
            c
            for c in ticket.project.tickets.exclude(
                state__in=(Ticket.State.ACCEPTED, Ticket.State.REJECTED)
            ).order_by('position', 'pk')
            if c.pk != ticket.pk and c not in ticket.blockers
        ],
        'epics': ticket.project.epics.all(),
        'milestones': ticket.project.milestones.all(),
    }
    return render(request, 'tokamak/ticket.html', context)


def ticket_detail_action(request, ticket):
    """Handle the POST actions on a ticket detail card (blockers/epics/
    milestones). Redirects back to the same page; errors flash via query."""
    action = request.POST.get('action', '')
    try:
        if action == 'add_blocker':
            source = get_object_or_404(Ticket, pk=request.POST.get('by'))
            services.add_blocker(source, ticket, reason=request.POST.get('reason', ''))
        elif action == 'remove_blocker':
            source = get_object_or_404(Ticket, pk=request.POST.get('by'))
            services.remove_blocker(source, ticket)
        elif action == 'set_epic':
            epic_name = (request.POST.get('epic') or '').strip()
            if epic_name:
                Epic = services.epic_model()
                epic, _ = Epic.objects.get_or_create(
                    project=ticket.project, name=epic_name[:200]
                )
            else:
                epic = None
            services.ticket_set_epic(ticket, epic)
        elif action == 'set_milestone':
            milestone_name = (request.POST.get('milestone') or '').strip()
            if milestone_name:
                Milestone = services.milestone_model()
                milestone, _ = Milestone.objects.get_or_create(
                    project=ticket.project, name=milestone_name[:200]
                )
            else:
                milestone = None
            services.ticket_set_milestone(ticket, milestone)
        else:
            return redirect(f'/tickets/{ticket.pk}/?action=unknown')
    except Exception as exc:  # flash the reason on reload
        message = getattr(exc, 'message', None) or str(exc)
        return redirect(f'/tickets/{ticket.pk}/?error={quote_plus(message[:200])}')
    return redirect(f'/tickets/{ticket.pk}/')


@ensure_csrf_cookie
def project_done(request, pk):
    """Done archive: accepted/rejected tickets grouped by iteration.

    The linear board only shows live work; delivering a ticket (accept or
    reject) moves it off the board and onto this page. Read-only view —
    mutations stay in ``tokamak/services.py``.
    """
    project = get_object_or_404(Project, pk=pk)

    done = list(
        project.tickets.filter(
            state__in=(Ticket.State.ACCEPTED, Ticket.State.REJECTED)
        ).select_related('iteration')
    )

    groups = []
    for iteration in sorted(project.iterations.all(), key=lambda it: -it.number):
        members = [t for t in done if t.iteration_id == iteration.id]
        if members:
            members.sort(key=lambda t: (t.state != Ticket.State.ACCEPTED, t.position))
            groups.append(
                {
                    'iteration': iteration,
                    'tickets': members,
                    'accepted_points': sum(
                        t.story_points or 0
                        for t in members
                        if t.state == Ticket.State.ACCEPTED
                    ),
                }
            )
    loose = [t for t in done if t.iteration_id is None]
    if loose:
        loose.sort(key=lambda t: (t.state != Ticket.State.ACCEPTED, t.position))
        groups.append({'iteration': None, 'tickets': loose, 'accepted_points': 0})

    accepted = sum(
        t.story_points or 0 for t in done if t.state == Ticket.State.ACCEPTED
    )
    rejected = sum(
        t.story_points or 0 for t in done if t.state == Ticket.State.REJECTED
    )
    context = {
        'project': project,
        'groups': groups,
        'accepted_count': sum(1 for t in done if t.state == Ticket.State.ACCEPTED),
        'rejected_count': sum(1 for t in done if t.state == Ticket.State.REJECTED),
        'accepted_points': accepted,
        'rejected_points': rejected,
    }
    return render(request, 'tokamak/done.html', context)


# ---------------------------------------------------------------------------
# Creation pages: markdown-first forms for projects and tickets.
#
# The ticket form adapts to its story type: a chore is nothing but a
# description (name + points fields fade out client-side and the service
# layer enforces it server-side), while features and bugs want a title and
# an optional Fibonacci estimate.
# ---------------------------------------------------------------------------

_EDITOR_ATTRS = {'rows': 12, 'data-md-editor': ''}


class ProjectCreateForm(forms.Form):
    name = forms.CharField(
        max_length=200, widget=forms.TextInput(attrs={'autofocus': True})
    )
    description = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs=dict(_EDITOR_ATTRS)),
    )


class TicketCreateForm(forms.Form):
    name = forms.CharField(max_length=300, required=False)
    type = forms.ChoiceField(choices=Ticket.Type.choices)
    story_points = forms.ChoiceField(required=False, choices=[])
    description = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs=dict(_EDITOR_ATTRS, rows=14)),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['story_points'].choices = [('', 'Unestimated')] + [
            (str(p), f'{p} pts') for p in settings.TOKAMAK['FIBONACCI_POINTS']
        ]

    def clean_story_points(self):
        raw = self.cleaned_data.get('story_points') or ''
        return int(raw) if raw else None

    def clean(self):
        cleaned = super().clean()
        if (
            cleaned.get('type') == Ticket.Type.CHORE
            and not (cleaned.get('description') or '').strip()
        ):
            self.add_error('description', 'A chore needs a description.')
        return cleaned


def _merge_errors(form, exc):
    """Fold a service-layer ValidationError back into the rendered form."""
    message_dict = getattr(exc, 'message_dict', None) or {
        None: list(getattr(exc, 'messages', None) or [str(exc)])
    }
    for field, msgs in message_dict.items():
        target = field if field != '__all__' and field in form.fields else None
        for msg in msgs:
            form.add_error(target, msg)


@ensure_csrf_cookie
def project_create(request):
    """Dedicated creation page for projects."""
    form = ProjectCreateForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        try:
            project = services.create_project(
                form.cleaned_data['name'],
                form.cleaned_data['description'],
            )
        except ValidationError as exc:
            _merge_errors(form, exc)
        else:
            return redirect(f"/projects/{project.pk}/", permanent=False)
    return render(request, 'tokamak/project_new.html', {'form': form})


@ensure_csrf_cookie
def ticket_create(request, pk):
    """Dedicated creation page for a ticket inside ``pk``'s project."""
    project = get_object_or_404(Project, pk=pk)
    initial = {}
    if request.method == 'GET':
        guess = request.GET.get('type')
        if guess in Ticket.Type.values:
            initial['type'] = guess
    form = TicketCreateForm(request.POST or None, initial=initial or None)
    if request.method == 'POST' and form.is_valid():
        try:
            ticket = services.create_ticket(
                project,
                name=form.cleaned_data['name'],
                description=form.cleaned_data['description'],
                type=form.cleaned_data['type'],
                story_points=form.cleaned_data['story_points'],
            )
        except ValidationError as exc:
            _merge_errors(form, exc)
        else:
            return redirect(f"/tickets/{ticket.pk}/", permanent=False)
    context = {'form': form, 'project': project}
    return render(request, 'tokamak/ticket_new.html', context)
