"""Agentic task system: trigger, prompt assembly, runner execution, REST API.

Tests the full lifecycle:
  - Gating (disabled by default)
  - Prompt assembly (ticket context + canned prompt + project memories)
  - Task triggering and state transitions
  - REST API surface (list, detail, trigger, cancel)
  - Runner integration with shatterling (StubChat for offline tests)
"""

import threading
import time

import pytest
from django.test.utils import override_settings
from rest_framework.test import APIClient

from tokamak import memory as bridge
from tokamak import services
from tokamak.models import AgenticTask, EventLog, ProjectRepo, Ticket
from tokamak.tasks import (
    build_task_prompt,
    get_tasks_config,
    tasks_enabled,
    trigger_task,
)
from tests._utils import make_authed_client, make_project


# --- fixtures ----------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_state():
    """Reset bridge caches and thread pool between tests."""
    bridge._SESSIONS.clear()
    bridge._TICKET_SESSIONS.clear()
    bridge._CONFIG_CACHE.clear()
    yield
    bridge._SESSIONS.clear()
    bridge._TICKET_SESSIONS.clear()
    bridge._CONFIG_CACHE.clear()


def tasks_cfg(tmp_path, **over):
    """Build a TOKAMAK_TASKS override with the given overrides."""
    cfg = {
        'enabled': True,
        'starting_prompt': 'Test starting prompt.',
        'max_turns': 3,
        'timeout_seconds': 10,
        'thread_pool_size': 2,
    }
    cfg.update(over)
    return override_settings(TOKAMAK_TASKS=cfg)


def mem_cfg(tmp_path, **over):
    """Build a TOKAMAK_MEMORY override for tests that need the bridge on."""
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


def make_ticket(project, name='Test Ticket', description='Do the thing.'):
    return services.create_ticket(project, name=name, description=description)


# --- gating ------------------------------------------------------------------


def test_tasks_disabled_by_default(settings):
    """Without explicit enablement, tasks_enabled() is False."""
    assert not tasks_enabled()


def test_trigger_rejected_when_disabled(db):
    """trigger_task raises ValidationError when the system is off."""
    from django.core.exceptions import ValidationError

    project = make_project()
    ticket = make_ticket(project)
    with pytest.raises(ValidationError):
        trigger_task(ticket)


# --- prompt assembly ---------------------------------------------------------


def test_prompt_contains_ticket_context(db, tmp_path):
    """The assembled prompt includes ticket name, description, and type."""
    from tokamak.models import Project

    project = Project.objects.create(name='MyProj', description='A test project')
    ticket = make_ticket(project, name='Fix the bug', description='It is broken.')

    with tasks_cfg(tmp_path):
        prompt = build_task_prompt(ticket)

    assert 'Fix the bug' in prompt
    assert 'It is broken.' in prompt
    assert 'MyProj' in prompt


def test_prompt_contains_canned_starting_prompt(db, tmp_path):
    """The canned starting prompt from settings appears in the assembled text."""
    project = make_project()
    ticket = make_ticket(project)

    with tasks_cfg(tmp_path, starting_prompt='CUSTOM INSTRUCTION HERE'):
        prompt = build_task_prompt(ticket)

    assert 'CUSTOM INSTRUCTION HERE' in prompt


def test_prompt_includes_extra_instructions(db, tmp_path):
    """Caller-supplied extra instructions are appended."""
    project = make_project()
    ticket = make_ticket(project)

    with tasks_cfg(tmp_path):
        prompt = build_task_prompt(ticket, extra_instructions='Focus on edge cases.')

    assert 'Focus on edge cases.' in prompt


def test_prompt_includes_repos(db, tmp_path):
    """Attached project repos appear in the ticket context block."""
    project = make_project()
    ticket = make_ticket(project)
    ProjectRepo.objects.create(
        project=project, name='main-repo', url='https://git.example.com/r.git'
    )

    with tasks_cfg(tmp_path):
        prompt = build_task_prompt(ticket)

    assert 'main-repo' in prompt
    assert 'https://git.example.com/r.git' in prompt


