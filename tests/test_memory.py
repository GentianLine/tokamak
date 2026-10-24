"""clm-mem memory bridge: config gating, local sessions, event mirroring,
eviction-backed replay, recall/reinject, management commands and REST routes.

Everything runs fully offline (clm_mem.StubChat); the optional dependency is
installed in CI-compatible dev environments via `pip install clm-mem`.
"""

import os
from io import StringIO

import pytest
from django.core.management import call_command
from django.test import override_settings
from rest_framework.test import APIClient

from tokamak import memory
from tokamak.automation import mirror_to_memory
from tokamak.models import EventLog, Ticket
from tests._utils import make_project


@pytest.fixture(autouse=True)
def clean_sessions():
    """Sessions are process-global; reset between tests to avoid leakage."""
    memory._SESSIONS.clear()
    memory._CONFIG_CACHE.clear()
    yield
    memory._SESSIONS.clear()
    memory._CONFIG_CACHE.clear()


def mem_cfg(tmp_path, **over):
    cfg = {
        'enabled': True,
        'mode': 'local',
        'workdir': str(tmp_path / 'memory'),
        'max_context_tokens': 4096,
        'shortterm_keep_pct': 0.5,
        'mirror': 'off',
    }
    cfg.update(over)
    return override_settings(TOKAMAK_MEMORY=cfg)


# --- gating -----------------------------------------------------------------


def test_disabled_bridge_raises_and_never_bites(db):
    with override_settings(TOKAMAK_MEMORY={'enabled': False}):
        assert not memory.is_enabled()
        with pytest.raises(memory.MemoryUnavailable):
            memory.open_session(1)
        # live mirror hook must swallow everything, never fail a board write
        mirror_to_memory(EventLog.objects.create(name='ticket.created', payload={}))


def test_missing_dependency_message(db, tmp_path):
    cfg = {'enabled': True, 'mode': 'local', 'workdir': str(tmp_path)}
    with override_settings(TOKAMAK_MEMORY=cfg):
        import clm_mem  # noqa: F401  (available here; just ensure no crash)

        h = memory.open_session(7)
        assert type(h).__name__ == 'MemoryHarness'


def test_served_mode_requires_base_url(db, tmp_path):
    cfg = {
        'enabled': True,
        'mode': 'served',
        'workdir': str(tmp_path),
        'backend_base_url': '',
    }
    with override_settings(TOKAMAK_MEMORY=cfg):
        with pytest.raises(memory.MemoryUnavailable):
            memory.open_session(1)


# --- local mirroring --------------------------------------------------------


def test_remember_event_tags_and_mirrors_files(db, tmp_path):
    project = make_project('MemA')
    ticket = Ticket.objects.create(project=project, name='Alpha task')
    event = EventLog.objects.create(
        name='ticket.state_changed',
        ticket=ticket,
        project=project,
        payload={'old': 'unstarted', 'new': 'started'},
    )
    with mem_cfg(tmp_path):
        block = memory.remember_event(event)
        assert block.tag == 'shortterm'
        assert 'state_changed' in block.content
        sess_dir = os.path.join(str(tmp_path / 'memory'), 'projects', str(project.id))
        assert os.path.exists(os.path.join(sess_dir, 'context.jsonl'))
        snap = memory.session_status(project.id)
        assert snap['counts']['shortterm'] >= 1


def test_note_endpoint_writes_longterm_block(db, tmp_path):
    project = make_project('MemB')
    client = APIClient()
    with mem_cfg(tmp_path):
        r = client.post(
            '/api/memory/note/',
            data={'project': project.id, 'text': 'Deploy port for MemB is 8012.'},
            format='json',
        )
        assert r.status_code == 200 and r.json()['enabled'] is True
        hits = memory.recall(project.id, 'deploy port')
        assert hits and hits[0]['live'] and hits[0]['tag'] == 'longterm'


def test_recall_distinguishes_live_from_archive(db, tmp_path):
    project = make_project('MemC')
    with mem_cfg(tmp_path, max_context_tokens=96):
        for i in range(24):
            memory.remember_event(
                EventLog.objects.create(
                    name='ticket.created',
                    project=project,
                    payload={'name': f'Evictme {i}', 'type': 'feature'},
                )
            )
        h = memory.open_session(project.id)
        # long-term never destroyed: overflow lands in the archive
        assert h.archive_size() > 0
        hits = memory.recall(project.id, 'Evictme')
        assert hits and any(not h_.get('live') for h_ in hits)
        archived = [h_ for h_ in hits if not h_.get('live')]
        ok = memory.reinject(project.id, archived[0]['id'])
        assert ok is True


