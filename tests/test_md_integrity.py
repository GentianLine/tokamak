"""Explicit-reference integrity markers (md-ref-integrity).

Decision recorded here: the existence rule for *bare* refs stays as-is
(unresolvable ``#12`` never links — prose like "color #f5f5f5" must not
become markup), but *explicit* references are deliberate, so a dangling
target is now flagged (class ``stale`` + explanatory title) instead of
linking silently into the void. Deleting a ticket through the service layer
therefore surfaces in every description that pointed at it on purpose.
"""

import pytest

from tokamak import markdown, services
from tokamak.models import Ticket

from tests._utils import make_project


@pytest.mark.django_db
def test_explicit_ref_to_existing_ticket_links_clean(db):
    project = make_project('Integ')
    ticket = services.create_ticket(project, name='Alive target')
    html = markdown.render(f'[See it](ticket:{ticket.pk})')
    assert f'/tickets/{ticket.pk}/' in html
    assert 'stale' not in html
    assert '#%d · Alive target' % ticket.pk in html  # hover title from label


@pytest.mark.django_db
def test_explicit_ref_to_missing_ticket_is_flagged(db):
    html = markdown.render('[Ghost](ticket:9999)')
    assert 'class="tokamak-ref stale"' in html
    assert 'ticket 9999: missing (deleted or never existed)' in html
    assert 'href="/tickets/9999/"' in html  # still links, per the contract


@pytest.mark.django_db
def test_project_scheme_flagged_the_same_way(db):
    project = make_project('Integ proj')
    clean = markdown.render(f'[Go](project:{project.pk})')
    assert 'stale' not in clean
    assert f'project {project.pk} · Integ proj' in clean

    dangling = markdown.render('[Nope](proj:7777)')
    assert 'class="tokamak-ref stale"' in dangling
    assert 'project 7777: missing (deleted or never existed)' in dangling


@pytest.mark.django_db
def test_bare_refs_keep_the_existence_rule(db):
    """Unresolvable bare refs stay plain text — no anchor is minted."""
    html = markdown.render('prose with #99 and color #f5f5f5 inside')
    assert '<a' not in html


@pytest.mark.django_db
def test_deletion_surfaces_in_explicit_refs(db):
    """The full lifecycle: clean link, service-layer delete, stale flag."""
    project = make_project('Cycle')
    ticket = services.create_ticket(project, name='Doomed')
    src = f'[Pointing](ticket:{ticket.pk})'

    first = markdown.render(src)
    assert 'stale' not in first

    services.delete_ticket(ticket)
    assert not Ticket.objects.filter(pk=ticket.pk).exists()

    second = markdown.render(src)
    assert 'class="tokamak-ref stale"' in second
