"""Shared fixtures for the tokamak test-suite.

Database setup/teardown is handled by pytest-django; helpers live in
``tests/_utils.py`` so individual test modules import them directly rather
than reaching into conftest.
"""

import pytest
from django.contrib.auth.models import User
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from tokamak import webhooks
from tests._utils import make_project


@pytest.fixture(autouse=True)
def _drain_webhook_pool():
    """Drain any in-flight async webhook deliveries after each test.

    The production default is async (a thread pool) so a slow target cannot
    block board writes. Tests that assert on delivered records configure their
    targets with ``async: False`` (see ``hook_cfg``), but a stray worker from an
    earlier test could still land late and pollute the next one's handler state.
    Draining at teardown keeps delivery deterministic across the suite without
    overriding per-test config. We also disconnect any fan-out receiver a test
    registered, so it cannot fire on unrelated board writes in later tests.
    """
    yield
    webhooks.deregister_fanout()
    webhooks.drain_pool()


@pytest.fixture
def auth_user(db):
    """A real user used to authenticate the default test clients.

    The API surface requires authentication (DRF ``IsAuthenticated`` for the
    REST views, Django ``@login_required`` for the JSON page endpoints), so the
    suite's default clients are authenticated by default. Tests that need to
    exercise the anonymous path can build their own unauthenticated client.
    """
    return User.objects.create_user(
        username='tester', password='test-pass-123', email='tester@example.com'
    )


@pytest.fixture
def client(client, auth_user):
    """A logged-in Django test client (satisfies ``@login_required`` views)."""
    client.force_login(auth_user)
    return client


@pytest.fixture
def api_client(db, auth_user):
    """An authenticated DRF test client.

    The API surface is protected two ways: DRF ``IsAuthenticated`` on the REST
    viewsets (satisfied by the token below) and Django ``@login_required`` on
    the JSON page endpoints such as ``/api/reorder/``. A plain token cannot
    satisfy the session-based decorator, so we also force-login the user —
    making this client pass both authentication paths.
    """
    apic = APIClient(force_authenticate=(auth_user, None))
    apic.force_login(auth_user)
    token = Token.objects.create(user=auth_user)
    apic.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')
    return apic
