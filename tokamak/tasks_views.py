"""REST surface for agentic tasks.

Routes (registered in ``tokamak/api_urls.py`` under /api/):

    POST   /tasks/trigger/       {"ticket": <id>, "instructions": "..."}
    GET    /tasks/               ?project=<id>&ticket=<id>&status=<status>
    GET    /tasks/<pk>/          detail + result/error
    POST   /tasks/<pk>/cancel/   mark a pending task as failed (best-effort)
"""

import json

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import JsonResponse, QueryDict
from django.views.decorators.http import require_GET, require_POST

from .models import AgenticTask, Ticket
from .tasks import tasks_enabled, trigger_task


def _json_body(request):
    try:
        return json.loads(request.body.decode('utf-8') or '{}')
    except ValueError:
        return {}


def _safe_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _serialize_task(task: AgenticTask) -> dict:
    out = {
        'id': task.id,
        'ticket': task.ticket_id,
        'project': task.project_id,
        'status': task.status,
        'prompt': task.prompt,
        'result': task.result,
        'error': task.error,
        'session_key': task.session_key,
        'created_at': task.created_at.isoformat(),
        'started_at': task.started_at.isoformat() if task.started_at else None,
        'completed_at': task.completed_at.isoformat() if task.completed_at else None,
        'payload': task.payload,
    }
    if task.duration:
        out['duration_seconds'] = task.duration.total_seconds()
    return out


def _gate():
    return {'enabled': tasks_enabled()}


@require_GET
def task_list(request):
    """GET /tasks/?project=<id>[&ticket=<id>][&status=<status>]"""
    qd = QueryDict(request.META.get('QUERY_STRING', ''))
    out = _gate()
    qs = AgenticTask.objects.select_related('ticket', 'project')

    pid = qd.get('project')
    if pid:
        qs = qs.filter(project_id=_safe_int(pid) or 0)
    tid = qd.get('ticket')
    if tid:
        qs = qs.filter(ticket_id=_safe_int(tid) or 0)
    status = qd.get('status')
    if status:
        qs = qs.filter(status=status)

    out['tasks'] = [_serialize_task(t) for t in qs[:100]]
    return JsonResponse(out)


@require_GET
def task_detail(request, pk):
    """GET /tasks/<pk>/ — full detail including prompt and result."""
    task = AgenticTask.objects.filter(pk=pk).first()
    if task is None:
        return JsonResponse({'error': 'task not found'}, status=404)
    out = _gate()
    out['task'] = _serialize_task(task)
    return JsonResponse(out)


@login_required
@require_POST
def task_trigger(request):
    """POST /tasks/trigger/ {"ticket": <id>, "instructions": "optional extra"}.

    Creates an agentic task for the given ticket and dispatches it to the
    runner. Returns the pending task immediately (202) so the caller can
    poll ``GET /tasks/<id>/`` for completion.
    """
    gate = _gate()
    if not gate['enabled']:
        return JsonResponse(
            {'error': 'agentic tasks disabled', **gate}, status=400
        )

    body = _json_body(request)
    tid = body.get('ticket')
    ticket = Ticket.objects.filter(pk=_safe_int(tid)).first() if tid else None
    if ticket is None:
        return JsonResponse({'error': 'valid ticket id required'}, status=400)

    extra = (body.get('instructions') or '').strip()
    try:
        task = trigger_task(ticket, user=request.user, extra_instructions=extra)
    except ValidationError as exc:
        return JsonResponse({'error': str(exc.message_dict)}, status=400)

    return JsonResponse({'task': _serialize_task(task)}, status=202)


@login_required
@require_POST
def task_cancel(request, pk):
    """POST /tasks/<pk>/cancel/ — mark a pending task as failed.

    Only works while the task is still PENDING (not yet picked up by a
    worker). Once RUNNING, cancellation is not supported in v1.
    """
    task = AgenticTask.objects.filter(pk=pk).first()
    if task is None:
        return JsonResponse({'error': 'task not found'}, status=404)
    if task.status != AgenticTask.Status.PENDING:
        return JsonResponse(
            {'error': f'cannot cancel a {task.status} task'}, status=409
        )
    from django.utils import timezone

    task.status = AgenticTask.Status.FAILED
    task.error = 'cancelled by user'
    task.completed_at = timezone.now()
    task.save(update_fields=['status', 'error', 'completed_at'])
    return JsonResponse({'task': _serialize_task(task)})