"""Agentic task system: trigger and run autonomous agent sessions on tickets.

The initial implementation provides an in-process thread runner that drives a
shatterling (clm-mem) agent session for one ticket. The prompt is assembled
from three layers:

1. **Ticket context** — name, description, type, iteration, repos (reuses the
   same structured block as ``services.open_ticket_session``).
2. **Canned starting prompt** — a configurable instruction template from
   ``settings.TOKAMAK_TASKS['starting_prompt']``.
3. **Project-level memories** — recalled long-term blocks from the project's
   clm-mem session (best-effort; degrades gracefully when the bridge is off).

Design: pluggable runner interface so the in-process thread pool can be
swapped for Celery / django-rq / channels without touching the API surface.
"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Dict, List, Optional

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.signals import post_save
from django.dispatch import Signal
from django.utils import timezone

from .models import AgenticTask, EventLog, MemorySession, Project, Ticket

logger = logging.getLogger(__name__)

#: Signal fired when an agentic task changes state (pending -> running -> done).
task_state_changed = Signal()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_TASKS_DEFAULTS: Dict = {
    'enabled': False,
    'starting_prompt': (
        'You are an autonomous agent assigned to work on this ticket. '
        'Read the context below, explore the attached sources, and begin '
        'implementing the changes described. Work step by step, verifying '
        'each change before moving on. When complete, summarize what you did.'
    ),
    'max_turns': 20,
    'timeout_seconds': 300,
    'thread_pool_size': 4,
}


def get_tasks_config() -> Dict:
    """Merge ``settings.TOKAMAK_TASKS`` over defaults."""
    user = getattr(settings, 'TOKAMAK_TASKS', None) or {}
    cfg = dict(_TASKS_DEFAULTS)
    cfg.update(user)
    return cfg


def tasks_enabled() -> bool:
    return bool(get_tasks_config()['enabled'])


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------


def build_task_prompt(ticket: Ticket, *, extra_instructions: str = '') -> str:
    """Assemble the initial prompt for an agentic task.

    Layers (in order):
      1. Structured ticket context block (same as session opening).
      2. Canned starting prompt from settings.
      3. Project-level memories (recalled from clm-mem, best-effort).
      4. Optional extra instructions passed by the caller.
    """
    cfg = get_tasks_config()
    parts: List[str] = []

    # Layer 1: ticket context (reuse the service-layer block builder)
    from .services import _build_ticket_context_block

    parts.append(_build_ticket_context_block(ticket))

    # Layer 2: canned starting prompt
    starting = cfg.get('starting_prompt', '')
    if starting:
        parts.append(starting.strip())

    # Layer 3: project-level memories (best-effort)
    try:
        from . import memory as bridge

        if bridge.is_enabled():
            mem_blocks = bridge.recall(ticket.project_id, query='', limit=10)
            live = [b for b in mem_blocks if b.get('live') and b.get('tag') == 'longterm']
            if live:
                lines = ['\n[Project memories]']
                for b in live:
                    lines.append(f"  - {b['content']}")
                parts.append('\n'.join(lines))
    except Exception:  # noqa: S110
        pass  # memory is optional; never block task creation on it

    # Layer 4: caller-specific instructions
    if extra_instructions:
        parts.append(f'\n[Additional instructions]\n{extra_instructions.strip()}')

    return '\n\n'.join(parts)


# ---------------------------------------------------------------------------
# Runner interface + in-process implementation
# ---------------------------------------------------------------------------


class TaskRunner:
    """Abstract runner: execute one agentic task. Subclass to swap backends."""

    def run(self, task: AgenticTask) -> str:
        raise NotImplementedError


class ShatterlingRunner(TaskRunner):
    """Drives a shatterling (clm-mem) agent session for one ticket.

    Opens the ticket harness, feeds the assembled prompt as the first turn,
    then lets the model run up to ``max_turns`` autonomous steps. Returns the
    model's final reply as the task result.
    """

    def run(self, task: AgenticTask) -> str:
        from . import memory as bridge

        cfg = get_tasks_config()
        max_turns = int(cfg.get('max_turns', 20))

        harness = bridge.ticket_harness(task.ticket)

        # Feed the full prompt as the first user turn.
        if callable(getattr(harness, 'turn', None)):  # RemoteHarness
            resp = bridge.remote_ingest(harness, task.prompt)
            reply = resp['choices'][0]['message']['content']
        else:
            reply = harness.step(task.prompt)

        # Autonomous continuation: let the agent keep working up to max_turns.
        final_reply = str(reply).strip()
        for _ in range(max_turns - 1):
            if not final_reply:
                break
            # Simple heuristic: stop when the model signals completion.
            if any(marker in final_reply.lower() for marker in (
                'task complete', 'all done', 'finished implementing',
            )):
                break
            continue_prompt = (
                'Continue working on the ticket. If you are done, say '
                '"Task complete" and summarize your changes.'
            )
            if callable(getattr(harness, 'turn', None)):
                resp = bridge.remote_ingest(harness, continue_prompt)
                final_reply = resp['choices'][0]['message']['content'].strip()
            else:
                final_reply = str(harness.step(continue_prompt)).strip()

        return final_reply


# ---------------------------------------------------------------------------
# In-process thread pool executor
# ---------------------------------------------------------------------------

_pool: Optional[ThreadPoolExecutor] = None
_pool_lock = threading.Lock()


def _get_pool() -> ThreadPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            size = int(get_tasks_config().get('thread_pool_size', 4))
            _pool = ThreadPoolExecutor(max_workers=size, thread_name_prefix='agentic-task')
        return _pool


def _execute_task(task_id: int) -> None:
    """Background worker: run one task to completion (or failure)."""
    try:
        task = AgenticTask.objects.select_related('ticket', 'project').get(pk=task_id)
    except AgenticTask.DoesNotExist:
        logger.warning('AgenticTask %s vanished before execution', task_id)
        return

    # Mark running
    task.status = AgenticTask.Status.RUNNING
    task.started_at = timezone.now()
    task.save(update_fields=['status', 'started_at'])
    task_state_changed.send(sender=AgenticTask, task=task, old='pending', new='running')

    try:
        runner = ShatterlingRunner()
        result = runner.run(task)
        task.status = AgenticTask.Status.COMPLETED
        task.result = result or ''
        task.completed_at = timezone.now()
        task.save(update_fields=['status', 'result', 'completed_at'])
    except Exception as exc:  # noqa: S110
        logger.exception('AgenticTask %s failed', task_id)
        task.status = AgenticTask.Status.FAILED
        task.error = str(exc)
        task.completed_at = timezone.now()
        task.save(update_fields=['status', 'error', 'completed_at'])

    task_state_changed.send(sender=AgenticTask, task=task, old='running', new=task.status)

    # Audit log
    EventLog.objects.create(
        name=f'agentic_task.{task.status}',
        ticket=task.ticket,
        project=task.project,
        payload={'task': task.id, 'result_head': (task.result or '')[:200]},
    )


# ---------------------------------------------------------------------------
# Service layer (the single entry point for triggering tasks)
# ---------------------------------------------------------------------------


def trigger_task(
    ticket: Ticket,
    *,
    user=None,
    extra_instructions: str = '',
) -> AgenticTask:
    """Create and dispatch an agentic task for one ticket.

    Validates that the task system is enabled, assembles the prompt, creates
    the DB row, and hands it to the thread pool. Returns the (pending)
    ``AgenticTask`` instance so the caller can poll its state.
    """
    cfg = get_tasks_config()
    if not cfg['enabled']:
        raise ValidationError({'tasks': 'Agentic tasks are disabled (TOKAMAK_TASKS.enabled)'})

    # Build the prompt BEFORE creating the row so it is persisted at trigger time.
    prompt = build_task_prompt(ticket, extra_instructions=extra_instructions)

    task = AgenticTask.objects.create(
        ticket=ticket,
        project=ticket.project,
        status=AgenticTask.Status.PENDING,
        prompt=prompt,
        session_key=f'ws:agentic-ticket-{ticket.id}',
        payload={'user': getattr(user, 'username', None)},
    )

    # Dispatch to the thread pool (fire-and-forget; state tracked in DB).
    _get_pool().submit(_execute_task, task.pk)

    EventLog.objects.create(
        name='agentic_task.triggered',
        ticket=ticket,
        project=ticket.project,
        payload={'task': task.id, 'user': getattr(user, 'username', None)},
    )

    return task
