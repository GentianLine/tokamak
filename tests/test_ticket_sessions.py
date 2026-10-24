"""Ticket-scoped clm-mem sessions: lifecycle triggers, monitoring links,
writeback summary, archive handoff across attempts, REST and CLI surfaces.

Offline via StubChat; the ticket_sessions sub-switch is off for all other
tests, so baseline board behavior is untouched.
"""

from io import StringIO

import pytest
from django.core.management import call_command
from django.test.utils import override_settings
from rest_framework.test import APIClient

from tokamak import memory as bridge
from tokamak import services
from tokamak.models import EventLog, MemorySession
from tests._utils import make_project


@pytest.fixture(autouse=True)
def clean_sessions():
    bridge._SESSIONS.clear()
    bridge._TICKET_SESSIONS.clear()
    bridge._CONFIG_CACHE.clear()
    yield
    bridge._SESSIONS.clear()
    bridge._TICKET_SESSIONS.clear()
    bridge._CONFIG_CACHE.clear()


def sess_cfg(tmp_path, **over):
    cfg = {
        'enabled': True,
        'mode': 'local',
        'workdir': str(tmp_path / 'mem'),
        'max_context_tokens': 4096,
        'shortterm_keep_pct': 0.5,
        'mirror': 'off',
        'ticket_sessions': True,
    }
    cfg.update(over)
    return override_settings(TOKAMAK_MEMORY=cfg)


def make_ticket(project, name='T'):
    return services.create_ticket(project, name=name)


# --- gating ----------------------------------------------------------------


def test_no_sessions_when_disabled(db):
    project = make_project('Gate')
    t = make_ticket(project, 'Quiet')
    services.transition_ticket(t, 'started')
    assert list(MemorySession.objects.filter(ticket=t)) == []


def test_bridge_on_but_subswitch_off(db, tmp_path):
    cfg = {
        'enabled': True,
        'mode': 'local',
        'workdir': str(tmp_path),
        'ticket_sessions': False,
    }
    with override_settings(TOKAMAK_MEMORY=cfg):
        project = make_project('Gate2')
        t = make_ticket(project, 'Quiet2')
        services.transition_ticket(t, 'started')
        assert list(MemorySession.objects.filter(ticket=t)) == []


# --- lifecycle ---------------------------------------------------------------


def test_open_on_started_and_close_on_finished(db, tmp_path):
    project = make_project('LifeA')
    t = make_ticket(project, 'Lifer')
    with sess_cfg(tmp_path):
        services.transition_ticket(t, 'started')
        sess = MemorySession.objects.filter(ticket=t).first()
        assert sess is not None and sess.status == 'open'
        assert sess.session_key == f'ws:ticket-{t.id}'
        assert sess.monitor_url.startswith('/api/sessions/')

        # longterm decision inside the session
        h = bridge.ticket_harness(t)
        h.add('user', 'Decision: ship behind a flag.', tag='longterm')
        h.evict()
        h.mirror_files()

        services.transition_ticket(t, 'finished')
        sess.refresh_from_db()
        assert sess.status == 'ended'
        assert sess.end_reason == 'ticket:finished'
        assert 'ship behind a flag' in sess.summary
        assert sess.payload['offloaded'] == 1 and sess.payload['archive'] == 1
        names = [
            e.name
            for e in EventLog.objects.filter(ticket=t).exclude(
                name__startswith='ticket'
            )
        ]
        assert sorted(names) == ['session.ended', 'session.opened']


def test_withdraw_and_reopen_attempt(db, tmp_path):
    project = make_project('LifeB')
    t = make_ticket(project, 'Flaky')
    with sess_cfg(tmp_path):
        services.transition_ticket(t, 'started')
        s1 = MemorySession.objects.filter(ticket=t).first()
        h = bridge.ticket_harness(t)
        h.add('user', 'Fact: cache lives in redis.', tag='longterm')
        h.evict()
        h.mirror_files()

        services.transition_ticket(t, 'unstarted')  # withdraw work
        s1.refresh_from_db()
        assert s1.status == 'ended' and s1.end_reason == 'withdrawn'

        services.transition_ticket(t, 'started')  # new attempt
        s2 = MemorySession.objects.filter(ticket=t).order_by('-started_at').first()
        assert s2.pk != s1.pk and s2.status == 'open'
        assert s2.payload.get('seeded_blocks', 0) >= 1
        contents = [b.content for b in bridge.ticket_harness(t).live_blocks()]
        assert any('redis' in c for c in contents)


def test_rejected_then_restart_new_attempt(db, tmp_path):
    project = make_project('LifeC')
    t = make_ticket(project, 'Rejector')
    with sess_cfg(tmp_path):
        services.transition_ticket(t, 'started')
        s1 = MemorySession.objects.filter(ticket=t).first()
        services.transition_ticket(t, 'finished')
        s1.refresh_from_db()
        assert s1.end_reason == 'ticket:finished'
        services.transition_ticket(t, 'rejected')
        # no open sessions left to close on reject; attempt closed at finish
        assert MemorySession.objects.filter(ticket=t).count() == 1
        services.transition_ticket(t, 'started')
        assert MemorySession.objects.filter(ticket=t).count() == 2


