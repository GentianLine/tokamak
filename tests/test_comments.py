"""Ticket threads — comments, service semantics, signals and REST surface.

New module per AGENTS.md lane rules (new behavior gets its own test file).
Semantics covered:

* comments are created/edited/removed only through ``tokamak/services.py``;
* removal is soft: the row survives as a tombstone, the body goes dark;
* every write mirrors into ``EventLog`` as comment.added/.updated/.removed;
* the REST surface (/comments/, /tickets/<pk>/thread|comment/,
  /projects/<pk>/threads/) behaves identically to service calls;
* thread automation (system notes on blocker events) is opt-in and never
  changes the default signal graph.
"""

import pytest
from django.core.exceptions import ValidationError
from django.test import override_settings

from tokamak import services
from tokamak.models import Comment, EventLog
from tokamak.signals import comment_added, comment_removed, comment_updated

from tests._utils import make_project

ACTIVE = {'auto_comment_blockers': True}


# ---------------------------------------------------------------------------
# Service semantics
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_add_comment_creates_row_and_event_log():
    """A comment lands on the thread and writes its own audit row."""
    project = make_project('Thr1')
    ticket = services.create_ticket(project, name='Discuss me')

    comment = services.add_comment(ticket, 'I estimate 3, not 5.', author='ana')

    assert Comment.objects.filter(ticket=ticket).count() == 1
    assert comment.body == 'I estimate 3, not 5.'
    assert comment.author == 'ana'
    assert comment.kind == Comment.Kind.HUMAN
    assert comment.deleted is False
    rows = EventLog.objects.filter(name='comment.added')
    assert rows.count() == 1
    payload = rows.first().payload
    assert payload['comment'] == comment.pk
    assert payload['author'] == 'ana'
    assert payload['body_head'].startswith('I estimate')
    # the audit row is pinned to both ticket and project
    assert rows.first().ticket_id == ticket.pk
    assert rows.first().project_id == project.pk


@pytest.mark.django_db
def test_add_comment_requires_words():
    """Whitespace-only bodies are refused, not silently dropped."""
    project = make_project('Thr2')
    ticket = services.create_ticket(project, name='Quiet')
    with pytest.raises(ValidationError):
        services.add_comment(ticket, '   ')
    assert Comment.objects.count() == 0


@pytest.mark.django_db
def test_add_comment_respects_length_cap():
    """The configured body cap is enforced by the service layer."""
    project = make_project('Thr3')
    ticket = services.create_ticket(project, name='Long')
    with override_settings(TOKAMAK_THREADS={'max_body_length': 16}):
        with pytest.raises(ValidationError):
            services.add_comment(ticket, 'x' * 17)
        ok = services.add_comment(ticket, 'x' * 16)
        assert len(ok.body) == 16


@pytest.mark.django_db
def test_add_comment_rejects_unknown_kind():
    """Only the declared kinds are writable."""
    project = make_project('Thr4')
    ticket = services.create_ticket(project, name='Kinded')
    with pytest.raises(ValidationError):
        services.add_comment(ticket, 'hi', kind='alien')


@pytest.mark.django_db
def test_thread_order_is_oldest_first():
    """The thread reads oldest first, matching the timeline metaphor."""
    project = make_project('Thr5')
    ticket = services.create_ticket(project, name='Ordered')
    a = services.add_comment(ticket, 'first')
    b = services.add_comment(ticket, 'second')
    thread = services.ticket_thread(ticket)
    assert [c.pk for c in thread] == [a.pk, b.pk]


@pytest.mark.django_db
def test_edit_comment_updates_and_audits():
    """Editing keeps the row, fires comment.updated with old/new bodies."""
    project = make_project('Thr6')
    ticket = services.create_ticket(project, name='Editable')
    comment = services.add_comment(ticket, 'original text', author='bo')

    edited = services.edit_comment(comment, 'rewritten text')
    assert edited.pk == comment.pk
    assert edited.body == 'rewritten text'

    row = EventLog.objects.get(name='comment.updated')
    assert row.payload['old_head'] == 'original text'
    assert row.payload['new_head'] == 'rewritten text'


