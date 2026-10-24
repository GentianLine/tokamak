"""Reusable object factories shared across the tokamak test-suite.

Each helper creates and returns a fresh model instance; callers are responsible
for any additional state (tickets, estimates, transitions) required by a
given test.
"""

from datetime import timedelta

from tokamak.models import Iteration, Project


def make_project(name='P'):
    """Create and return a fresh :class:`~tokamak.models.Project`."""
    return Project.objects.create(name=name)


def make_iteration(project, number, start, length=14):
    """Create an iteration of ``length`` days starting on ``start``."""
    return Iteration.objects.create(
        project=project,
        number=number,
        start_date=start,
        end_date=start + timedelta(days=length - 1),
    )
