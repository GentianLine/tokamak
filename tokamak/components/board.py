"""Unicorn board component: the interactive ticket stream.

The board is a single Tokamak stream — one ordered list, not a kanban of
columns: started work sits at the top, delivered/in-review work next, then
the unstarted backlog, cut by a red sprint line at the average velocity.
Accepted/rejected tickets leave the board entirely and live on the done
archive page (``tokamak/pages.py::project_done``).

All mutations call ``tokamak.services`` — the exact same path the REST API
takes — so signals fire identically from UI clicks and automation.
"""

from django_unicorn.components import UnicornView

from .. import services
from ..models import Project, Ticket
from ..reorder import reorder_ticket as _reorder_service

POINT_CHOICES = [1, 2, 3, 5, 8, 13]

# Display rank within the linear stream: active work first (stream order),
# then delivered-but-unaccepted ("in review"), then the backlog.
STATE_RANK = {
    Ticket.State.STARTED: 0,
    Ticket.State.FINISHED: 1,
    Ticket.State.UNSTARTED: 2,
}

# States that leave the board and appear on the done archive instead.
ARCHIVED_STATES = (Ticket.State.ACCEPTED, Ticket.State.REJECTED)

BACKWARD = {
    Ticket.State.STARTED: Ticket.State.UNSTARTED,
    Ticket.State.FINISHED: Ticket.State.STARTED,
    Ticket.State.ACCEPTED: Ticket.State.FINISHED,
    Ticket.State.REJECTED: Ticket.State.STARTED,
}