@pytest.mark.django_db
def test_edit_comment_refuses_empty_and_removed():
    """A removed comment is frozen; an empty body never lands."""
    project = make_project('Thr7')
    ticket = services.create_ticket(project, name='Frozen')
    comment = services.add_comment(ticket, 'will be removed')
    services.delete_comment(comment)

    with pytest.raises(ValidationError):
        services.edit_comment(comment, 'too late')
    live = services.add_comment(ticket, 'live one')
    with pytest.raises(ValidationError):
        services.edit_comment(live, '')


@pytest.mark.django_db
def test_delete_comment_is_soft_and_idempotent():
    """Removal hides the body but keeps history queryable."""
    project = make_project('Thr8')
    ticket = services.create_ticket(project, name='Tombstone')
    comment = services.add_comment(ticket, 'regrettable opinion', author='zed')

    removed = services.delete_comment(comment)
    assert removed.deleted is True
    assert removed.deleted_at is not None
    assert Comment.objects.filter(pk=comment.pk).exists()
    assert services.ticket_thread(ticket) == []
    assert [c.pk for c in services.ticket_thread(ticket, True)] == [comment.pk]
    assert comment.visible_body == '[removed]'

    again = services.delete_comment(comment, reason='twice')
    assert again.deleted is True
    # Idempotent by design: the second removal is a no-op, so exactly one
    # audit row exists — and it still records who took the comment down.
    events = EventLog.objects.filter(name='comment.removed')
    assert events.count() == 1
    assert events.first().payload['reason'] == 'manual'


@pytest.mark.django_db
def test_signal_payload_kwarg_names():
    """Payload kwarg names are part of the contract (AGENTS.md §1)."""
    project = make_project('Thr9')
    ticket = services.create_ticket(project, name='Signals')
    seen = {}

    def spy_added(sender, comment, **kwargs):
        seen['added'] = comment.pk

    def spy_updated(sender, comment, old_body='', new_body='', **kwargs):
        seen['updated'] = (old_body, new_body)

    def spy_removed(sender, comment, reason='manual', **kwargs):
        seen['removed'] = reason

    comment_added.connect(spy_added, dispatch_uid='spy_added_thr')
    comment_updated.connect(spy_updated, dispatch_uid='spy_updated_thr')
    comment_removed.connect(spy_removed, dispatch_uid='spy_removed_thr')
    try:
        comment = services.add_comment(ticket, 'one')
        services.edit_comment(comment, 'two')
        services.delete_comment(comment, reason='cleanup')
    finally:
        comment_added.disconnect(spy_added, dispatch_uid='spy_added_thr')
        comment_updated.disconnect(spy_updated, dispatch_uid='spy_updated_thr')
        comment_removed.disconnect(spy_removed, dispatch_uid='spy_removed_thr')

    assert seen['added'] == comment.pk
    assert seen['updated'] == ('one', 'two')
    assert seen['removed'] == 'cleanup'


@pytest.mark.django_db
def test_comment_stats_flags_quiet_backlog():
    """Discussion health: quiet backlog counts live tickets without human words."""
    project = make_project('Stats1')
    loud = services.create_ticket(project, name='Talked about')
    quiet = services.create_ticket(project, name='Never mentioned')
    shipped = services.create_ticket(project, name='Shipped', story_points=3)
    services.transition_ticket(shipped, 'started')
    services.transition_ticket(shipped, 'finished')
    services.transition_ticket(shipped, 'accepted')

    services.add_comment(loud, 'here is the context', author='ana')
    services.add_comment(loud, 'nice', author='bo')

    stats = services.comment_stats(project)
    assert stats['comments'] == 2
    assert stats['system'] == 0
    assert stats['tickets_with_threads'] == 1
    assert [t.pk for t in stats['quiet_backlog']] == [quiet.pk]

    services.add_comment(quiet, 'system note', kind=Comment.Kind.SYSTEM)
    stats = services.comment_stats(project)
    # a system-only thread still counts as no human discussion
    assert [t.pk for t in stats['quiet_backlog']] == [quiet.pk]
    assert stats['system'] == 1


