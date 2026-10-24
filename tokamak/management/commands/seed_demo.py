"""Seed demo data: a project with 4 completed iterations of history,
one active iteration, and a backlog — enough for the cutoff estimator.

Run: python manage.py seed_demo
"""

import random
from datetime import date, timedelta

from django.core.management.base import BaseCommand

from tokamak import services
from tokamak.models import Iteration, Project, Ticket

FEATURES = [
    'Drag-and-drop ticket reordering',
    'Email notifications on assignment',
    'CSV export of accepted stories',
    'Dark mode',
    'Two-factor auth',
    'Search with filters',
    'Slack integration',
    'Recurring chores',
    'Kanban swimlanes per epic',
    'Bulk estimate dialog',
    'Cycle-time report',
    'Mobile responsive board',
    'Webhooks for external tools',
    'Migrate from the old board',
    'Granular permissions',
    'Audit log viewer',
    'Velocity anomaly alerts',
    'Sprint template library',
]


class Command(BaseCommand):
    help = 'Create demo projects, iterations and tickets with signal-firing writes.'

    def handle(self, *args, **options):
        random.seed(7)
        if Project.objects.filter(name='Website Relaunch').exists():
            self.stdout.write('Demo data already present; skipping.')
            return

        project = Project.objects.create(
            name='Website Relaunch',
            description='Marketing site rebuild with automation-friendly triage.',
        )
        services.save_project(project)  # fires project_updated → EventLog

        today = date.today()
        length = 14
        iteration_defs = []
        for n in range(1, 6):
            if n < 5:
                start = today - timedelta(days=length * (5 - n) + length)
                end = start + timedelta(days=length - 1)  # completed
            else:
                start, end = today - timedelta(days=3), today + timedelta(
                    days=10
                )  # active
            iteration_defs.append(
                Iteration.objects.create(
                    project=project,
                    number=n,
                    start_date=start,
                    end_date=end,
                    goal=f'Deliver iteration {n} scope',
                )
            )

        # Historical velocity ~8-13 points accepted per completed iteration.
        names = list(FEATURES)
        random.shuffle(names)
        idx = 0
        for it in iteration_defs[:4]:
            target = random.choice([8, 9, 10, 11, 12, 13])
            placed = 0
            while placed < target and idx < len(names):
                pts = random.choice([1, 2, 3, 3, 5, 5, 8])
                t = services.create_ticket(
                    project, name=names[idx], type=Ticket.Type.FEATURE, iteration=it
                )
                services.estimate_ticket(t, pts)
                for state in (
                    Ticket.State.STARTED,
                    Ticket.State.FINISHED,
                    Ticket.State.ACCEPTED,
                ):
                    services.transition_ticket(t, state)
                placed += pts
                idx += 1
            # one bug left unfinished to make the data realistic
            if it.number == 3:
                t = services.create_ticket(
                    project,
                    name='Board glitches on rapid clicks',
                    type=Ticket.Type.BUG,
                    iteration=it,
                )
                services.estimate_ticket(t, 3)
                services.transition_ticket(t, Ticket.State.STARTED)

        # Active iteration (in progress work).
        active = iteration_defs[4]
        for name, pts in [('Realtime board sync', 5), ('Fix sprint date math', 2)]:
            t = services.create_ticket(
                project, name=name, type=Ticket.Type.FEATURE, iteration=active
            )
            services.estimate_ticket(t, pts)
            services.transition_ticket(t, Ticket.State.STARTED)

        # Backlog with a mix of estimates and unestimated items.
        for name in names[idx : idx + 8]:
            t = services.create_ticket(
                project,
                name=name,
                type=random.choice([Ticket.Type.FEATURE] * 3 + [Ticket.Type.BUG]),
            )
            if random.random() < 0.75:
                services.estimate_ticket(t, random.choice([1, 2, 3, 5, 8]))
        services.create_ticket(
            project, name='Rotate API keys', type=Ticket.Type.CHORE
        )  # auto-estimated by signal hook

        # Epics + milestones: group the backlog so the board header shows
        # scope rollups for the board header.
        epic_auth = services.create_epic(project, 'Auth & Accounts')
        epic_board = services.create_epic(project, 'Board UX')
        ms_beta = services.create_milestone(
            project, 'Beta', due_date=date.today() + timedelta(days=28)
        )
        backlog = list(project.tickets.filter(iteration__isnull=True))[:6]
        for n, t in enumerate(backlog):
            epic = epic_auth if n % 2 else epic_board
            services.ticket_set_epic(t, epic)
        live_active = list(project.tickets.filter(state=Ticket.State.STARTED)[:3])
        for t in live_active:
            services.ticket_set_milestone(t, ms_beta)

        # One blocker edge: the active bug blocks a backlog story
        # ("waiting on the fix"), so the board shows a red flag.
        blocked_bug = project.tickets.filter(
            type=Ticket.Type.BUG, state=Ticket.State.STARTED
        ).first()
        if blocked_bug and backlog:
            services.add_blocker(blocked_bug, backlog[0], reason='waiting on this fix')

        self.stdout.write(
            self.style.SUCCESS(
                f'Seeded project "{project.name}" with {project.tickets.count()} tickets, '
                f'{project.iterations.count()} iterations.'
            )
        )