def test_manual_close_idempotent(db, tmp_path):
    project = make_project('LifeD')
    t = make_ticket(project, 'Manual')
    with sess_cfg(tmp_path):
        services.transition_ticket(t, 'started')
        s = MemorySession.objects.filter(ticket=t).first()
        closed = services.close_ticket_session(s, reason='manual')
        assert closed.status == 'ended' and closed.end_reason == 'manual'
        again = services.close_ticket_session(closed, reason='whatever')
        assert again.end_reason == 'manual'  # idempotent, not overwritten


def test_reopen_closes_stale_attempt(db, tmp_path):
    project = make_project('LifeE')
    t = make_ticket(project, 'Stale')
    with sess_cfg(tmp_path):
        s1 = services.open_ticket_session(t)
        s2 = services.open_ticket_session(t)  # reopen replaces
        s1.refresh_from_db()
        assert s1.status == 'ended' and s1.end_reason == 'reopen'
        assert s2.status == 'open'


def test_monitor_url_served_mode(db, tmp_path):
    project = make_project('ServeA')
    t = make_ticket(project, 'Served')
    with sess_cfg(
        tmp_path,
        mode='served',
        backend_base_url='http://127.0.0.1:8080/v1',
    ):
        # Explicit contract: open() is fail-fast, so a *closed port* must
        # raise MemoryUnavailable and the service layer degrades around it
        # (best-effort seeding keeps the DB lifecycle alive).
        s = services.open_ticket_session(t)
        assert s.monitor_url == f'http://127.0.0.1:8080/?session=ws:ticket-{t.id}'


# --- REST ---------------------------------------------------------------------


def test_rest_gate_reports_disabled(db):
    client = APIClient()
    # list still answers (gate flags + empty list); open is refused
    r = client.get('/api/sessions/?ticket=1')
    assert r.status_code == 200 and r.json()['enabled'] is False
    assert r.json()['sessions'] == []
    r2 = client.post('/api/sessions/open/', data={'ticket': 1}, format='json')
    assert r2.status_code == 400


def test_rest_round_trip(db, tmp_path):
    project = make_project('RestS')
    t = make_ticket(project, 'RESTed')
    client = APIClient()
    with sess_cfg(tmp_path):
        r = client.post('/api/sessions/open/', data={'ticket': t.id}, format='json')
        assert r.status_code == 201
        sess = r.json()['session']
        sid = sess['id']

        # monitor snapshot shows the live window
        h = bridge.ticket_harness(t)
        h.add('user', 'Note: staging db is replica-3.', tag='longterm')
        h.evict()
        r = client.get(f'/api/sessions/{sid}/snapshot/')
        body = r.json()
        assert any('replica-3' in b['content'] for b in body['blocks'])

        # close with explicit summary override flows back
        r = client.post(
            f'/api/sessions/{sid}/close/',
            data={'reason': 'manual', 'summary': 'Done: shipped.'},
            format='json',
        )
        assert r.json()['session']['status'] == 'ended'

        # list + latest for the ticket
        r = client.get(f'/api/sessions/?ticket={t.id}&status=ended')
        data = r.json()
        assert len(data['sessions']) == 1
        assert data['latest']['summary'] == 'Done: shipped.'

        # second close is a no-op note
        r = client.post(f'/api/sessions/{sid}/close/', data={}, format='json')
        assert r.json().get('note') == 'already ended'

        # snapshot of an ended session explains itself
        r = client.get(f'/api/sessions/{sid}/snapshot/')
        assert 'closed' in r.json()['note']


def test_rest_open_missing_ticket(db, tmp_path):
    client = APIClient()
    with sess_cfg(tmp_path):
        r = client.post('/api/sessions/open/', data={}, format='json')
        assert r.status_code == 400
        r = client.get('/api/sessions/')
        assert r.status_code == 400


# --- CLI -----------------------------------------------------------------------


def test_cli_open_close_list_summary(db, tmp_path):
    project = make_project('CliS')
    t = make_ticket(project, 'Clisy')
    with sess_cfg(tmp_path):
        out = StringIO()
        call_command('ticket_session', '--open-ticket', str(t.id), stdout=out)
        assert 'opened ws:ticket-' in out.getvalue()

        from tokamak.models import MemorySession as MS

        s = MS.objects.filter(ticket=t).first()
        out = StringIO()
        call_command(
            'ticket_session',
            '--close-session',
            str(s.pk),
            '--reason',
            'manual',
            stdout=out,
        )
        assert 'closed #' in out.getvalue()

        out = StringIO()
        call_command('ticket_session', '--list', '--ticket', str(t.id), stdout=out)
        assert f'ws:ticket-{t.id}' in out.getvalue()

        out = StringIO()
        call_command('ticket_session', '--summary', str(s.pk), stdout=out)
        assert 'no durable fact' in out.getvalue() or 'Session notes' in out.getvalue()


def test_cli_noop_errors(db):
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        call_command('ticket_session')