class BoardView(UnicornView):
    template_name = 'unicorn/board.html'

    project_id: int = None
    new_ticket_name: str = ''
    new_ticket_type: str = Ticket.Type.FEATURE
    error: str = ''

    point_choices = POINT_CHOICES
    type_choices = Ticket.Type.choices

    # ------------------------------------------------------------------
    def mount(self):
        pass  # columns/forecast are computed lazily on every render.

    # NOTE: computed values are plain methods, not properties. Unicorn
    # serializes every public non-callable attribute into its component data
    # and re-sets it on each request, which would break read-only properties.
    # Django templates call no-arg methods transparently, so the template
    # syntax is unchanged ({% for row in rows %}, {{ line.points }}).
    def project(self) -> Project:
        if not self.project_id:
            return None
        return Project.objects.get(pk=self.project_id)

    def candidates(self):
        """Map ticket_id -> list of live tickets it could be blocked by.

        Candidates are non-archived tickets in the project that do not
        already block this ticket. The board template uses them for the
        inline 'block' picker (one select per row, no page reload)."""
        project = self.project()
        if project is None:
            return {}
        live = list(
            project.tickets.exclude(state__in=ARCHIVED_STATES).order_by(
                'position', 'pk'
            )
        )
        from ..models import Blocker

        edges = {
            (e.source_id, e.target_id)
            for e in Blocker.objects.filter(source__project=project)
        }
        result = {}
        for t in live:
            opts = [
                {'id': c.pk, 'name': c.name}
                for c in live
                if c.pk != t.pk and (c.pk, t.pk) not in edges
            ]
            result[t.pk] = opts[:12]
        return result

    def grouping(self):
        """Epic + milestone rollups for the board header (derived reads).

        Epics are manual groupings; a milestone counts as done only when all
        its attached tickets are accepted. Both stay presentation-only --
        membership changes go through tokamak/services."""
        project = self.project()
        if project is None:
            return {'epics': [], 'milestones': []}
        return {
            'epics': services.epic_rollup(project),
            'milestones': services.project_milestones(project),
        }

    def stream(self):
        """Linear board model: ordered rows cut by the sprint line.

        Returns ``{'rows': [...], 'line': {...}}`` where each row carries a
        ticket plus display metadata, and ``line`` holds the capacity line
        (points, dotted flag, basis) plus the split index — the number of
        rows that fit above it.
        """
        project = self.project()
        if project is None:
            return {'rows': [], 'line': None}

        tickets = list(
            project.tickets.exclude(state__in=ARCHIVED_STATES)
            .select_related('iteration')
            .order_by()
        )
        # Stream ordering: started at the top, then in-review, then backlog.
        tickets.sort(
            key=lambda t: (
                STATE_RANK.get(t.state, 9),
                t.position,
                t.pk,
            )
        )

        line = services.sprint_line(project)
        candidates = self.candidates()
        rows = []
        running = 0.0
        index = 0  # count of rows whose cumulative points still fit the line
        for ticket in tickets:
            points = ticket.story_points or 0
            if running + points <= line['points']:
                index += 1
            running += points
            rows.append(
                {
                    'ticket': ticket,
                    'state': ticket.state,
                    'label': Ticket.State(ticket.state).label,
                    'points': ticket.story_points,
                    'running': running,
                    # Red flag: unresolved blockers upstream.
                    'blocked': ticket.is_blocked,
                    'blocker_reasons': ticket.blocker_reasons,
                    # Live blocker sources for the inline resolve buttons.
                    'blocking': [
                        {'id': b.source_id, 'name': b.source.name}
                        for b in ticket.incoming_blockers.select_related('source')
                        if b.source.state
                        not in (
                            Ticket.State.ACCEPTED,
                            Ticket.State.REJECTED,
                        )
                    ],
                    # Picker options for the inline 'block' control.
                    'candidates': candidates.get(ticket.pk, []),
                }
            )
        line['index'] = index
        for i, row in enumerate(rows):
            row['above_line'] = i < index
        return {'rows': rows, 'line': line}

    def done_count(self):
        """How many tickets live on the archive page instead of the board."""
        project = self.project()
        if project is None:
            return 0
        return project.tickets.filter(state__in=ARCHIVED_STATES).count()

    def forecast(self):
        project = self.project()
        if project is None:
            return {}
        return services.estimate_cutoff(project)

    def reorder(self, ticket_id, target_position):
        """Drag-reorder within the ticket's display group (see
        ``tokamak/reorder.py``); ordering is persisted via the service."""
        try:
            _reorder_service(Ticket.objects.get(pk=ticket_id), int(target_position))
            self.error = ''
        except Exception as exc:
            self.error = _friendly(exc)

    # ------------------------------------------------------------------
    # Actions (each one goes through the service layer → signals)
    # ------------------------------------------------------------------
    def add_ticket(self):
        try:
            services.create_ticket(
                self.project(), name=self.new_ticket_name, type=self.new_ticket_type
            )
            self.new_ticket_name = ''
            self.error = ''
        except Exception as exc:  # ValidationError etc.
            self.error = _friendly(exc)

    def advance(self, ticket_id):
        ticket = Ticket.objects.get(pk=ticket_id)
        forward = {
            Ticket.State.UNSTARTED: Ticket.State.STARTED,
            Ticket.State.STARTED: Ticket.State.FINISHED,
            Ticket.State.FINISHED: Ticket.State.ACCEPTED,
            Ticket.State.REJECTED: Ticket.State.STARTED,
        }
        self._transition(ticket, forward.get(ticket.state))

    def rewind(self, ticket_id):
        ticket = Ticket.objects.get(pk=ticket_id)
        self._transition(ticket, BACKWARD.get(ticket.state))

    def move_to(self, ticket_id, state):
        """Drag-and-drop target: walk the ticket to ``state`` hop by hop."""
        ticket = Ticket.objects.get(pk=ticket_id)
        for hop in services.transition_path(ticket.state, state) or []:
            self._transition(ticket, hop)
            if self.error:
                return
            ticket.refresh_from_db()

    def set_points(self, ticket_id, points):
        try:
            services.estimate_ticket(Ticket.objects.get(pk=ticket_id), int(points))
            self.error = ''
        except Exception as exc:
            self.error = _friendly(exc)

    def delete_ticket(self, ticket_id):
        Ticket.objects.filter(pk=ticket_id).delete()

    # ------------------------------------------------------------------
    # Blockers (red-flag affordance): each mutation goes through services
    # so the REST API, automation and direct calls share validation +
    # signals.
    # ------------------------------------------------------------------
    def add_blocker(self, target_id, source_id):
        """Declare that ticket ``source_id`` blocks ``target_id``. The
        inline board flow uses a blank reason; the detail page offers one."""
        try:
            source = Ticket.objects.get(pk=source_id)
            target = Ticket.objects.get(pk=target_id)
            services.add_blocker(source, target)
            self.error = ''
        except Exception as exc:
            self.error = _friendly(exc)

    def remove_blocker(self, target_id, source_id):
        try:
            source = Ticket.objects.get(pk=source_id)
            target = Ticket.objects.get(pk=target_id)
            services.remove_blocker(source, target)
            self.error = ''
        except Exception as exc:
            self.error = _friendly(exc)

    # ------------------------------------------------------------------
    def _transition(self, ticket, target):
        if target is None:
            return
        try:
            user = getattr(self.request, 'user', None) if self.request else None
            services.transition_ticket(
                ticket, target, user=user if user and user.is_authenticated else None
            )
            self.error = ''
        except Exception as exc:
            self.error = _friendly(exc)


def _friendly(exc):
    message = getattr(exc, 'message', None) or str(exc)
    return message[:300]