@pytest.mark.django_db
def test_comment_stats_counts_removed():
    """Removed comments are reported separately, never silently dropped."""
    project = make_project('Stats2')
    ticket = services.create_ticket(project, name='Mixed')
    services.add_comment(ticket, 'stays')
    gone = services.add_comment(ticket, 'leaves', author='q')
    services.delete_comment(gone)

    stats = services.comment_stats(project)
    assert stats['comments'] == 1
    assert stats['removed'] == 1
    assert Comment.objects.filter(ticket=ticket).count() == 2


# ---------------------------------------------------------------------------
# Thread automation (opt-in receivers)
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_blocker_comments_are_dormant_by_default():
    """Without TOKAMAK_THREADS the blocker graph writes no comments."""
    project = make_project('Auto1')
    a = services.create_ticket(project, name='Cause')
    b = services.create_ticket(project, name='Held')

    services.add_blocker(a, b, reason='waiting on design')
    assert Comment.objects.count() == 0


@pytest.mark.django_db
def test_auto_comment_blocker_leaves_system_note():
    """Opted in, adding a blocker posts a system comment on the blocked ticket."""
    project = make_project('Auto2')
    a = services.create_ticket(project, name='Cause2')
    b = services.create_ticket(project, name='Held2')

    with override_settings(TOKAMAK_THREADS=ACTIVE):
        services.add_blocker(a, b, reason='waiting on design')

    notes = Comment.objects.filter(ticket=b)
    assert notes.count() == 1
    note = notes.first()
    assert note.kind == Comment.Kind.SYSTEM
    assert note.author == 'tokamak'
    assert f'Blocked by #{a.pk}' in note.body
    assert 'waiting on design' in note.body


@pytest.mark.django_db
def test_auto_comment_unblock_records_clearance():
    """Removing the edge posts how it cleared; accepting the cause says delivered."""
    project = make_project('Auto3')
    a = services.create_ticket(project, name='Cause3')
    b = services.create_ticket(project, name='Held3')

    with override_settings(TOKAMAK_THREADS=ACTIVE):
        services.add_blocker(a, b)
        services.remove_blocker(a, b)
        body = Comment.objects.filter(ticket=b).order_by('-pk').first().body
        assert 'Unblocked' in body and 'resolved manually' in body

        services.add_blocker(a, b)
        services.transition_ticket(a, 'started')
        services.transition_ticket(a, 'finished')
        services.estimate_ticket(a, 3)
        services.transition_ticket(a, 'accepted')
    latest = Comment.objects.filter(ticket=b).order_by('-created_at', '-id').first()
    assert 'Unblocked' in latest.body and 'delivered' in latest.body


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_comment_api_crud_through_services(api_client):
    """Create/edit/list/soft-delete via /api/comments/."""
    project = make_project('Api1')
    ticket = services.create_ticket(project, name='Api commented')

    resp = api_client.post(
        '/api/comments/',
        {'ticket': ticket.pk, 'body': 'from the API', 'author': 'bot'},
        content_type='application/json',
    )
    assert resp.status_code == 201, resp.content
    payload = resp.json()
    pk = payload['id']
    assert payload['body'] == 'from the API'

    resp = api_client.patch(
        f'/api/comments/{pk}/',
        {'body': 'edited over REST'},
        content_type='application/json',
    )
    assert resp.status_code == 200
    assert resp.json()['body'] == 'edited over REST'

    resp = api_client.delete(f'/api/comments/{pk}/')
    assert resp.status_code == 204
    comment = Comment.objects.get(pk=pk)
    assert comment.deleted is True

    # hidden by default, visible when asked for
    assert api_client.get('/api/comments/').json()['results'] == []
    listed = api_client.get('/api/comments/?deleted=all').json()['results']
    assert len(listed) == 1 and listed[0]['deleted'] is True


