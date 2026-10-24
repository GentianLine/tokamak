from django.db import models
from django.utils import timezone


class Project(models.Model):
    """A trackable project (an agile team's backlog container)."""

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    archived = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    # ------------------------------------------------------------------
    # Derived planning data (delegates to services so the REST API, the
    # Unicorn UI and automation all share one implementation).
    # ------------------------------------------------------------------
    @property
    def backlog_points(self):
        from .services import backlog_points

        return backlog_points(self)

    @property
    def average_velocity(self):
        from .services import average_velocity

        return average_velocity(self)


class Epic(models.Model):
    """A named grouping of tickets for scope organization.

    A lightweight umbrella that labels
    related tickets so the backlog can be understood at a glance. Epics are
    per-project and carry no workflow state — membership is manual via
    ``Ticket.epic`` (service layer: ``tokamak/services.py::ticket_set_epic``).
    """

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='epics')
    name = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f'{self.project.name} — {self.name}'


class Milestone(models.Model):
    """A date-targeted delivery goal: what ships by when.

    Tickets are attached to at most one milestone; the board groups by it so
    the team sees what ships when. Completion is derived, never stored:
    a milestone is done exactly when every attached ticket is accepted.
    """

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name='milestones'
    )
    name = models.CharField(max_length=200)
    due_date = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['due_date', 'name']

    def __str__(self):
        return f'{self.project.name} — {self.name}'

    @property
    def ticket_count(self):
        return self.tickets.count()

    @property
    def accepted_count(self):
        return self.tickets.filter(state=Ticket.State.ACCEPTED).count()

    @property
    def is_complete(self):
        """A milestone with no tickets attached counts as nothing yet."""
        return self.ticket_count > 0 and self.accepted_count == self.ticket_count


class Iteration(models.Model):
    """A sprint / iteration with a fixed start and end (the "cutoff")."""

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name='iterations'
    )
    number = models.PositiveIntegerField()
    name = models.CharField(max_length=200, blank=True)
    goal = models.TextField(blank=True)
    start_date = models.DateField()
    end_date = models.DateField()

    class Meta:
        ordering = ['number']
        unique_together = [('project', 'number')]

    def __str__(self):
        return f'{self.project.name} - Iteration {self.number}'

    @property
    def is_completed(self):
        return self.end_date < timezone.localdate()

    @property
    def is_active(self):
        today = timezone.localdate()
        return self.start_date <= today <= self.end_date

    @property
    def accepted_points(self):
        agg = self.tickets.filter(state=Ticket.State.ACCEPTED).aggregate(
            total=models.Sum('story_points')
        )
        return agg['total'] or 0

    @property
    def committed_points(self):
        agg = self.tickets.aggregate(total=models.Sum('story_points'))
        return agg['total'] or 0