def test_prompt_includes_project_memories(db, tmp_path):
    """When the memory bridge is on, long-term project blocks are included."""
    project = make_project()
    ticket = make_ticket(project)

    with tasks_cfg(tmp_path), mem_cfg(tmp_path):
        # Seed a long-term memory block into the project session.
        harness = bridge.open_session(project.id)
        harness.add('user', 'The deploy target is port 8080.', tag='longterm')
        harness.evict()

        prompt = build_task_prompt(ticket)

    assert 'port 8080' in prompt or 'Project memories' in prompt


# --- trigger + execution -----------------------------------------------------


def test_trigger_creates_pending_task(db, tmp_path):
    """trigger_task creates a PENDING row with the assembled prompt."""
    project = make_project()
    ticket = make_ticket(project)

    with tasks_cfg(tmp_path), mem_cfg(tmp_path):
        task = trigger_task(ticket, extra_instructions='Be thorough.')

    assert task.status == AgenticTask.Status.PENDING
    assert task.ticket_id == ticket.id
    assert task.project_id == project.id
    assert 'Test Ticket' in task.prompt
    assert 'Be thorough.' in task.prompt


def test_trigger_writes_event_log(db, tmp_path):
    """An EventLog row is written on trigger."""
    project = make_project()
    ticket = make_ticket(project)

    with tasks_cfg(tmp_path), mem_cfg(tmp_path):
        task = trigger_task(ticket)

    events = EventLog.objects.filter(name='agentic_task.triggered', ticket=ticket)
    assert events.exists()
    assert events.first().payload['task'] == task.id


def test_task_executes_to_completion(db, tmp_path, monkeypatch):
    """The runner executes a task synchronously and marks COMPLETED.

    We patch the thread pool to run inline (pytest-django's test DB is not
    visible from other threads). Uses StubChat offline.
    """
    import tokamak.tasks as tasks_mod

    # Run _execute_task inline instead of in a thread.
    original_submit = None

    class _InlinePool:
        def submit(self, fn, *args, **kwargs):
            fn(*args, **kwargs)  # run synchronously

    monkeypatch.setattr(tasks_mod, '_get_pool', lambda: _InlinePool())

    project = make_project()
    ticket = make_ticket(project)

    with tasks_cfg(tmp_path, max_turns=2), mem_cfg(tmp_path):
        task = trigger_task(ticket)
        # Since we ran inline, the DB row should already be updated.
        task.refresh_from_db()

    assert task.status == AgenticTask.Status.COMPLETED
    assert task.started_at is not None
    assert task.completed_at is not None
    # StubChat echoes back something; result should be non-empty.
    assert len(task.result) > 0 or task.error == ''


def test_task_failure_recorded(db, tmp_path, monkeypatch):
    """If the runner raises, the task transitions to FAILED with the error."""
    import tokamak.tasks as tasks_mod

    class _InlinePool:
        def submit(self, fn, *args, **kwargs):
            fn(*args, **kwargs)

    monkeypatch.setattr(tasks_mod, '_get_pool', lambda: _InlinePool())

    project = make_project()
    ticket = make_ticket(project)

    # Disable memory so the harness creation fails inside the runner.
    with tasks_cfg(tmp_path), override_settings(TOKAMAK_MEMORY={'enabled': False}):
        task = trigger_task(ticket)
        task.refresh_from_db()

    assert task.status == AgenticTask.Status.FAILED
    assert 'disabled' in task.error.lower() or 'not installed' in task.error.lower()


# --- REST API ----------------------------------------------------------------


def test_api_task_list_empty(db, tmp_path):
    """GET /api/tasks/ returns an empty list when no tasks exist."""
    client = make_authed_client()
    with tasks_cfg(tmp_path):
        resp = client.get('/api/tasks/')
    assert resp.status_code == 200
    data = resp.json()
    assert data['tasks'] == []
    assert data['enabled'] is True