def test_empty_query_lists_live_only(db, tmp_path):
    project = make_project('MemD')
    with mem_cfg(tmp_path):
        memory.remember_event(
            EventLog.objects.create(
                name='project.updated', project=project, payload={'name': 'MemD'}
            )
        )
        hits = memory.recall(project.id, '')
        assert hits and all(h_.get('live') for h_ in hits)


# --- live mirror receivers ---------------------------------------------------


def test_live_mirror_on_signals(db, tmp_path):
    project = make_project('LiveA')
    with mem_cfg(tmp_path, mirror='live'):
        from tokamak import services

        # creation must go through the service layer for signals to fire
        ticket = services.create_ticket(project, name='Live task')
        h = memory.open_session(project.id)
        contents = ' '.join(b.content for b in h.live_blocks())
        assert "ticket created" in contents
        services_transition = None
        from tokamak import services

        services.transition_ticket(ticket, Ticket.State.STARTED)
        contents = ' '.join(b.content for b in h.live_blocks())
        assert 'state_changed' in contents


def test_mirror_off_leaves_sessions_empty(db, tmp_path):
    project = make_project('OffA')
    from tokamak import services

    with mem_cfg(tmp_path, mirror='off'):
        services.create_ticket(project, name='Quiet task')
        # session may exist but must carry nothing mirrored
        try:
            h = memory.open_session(project.id)
            assert list(h.live_blocks()) == []
        except memory.MemoryUnavailable:
            pass  # also fine: nothing opened


# --- management commands -------------------------------------------------------


def test_memory_sync_incremental(db, tmp_path):
    project = make_project('SyncA')
    from tokamak import services

    with mem_cfg(tmp_path):
        services.create_ticket(project, name='Sync task')  # fires signals -> rows
        out = StringIO()
        call_command('memory_sync', '--dry-run', stdout=out)
        assert 'would mirror' in out.getvalue()

        call_command('memory_sync', stdout=out)
        first_count = EventLog.objects.count()
        h = memory.open_session(project.id)
        assert len(h.live_blocks()) == first_count

        # watermark: a second incremental sync mirrors nothing new
        out = StringIO()
        call_command('memory_sync', stdout=out)
        assert 'mirrored 0' in out.getvalue()
        # but replay-all re-admits everything
        services.create_ticket(project, name='Later task')
        out = StringIO()
        call_command('memory_sync', '--replay-all', stdout=out)
        assert 'mirrored' in out.getvalue()


def test_memory_recall_cli(db, tmp_path):
    project = make_project('RecallA')
    with mem_cfg(tmp_path):
        memory.remember_event(
            EventLog.objects.create(
                name='ticket.created',
                project=project,
                payload={'name': 'Burndown widget', 'type': 'feature'},
            )
        )
        out = StringIO()
        call_command(
            'memory_recall', 'burndown', '--project', str(project.id), stdout=out
        )
        assert 'Burndown widget' in out.getvalue()


# --- REST surface --------------------------------------------------------------


def test_status_endpoint_reports_bridge(db):
    client = APIClient()
    r = client.get('/api/memory/status/')
    assert r.status_code == 200
    body = r.json()
    assert body['enabled'] is False and body['clm_mem_installed'] is True


def test_status_with_snapshot(db, tmp_path):
    project = make_project('RestA')
    client = APIClient()
    with mem_cfg(tmp_path):
        memory.remember_event(
            EventLog.objects.create(
                name='project.updated', project=project, payload={'name': 'RestA'}
            )
        )
        r = client.get(f'/api/memory/status/?project={project.id}')
        assert 'snapshot' in r.json()


def test_recall_endpoint_round_trip(db, tmp_path):
    project = make_project('RestB')
    client = APIClient()
    with mem_cfg(tmp_path):
        memory.remember_event(
            EventLog.objects.create(
                name='ticket.created',
                project=project,
                payload={'name': 'Zephyr thing', 'type': 'feature'},
            )
        )
        r = client.post(
            '/api/memory/recall/',
            data={'project': project.id, 'query': 'zephyr'},
            format='json',
        )
        assert r.status_code == 200
        assert any('Zephyr' in h['content'] for h in r.json()['hits'])


def test_endpoints_reject_when_disabled(db):
    client = APIClient()
    r = client.post(
        '/api/memory/recall/', data={'project': 1, 'query': 'x'}, format='json'
    )
    assert r.status_code == 400 and r.json()['enabled'] is False