class Ticket(models.Model):
    """A single ticket: feature, chore or bug moving through the board."""

    class Type(models.TextChoices):
        FEATURE = 'feature', 'Feature'
        CHORE = 'chore', 'Chore'
        BUG = 'bug', 'Bug'

    class State(models.TextChoices):
        UNSTARTED = 'unstarted', 'Unstarted'
        STARTED = 'started', 'Started'
        FINISHED = 'finished', 'Finished'
        ACCEPTED = 'accepted', 'Accepted'
        REJECTED = 'rejected', 'Rejected'

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name='tickets'
    )
    iteration = models.ForeignKey(
        Iteration,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='tickets',
    )
    epic = models.ForeignKey(
        Epic,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='tickets',
        help_text='Optional epic grouping (service: ticket_set_epic).',
    )
    milestone = models.ForeignKey(
        Milestone,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='tickets',
        help_text='Optional delivery milestone (service: ticket_set_milestone).',
    )
    name = models.CharField(max_length=300)
    description = models.TextField(blank=True)
    type = models.CharField(max_length=20, choices=Type.choices, default=Type.FEATURE)
    state = models.CharField(
        max_length=20, choices=State.choices, default=State.UNSTARTED, db_index=True
    )
    story_points = models.PositiveIntegerField(
        null=True, blank=True, help_text='Fibonacci estimate: 1, 2, 3, 5, 8, 13, 21'
    )
    external_ref = models.CharField(
        max_length=100,
        blank=True,
        help_text='Optional id from an external system (automation).',
    )
    position = models.PositiveIntegerField(
        default=0, help_text='Ordering within a column.'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['position', 'created_at']

    def __str__(self):
        return self.name

    @property
    def is_estimated(self):
        return self.story_points is not None

    @property
    def is_done(self):
        return self.state in (self.State.ACCEPTED, self.State.REJECTED)

    # ------------------------------------------------------------------
    # Blockers (red-flag affordance; edges live in Blocker, the
    # many-to-many relationship table. Derived reads only — mutations go
    # through tokamak/services.py::add_blocker / remove_blocker.)
    # ------------------------------------------------------------------
    @property
    def blockers(self):
        """The tickets this one blocks (targets of its outgoing edges)."""
        return [b.target for b in self.outgoing_blockers.select_related('target')]

    @property
    def blocked_by(self):
        """The tickets blocking this one (its incoming edges)."""
        return [b.source for b in self.incoming_blockers.select_related('source')]

    @property
    def is_blocked(self):
        """Blocked flag: at least one unresolved blocker upstream.

        A blocker only counts while the blocking ticket is live: an edge
        whose blocker was accepted (auto-cleared) or rejected and archived
        does not hold the target down.
        """
        return self.incoming_blockers.exclude(
            source__state__in=(
                self.State.ACCEPTED,
                self.State.REJECTED,
            )
        ).exists()

    @property
    def blocker_reasons(self):
        """Non-empty reasons from the incoming blockers, in creation order."""
        return [
            b.reason
            for b in self.incoming_blockers.exclude(reason='').order_by('created_at')
            if b.reason
        ]


class Blocker(models.Model):
    """One edge of the blocks/blocked_by relationship between tickets.

    ``source`` is the blocker ticket, ``target`` the blocked one: *source*
    blocks *target* (equivalently, target is blocked by source). The pair
    plus an optional ``reason`` describing why the target is held down;
    service-layer add/remove (``tokamak/services.py``) enforces acyclic,
    non-self, same-project edges and fires ``blocker_added`` /
    ``blocker_removed``.
    """

    source = models.ForeignKey(
        Ticket, on_delete=models.CASCADE, related_name='outgoing_blockers'
    )
    target = models.ForeignKey(
        Ticket, on_delete=models.CASCADE, related_name='incoming_blockers'
    )
    reason = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('source', 'target')]
        ordering = ['created_at']

    def __str__(self):
        return f'#{self.source_id} blocks #{self.target_id}'

    @property
    def blocked_ticket(self):
        """The ticket on the receiving end (alias for template clarity)."""
        return self.target


class EventLog(models.Model):
    """
    Append-only audit trail written by the signal receivers.

    This is what automated processors can subscribe to (now: a DB row; later:
    a webhook/queue) whenever a project or ticket state changes.
    """

    name = models.CharField(max_length=100, db_index=True)
    project = models.ForeignKey(
        Project, null=True, blank=True, on_delete=models.CASCADE, related_name='events'
    )
    ticket = models.ForeignKey(
        Ticket, null=True, blank=True, on_delete=models.CASCADE, related_name='events'
    )
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.name} @ {self.created_at:%Y-%m-%d %H:%M}'


class MemorySession(models.Model):
    """One clm-mem chat/memory session attached to a ticket attempt.

    Lifecycle (see INTEGRATION.md "Session lifecycle"): OPEN -> ENDED. A
    session opens when work starts on the ticket, is monitored via
    ``monitor_url`` (web-studio workspace in served mode, bridge snapshot
    route + mirrored context files in local mode), and ends deterministically
    — explicit close, or the ticket leaving the active lane. On end the live
    memory offloads into the ticket-scoped archive and a distilled summary
    flows back (``summary``); nothing else mutates the ticket silently.
    """

    class Status(models.TextChoices):
        OPEN = 'open', 'Open'
        ENDED = 'ended', 'Ended'

    ticket = models.ForeignKey(
        Ticket, on_delete=models.CASCADE, related_name='memory_sessions'
    )
    session_key = models.CharField(max_length=200, db_index=True)
    mode = models.CharField(max_length=10, default='local')
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.OPEN
    )
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    end_reason = models.CharField(max_length=50, blank=True)
    monitor_url = models.CharField(max_length=500, blank=True)
    summary = models.TextField(blank=True)
    payload = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-started_at']

    def __str__(self):
        return f'{self.session_key} ({self.status}) @ {self.started_at:%Y-%m-%d %H:%M}'
