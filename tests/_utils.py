"""Reusable object factories shared across the tokamak test-suite.

Each helper creates and returns a fresh model instance; callers are responsible
for any additional state (tickets, estimates, transitions) required by a
given test.
"""

from datetime import timedelta

from django.contrib.auth.models import User
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

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


def make_authed_client(username='tester'):
    """Return an :class:`APIClient` authenticated for the protected endpoints.

    The JSON page endpoints (``/api/memory/*``, ``/api/sessions/*``,
    ``/api/reorder/``) are guarded by Django ``@login_required`` while the REST
    viewsets use DRF ``IsAuthenticated``. A token alone cannot satisfy the
    session-based decorator, so we create a user, force-login (session path)
    and also attach a token header (DRF path). Tests that need to hit those
    routes inline should build their client this way rather than with a bare
    ``APIClient()``.
    """
    user, _ = User.objects.get_or_create(
        username=username,
        defaults={'email': f'{username}@example.com'},
    )
    if not user.has_usable_password():
        user.set_password('test-pass-123')
        user.save()
    client = APIClient()
    client.force_login(user)
    token = Token.objects.get_or_create(user=user)[0]
    client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')
    return client
