"""Served mode against the clm-mem *explicit session API* (branch
``spike/session-api``, see clm-mem/docs/library-migration.md): a real
``clm-mem serve`` supervisor with mock backends, no GPU.

These tests pin the migration contract end-to-end for the bridge:

* branches open eagerly at connect time (POST /v1/clm/sessions with
  creation-time options), never as header side effects;
* ingest turns go to POST /v1/clm/sessions/<id>/turns and answer with
  harness truth (``clm_mem`` payload: counts/budget/archive/events);
* legacy headers earn a 410, /v1/chat/completions is pure passthrough
  (never remembers), turns on unknown branches earn a 409;
* lifecycle ends with DELETE /v1/clm/sessions/<id> (good-citizen cleanup).

Offline via the bundled mock backends (StubChat/SystemOne fakes). Skips if
the installed clm-mem predates the explicit-session SDK.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

import pytest
from django.test import override_settings
from rest_framework.test import APIClient

from tokamak import memory
from tokamak.models import EventLog
from tests._utils import make_project


def _contract_available():
    """True when the installed clm-mem speaks open()/turn() (session-api)."""
    try:
        from clm_mem import RemoteHarness

        return hasattr(RemoteHarness, 'open') and hasattr(RemoteHarness, 'turn')
    except ImportError:
        return False


pytestmark = pytest.mark.skipif(
    not _contract_available(), reason='clm-mem explicit session API not installed'
)


@pytest.fixture(scope='module')
def sup():
    from clm_mem import start_supervisor

    s = start_supervisor(
        public_port=0,
        served_model='mock-model',
        backend_mode='mock',
        systemone_mode='mock',
        harness_kwargs={'memory_preamble': False},
        timeout=30,
        ui=False,
    )
    yield s
    s.shutdown()


@pytest.fixture(autouse=True)
def clean_sessions():
    memory._SESSIONS.clear()
    memory._TICKET_SESSIONS.clear()
    memory._CONFIG_CACHE.clear()
    yield
    memory._SESSIONS.clear()
    memory._TICKET_SESSIONS.clear()
    memory._CONFIG_CACHE.clear()


def served_cfg(sup, tmp_path, **over):
    cfg = {
        'enabled': True,
        'mode': 'served',
        'workdir': str(tmp_path / 'mem'),
        'max_context_tokens': 600,
        'shortterm_keep_pct': 0.5,
        'mirror': 'off',
        'backend_base_url': sup.public_base,
        'backend_model': 'mock-model',
    }
    cfg.update(over)
    return override_settings(TOKAMAK_MEMORY=cfg)


def snapshot(sup, sid):
    return http_call(
        sup, '/v1/clm/sessions/' + urllib.parse.quote(sid, '') + '/snapshot'
    )


def http_call(sup, path, method='GET', body=None, headers=None):
    """Raw HTTP against the daemon: returns (status, json|text)."""
    url = f'{sup.base_url}{path}'
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('Content-Type', 'application/json')
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        resp = urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw
    raw = resp.read().decode()
    try:
        return resp.status, json.loads(raw)
    except Exception:
        return resp.status, raw


# --- eager open / creation-time options --------------------------------------


def test_open_session_opens_branch_eagerly(sup, db, tmp_path):
    project = make_project(f'SrvA{EventLog.objects.count()}')
    pid = project.id + 10000  # unique branch name per run keeps isolation real
    sid = f'ws:project-{pid}'
    with served_cfg(sup, tmp_path):
        h = memory._serve_remote(memory.get_config(), sid)
        assert h.opened() is True
        st, listing = http_call(sup, '/v1/clm/sessions')
        assert sid in listing['sessions']
        st, snap = snapshot(sup, sid)
        assert st == 200 and snap['session_id'] == sid


def test_creation_options_reach_the_branch(sup, db, tmp_path):
    pid = 20000 + EventLog.objects.count()
    sid = f'ws:project-{pid}'
    with served_cfg(sup, tmp_path, max_context_tokens=777, shortterm_keep_pct=0.6):
        h = memory._serve_remote(memory.get_config(), sid)
        st, snap = snapshot(sup, sid)
        assert st == 200 and snap['budget'] == 777
        assert float(snap['shortterm_keep_pct']) == pytest.approx(0.6)


def test_reopen_is_idempotent_and_never_reshapes(sup, db, tmp_path):
    pid = 30000 + EventLog.objects.count()
    sid = f'ws:project-{pid}'
    with served_cfg(sup, tmp_path, max_context_tokens=640):
        memory._serve_remote(memory.get_config(), sid)
        # a second open on the same branch must never reshape it
        st, resp = http_call(
            sup,
            '/v1/clm/sessions',
            'POST',
            {'session': sid, 'options': {'max_context_tokens': 999}},
        )
        assert st in (200, 201)
        assert resp['budget'] == 640  # reopen never reshapes the branch
        # the bridge's own reopen path is equally non-destructive
        h = memory._serve_remote(
            memory.get_config(), sid  # different options would be ignored too
        )
        st, snap = snapshot(sup, sid)
        assert snap['budget'] == 640 and h.opened() is True


# --- ingest turns / harness truth ---------------------------------------------


def test_remember_event_flows_through_turns(sup, db, tmp_path):
    project = make_project('SrvD')
    with served_cfg(sup, tmp_path):
        resp = memory.remember_event(
            EventLog.objects.create(
                name='project.updated', project=project, payload={'name': 'SrvD'}
            )
        )
        assert resp['clm_mem']['counts']  # harness truth rides on the reply
        assert resp['choices'][0]['message']['content']
        hits = memory.recall(project.id, 'project updated')
        assert any(h_['live'] for h_ in hits)


def test_status_snapshot_no_polling_side_channel(sup, db, tmp_path):
    project = make_project('SrvE')
    with served_cfg(sup, tmp_path):
        memory.remember_event(
            EventLog.objects.create(
                name='ticket.created', project=project, payload={'name': 'T1'}
            )
        )
        snap = memory.session_status(project.id)
        assert snap['counts']['longterm'] >= 1
        assert 'budget' in snap and 'tokens' in snap


def test_note_endpoint_served_writes_and_reports_block(sup, db, tmp_path):
    project = make_project('SrvF')
    client = APIClient()
    with served_cfg(sup, tmp_path):
        r = client.post(
            '/api/memory/note/',
            data={'project': project.id, 'text': 'Deploy port for SrvF is 8020.'},
            format='json',
        )
        assert r.status_code == 200 and r.json()['enabled'] is True
        hits = memory.recall(project.id, 'deploy port')
        assert any(h_['live'] for h_ in hits)


# --- error shapes / legacy rejection ------------------------------------------


def test_legacy_headers_get_410(sup):
    st, body = http_call(
        sup,
        '/v1/chat/completions',
        'POST',
        {'messages': [{'role': 'user', 'content': 'hi'}]},
        headers={'X-Cllm-Mem-Session': 'ws:ghost'},
    )
    assert st == 410
    assert 'clm/sessions' in str(body)


def test_chat_completions_is_pure_passthrough(sup):
    # no headers, plain OpenAI semantics: answers, never remembers
    st, resp = http_call(
        sup,
        '/v1/chat/completions',
        'POST',
        {'messages': [{'role': 'user', 'content': 'Remember: ghost fact GX-9'}]},
    )
    assert st == 200 and 'choices' in resp
    # nothing became a branch as a side effect of passthrough
    st, listing = http_call(sup, '/v1/clm/sessions')
    assert all(not s.startswith('ws:ghost') for s in listing['sessions'])


def test_turn_on_unknown_branch_is_409(sup):
    st, body = http_call(
        sup,
        '/v1/clm/sessions/' + urllib.parse.quote('ws:nobody', '') + '/turns',
        'POST',
        {'content': 'hello?'},
    )
    assert st == 409 and 'open it first' in str(body)


def test_bare_id_no_longer_names_a_branch(sup):
    st, body = http_call(sup, '/v1/clm/sessions', 'POST', {'session': 'alice'})
    assert st == 400 and 'ws:<name>' in str(body)


# --- lifecycle: drop is deliberate --------------------------------------------


def test_drop_session_deletes_branch_server_side(sup, db, tmp_path):
    project = make_project('SrvG')
    with served_cfg(sup, tmp_path):
        memory.open_session(project.id)
        st, listing = http_call(sup, '/v1/clm/sessions')
        assert f'ws:project-{project.id}' in listing['sessions']
        assert memory.drop_session(project.id) is True
        st, listing = http_call(sup, '/v1/clm/sessions')
        assert f'ws:project-{project.id}' not in listing['sessions']


def test_ticket_branch_dropped_on_session_end(sup, db, tmp_path):
    from tokamak import services

    project = make_project('SrvH')
    ticket = services.create_ticket(project, name='Servey')
    cfg = {
        'enabled': True,
        'mode': 'served',
        'workdir': str(tmp_path / 'mem'),
        'max_context_tokens': 600,
        'shortterm_keep_pct': 0.5,
        'mirror': 'off',
        'ticket_sessions': True,
        'backend_base_url': sup.public_base,
        'backend_model': 'mock-model',
    }
    with override_settings(TOKAMAK_MEMORY=cfg):
        sess = services.open_ticket_session(ticket)
        st, listing = http_call(sup, '/v1/clm/sessions')
        assert f'ws:ticket-{ticket.id}' in listing['sessions']
        services.close_ticket_session(sess, reason='manual')
        st, listing = http_call(sup, '/v1/clm/sessions')
        assert f'ws:ticket-{ticket.id}' not in listing['sessions']