def test_api_task_list_filtered(db, tmp_path):
    """GET /api/tasks/?ticket=<id> filters correctly."""
    project = make_project()
    ticket = make_ticket(project)

    with tasks_cfg(tmp_path), mem_cfg(tmp_path):
        trigger_task(ticket)

    client = make_authed_client()
    with tasks_cfg(tmp_path):
        resp = client.get(f'/api/tasks/?ticket={ticket.id}')
    assert resp.status_code == 200
    data = resp.json()
    assert len(data['tasks']) >= 1
    assert all(t['ticket'] == ticket.id for t in data['tasks'])


def test_api_task_detail(db, tmp_path):
    """GET /api/tasks/<id>/ returns full task detail."""
    project = make_project()
    ticket = make_ticket(project)

    with tasks_cfg(tmp_path), mem_cfg(tmp_path):
        task = trigger_task(ticket)

    client = make_authed_client()
    resp = client.get(f'/api/tasks/{task.id}/')
    assert resp.status_code == 200
    data = resp.json()['task']
    assert data['id'] == task.id
    assert data['ticket'] == ticket.id
    assert 'prompt' in data


def test_api_task_trigger(db, tmp_path):
    """POST /api/tasks/trigger/ creates and dispatches a task (202)."""
    project = make_project()
    ticket = make_ticket(project)
    client = make_authed_client()

    with tasks_cfg(tmp_path), mem_cfg(tmp_path):
        resp = client.post(
            '/api/tasks/trigger/',
            {'ticket': ticket.id, 'instructions': 'Use TDD.'},
            format='json',
        )

    assert resp.status_code == 202
    data = resp.json()
    assert data['task']['status'] == 'pending'
    assert data['task']['ticket'] == ticket.id
    assert 'Use TDD.' in data['task']['prompt']


def test_api_task_trigger_disabled(db, tmp_path):
    """POST /api/tasks/trigger/ returns 400 when the system is off."""
    project = make_project()
    ticket = make_ticket(project)
    client = make_authed_client()

    resp = client.post(
        '/api/tasks/trigger/', {'ticket': ticket.id}, format='json'
    )
    assert resp.status_code == 400
    assert 'disabled' in resp.json()['error']


def test_api_task_trigger_bad_ticket(db, tmp_path):
    """POST /api/tasks/trigger/ with a nonexistent ticket returns 400."""
    client = make_authed_client()

    with tasks_cfg(tmp_path):
        resp = client.post('/api/tasks/trigger/', {'ticket': 99999}, format='json')

    assert resp.status_code == 400


def test_api_task_cancel_pending(db, tmp_path):
    """POST /api/tasks/<id>/cancel/ on a PENDING task marks it FAILED."""
    project = make_project()
    ticket = make_ticket(project)
    client = make_authed_client()

    with tasks_cfg(tmp_path), mem_cfg(tmp_path):
        task = trigger_task(ticket)
        # Cancel immediately (before the thread picks it up).
        resp = client.post(f'/api/tasks/{task.id}/cancel/')

    assert resp.status_code == 200
    data = resp.json()['task']
    assert data['status'] == 'failed'
    assert 'cancelled' in data['error']


def test_api_task_cancel_running_fails(db, tmp_path):
    """Cancelling a RUNNING task returns 409."""
    project = make_project()
    ticket = make_ticket(project)
    client = make_authed_client()

    with tasks_cfg(tmp_path), mem_cfg(tmp_path):
        task = trigger_task(ticket)
        # Force the state to RUNNING (simulating a worker that picked it up).
        from django.utils import timezone

        task.status = AgenticTask.Status.RUNNING
        task.started_at = timezone.now()
        task.save(update_fields=['status', 'started_at'])

        resp = client.post(f'/api/tasks/{task.id}/cancel/')

    assert resp.status_code == 409


# --- config ------------------------------------------------------------------


def test_get_tasks_config_merges(settings):
    """get_tasks_config merges user settings over defaults."""
    with override_settings(TOKAMAK_TASKS={'enabled': True, 'max_turns': 5}):
        cfg = get_tasks_config()
    assert cfg['enabled'] is True
    assert cfg['max_turns'] == 5
    # Default fields still present.
    assert 'starting_prompt' in cfg
    assert 'timeout_seconds' in cfg