@pytest.mark.django_db
def test_comment_api_filters(api_client):
    """?ticket / ?author / ?kind filter the comment list."""
    project = make_project('Api2')
    t1 = services.create_ticket(project, name='One')
    t2 = services.create_ticket(project, name='Two')
    services.add_comment(t1, 'alpha', author='lee')
    services.add_comment(t2, 'beta', author='kim')
    services.add_comment(t2, 'system note', kind=Comment.Kind.SYSTEM)

    assert len(api_client.get('/api/comments/').json()['results']) == 3
    assert len(api_client.get(f'/api/comments/?ticket={t2.pk}').json()['results']) == 2
    assert len(api_client.get('/api/comments/?author=lee').json()['results']) == 1
    kinds = api_client.get('/api/comments/?kind=system').json()['results']
    assert len(kinds) == 1 and kinds[0]['kind'] == 'system'


@pytest.mark.django_db
def test_comment_api_validates_like_the_service(api_client):
    """An empty body over REST answers 400, same as the service layer."""
    project = make_project('Api3')
    ticket = services.create_ticket(project, name='Bad body')

    resp = api_client.post(
        f'/api/tickets/{ticket.pk}/comment/',
        {'body': '   '},
        content_type='application/json',
    )
    assert resp.status_code == 400

    resp = api_client.post(
        f'/api/tickets/{ticket.pk}/comment/',
        {'body': 'good one', 'author': 'kim'},
        content_type='application/json',
    )
    assert resp.status_code == 201
    assert Comment.objects.filter(ticket=ticket).count() == 1


@pytest.mark.django_db
def test_ticket_thread_endpoint(api_client):
    """GET /api/tickets/<pk>/thread/ returns live comments plus removed count."""
    project = make_project('Api4')
    ticket = services.create_ticket(project, name='Threaded')
    keep = services.add_comment(ticket, 'stays', author='kim')
    gone = services.add_comment(ticket, 'poof')
    services.delete_comment(gone)

    resp = api_client.get(f'/api/tickets/{ticket.pk}/thread/')
    assert resp.status_code == 200
    body = resp.json()
    assert body['ticket'] == ticket.pk
    assert [c['body'] for c in body['comments']] == ['stays']
    assert body['removed'] == 1


@pytest.mark.django_db
def test_project_thread_stats_endpoint(api_client):
    """GET /api/projects/<pk>/threads/ reports discussion health."""
    project = make_project('Api5')
    ticket = services.create_ticket(project, name='Stats via API')
    services.add_comment(ticket, 'hello', author='kim')

    resp = api_client.get(f'/api/projects/{project.pk}/threads/')
    assert resp.status_code == 200
    body = resp.json()
    assert body['project'] == project.pk
    assert body['comments'] == 1
    # the only ticket in the project has a human comment, so nothing is quiet
    assert body['quiet_backlog_count'] == 0
    assert body['quiet_backlog'] == []


@pytest.mark.django_db
def test_project_thread_stats_unknown_project(api_client):
    """Unknown projects answer 404 rather than a stack trace."""
    assert api_client.get('/api/projects/9999/threads/').status_code == 404


# ---------------------------------------------------------------------------
# Ticket detail page (the human surface)
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_ticket_detail_renders_thread_with_markdown(client):
    """Comments render their markdown and carry author + timestamp."""
    project = make_project('Page1')
    ticket = services.create_ticket(project, name='Talk about it')
    services.add_comment(ticket, 'I meant **three** points.', author='ana')

    body = client.get(f'/tickets/{ticket.pk}/').content.decode()
    assert 'Thread' in body
    assert 'ana' in body
    assert '<strong>three</strong>' in body


@pytest.mark.django_db
def test_ticket_detail_shows_tombstone_count(client):
    """Removed comments appear as tombstones and are counted in the header."""
    project = make_project('Page2')
    ticket = services.create_ticket(project, name='Cleanup')
    comment = services.add_comment(ticket, 'oops', author='zed')
    services.delete_comment(comment)

    body = client.get(f'/tickets/{ticket.pk}/').content.decode()
    assert 'removed' in body
    assert '[removed]' in body
    # the body text itself is gone from the page
    assert 'oops<' not in body


