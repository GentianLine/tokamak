"""Shared fixtures for the tokamak test-suite.

Database setup/teardown is handled by pytest-django; helpers live in
``tests/_utils.py`` so individual test modules import them directly rather
than reaching into conftest.
"""

import pytest
from rest_framework.test import APIClient

from tests._utils import make_project


@pytest.fixture
def project(db):
    """A fresh project in the test database."""
    return make_project()


@pytest.fixture
def api_client(db):
    """An unauthenticated DRF test client."""
    return APIClient()