@pytest.mark.django_db
def test_ticket_detail_post_add_comment(client):
    """The thread form writes through the service layer."""
    project = make_project('Page3')
    ticket = services.create_ticket(project, name='Posting')

    resp = client.post(
        f'/tickets/{ticket.pk}/',
        {'action': 'add_comment', 'body': 'posted from the page', 'author': 'kim'},
    )
    assert resp.status_code == 302
    comments = services.ticket_thread(ticket)
    assert len(comments) == 1
    assert comments[0].body == 'posted from the page'
    assert comments[0].author == 'kim'


@pytest.mark.django_db
def test_ticket_detail_post_empty_comment_flashes(client):
    """An empty comment redirects back with the service reason in the query."""
    project = make_project('Page4')
    ticket = services.create_ticket(project, name='Empty post')

    resp = client.post(
        f'/tickets/{ticket.pk}/',
        {'action': 'add_comment', 'body': '', 'author': ''},
    )
    assert resp.status_code == 302
    assert 'error=' in resp.url
    assert Comment.objects.count() == 0
    body = client.get(resp['Location']).content.decode()
    assert 'needs some words' in body


@pytest.mark.django_db
def test_ticket_detail_edit_flow(client):
    """?edit=<id> opens an inline editor; the POST rewrites the comment."""
    project = make_project('Page5')
    ticket = services.create_ticket(project, name='Editing')
    comment = services.add_comment(ticket, 'first draft', author='ana')

    page = client.get(f'/tickets/{ticket.pk}/?edit={comment.pk}').content.decode()
    assert 'edit_comment' in page
    assert 'first draft' in page

    resp = client.post(
        f'/tickets/{ticket.pk}/',
        {
            'action': 'edit_comment',
            'comment': str(comment.pk),
            'body': 'second draft',
        },
    )
    assert resp.status_code == 302
    comment.refresh_from_db()
    assert comment.body == 'second draft'


@pytest.mark.django_db
def test_ticket_detail_post_delete_comment(client):
    """The remove button soft-deletes and keeps the row as history."""
    project = make_project('Page6')
    ticket = services.create_ticket(project, name='Removing')
    comment = services.add_comment(ticket, 'bye', author='zed')

    resp = client.post(
        f'/tickets/{ticket.pk}/',
        {'action': 'delete_comment', 'comment': str(comment.pk)},
    )
    assert resp.status_code == 302
    comment.refresh_from_db()
    assert comment.deleted is True
    assert services.ticket_thread(ticket) == []


# ---------------------------------------------------------------------------
# Admin surface (read-only)
# ---------------------------------------------------------------------------
@pytest.fixture
def admin_client(db):
    """A logged-in Django admin client."""
    from django.contrib.auth.models import User
    from django.test import Client

    User.objects.create_superuser(
        username='root', password='pw', email='root@example.com'
    )
    login = Client()
    login.login(username='root', password='pw')
    return login


@pytest.mark.django_db
def test_comment_admin_lists_threads(admin_client):
    """The admin changelist shows comments with author/kind/deleted columns."""
    project = make_project('Adm1')
    ticket = services.create_ticket(project, name='Admin view')
    comment = services.add_comment(ticket, 'visible to admins', author='ana')

    resp = admin_client.get('/admin/tokamak/comment/')
    assert resp.status_code == 200
    body = resp.content.decode()
    assert 'visible to admins' in body or str(comment.pk) in body
    assert 'ana' in body


@pytest.mark.django_db
def test_comment_admin_never_writes(admin_client):
    """Browsing the admin leaves comments and their audit rows untouched."""
    project = make_project('Adm2')
    ticket = services.create_ticket(project, name='Frozen by admin')
    comment = services.add_comment(ticket, 'untouched', author='ana')
    before_comments = Comment.objects.count()
    before_events = EventLog.objects.filter(name__startswith='comment.').count()

    admin_client.get('/admin/tokamak/comment/')
    assert Comment.objects.count() == before_comments
    assert EventLog.objects.filter(name__startswith='comment.').count() == before_events
    assert Comment.objects.get(pk=comment.pk).body == 'untouched'
