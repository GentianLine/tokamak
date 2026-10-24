"""REST surface for ticket memory sessions (clm-mem bridge).

Routes (registered in ``tokamak/api_urls.py`` under /api/):

    GET    /sessions/?ticket=<id>&status=open     list sessions for a ticket
    POST   /sessions/open/                        {"ticket": <id>}
    POST   /sessions/<pk>/close/                  {"reason": "manual", "summary": "..."}
    GET    /sessions/<pk>/snapshot/               live-window monitor view

Monitoring model: in served mode ``monitor_url`` points at the clm-mem web
studio workspace (chat + eviction hints live); in local mode the snapshot
route here is the monitor — same blocks, counts and events as the studio
shows, over the bridge. The mirrored context.md / context.jsonl files are the
human-readable trail on disk.

What flows back from a session into the ticket lives in
``tokamak/services.close_ticket_session`` (summary + EventLog); these views
only drive that service layer.
"""

import json

from django.http import JsonResponse, QueryDict
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from . import memory as bridge
from . import services
from .models import MemorySession, Ticket


def _json_body(request):
    try:
        return json.loads(request.body.decode('utf-8') or '{}')
    except ValueError:
        return {}


def _serialize(sess):
    return {
        'id': sess.id,
        'ticket': sess.ticket_id,
        'session_key': sess.session_key,
        'mode': sess.mode,
        'status': sess.status,
        'started_at': sess.started_at.isoformat(),
        'ended_at': sess.ended_at.isoformat() if sess.ended_at else None,
        'end_reason': sess.end_reason,
        'monitor_url': sess.monitor_url,
        'summary': sess.summary,
        'payload': sess.payload,
    }


def _bridge_gate():
    """Two-level gate: bridge enabled AND ticket_sessions sub-switch on."""
    cfg = bridge.get_config()
    return {
        'enabled': bool(cfg['enabled'] and cfg.get('ticket_sessions')),
        'bridge_enabled': bridge.is_enabled(),
    }


@require_GET
def session_list(request):
    """GET /sessions/?ticket=<id>[&status=open|ended] — list + latest summary."""
    qd = QueryDict(request.META.get('QUERY_STRING', ''))
    out = _bridge_gate()
    tid = qd.get('ticket')
    if not tid:
        return JsonResponse({'error': 'ticket required', **out}, status=400)
    qs = MemorySession.objects.filter(ticket_id=int(tid))
    status = qd.get('status')
    if status:
        qs = qs.filter(status=status)
    latest = (
        services.latest_session_summary(Ticket.objects.filter(pk=int(tid)).first())
        if tid
        else None
    )
    out.update(
        sessions=[_serialize(s) for s in qs],
        latest=(_serialize(latest) if latest else None),
    )
    return JsonResponse(out)


@csrf_exempt
@require_POST
def session_open(request):
    """POST /sessions/open/ {"ticket": <id>} — manual session open."""
    gate = _bridge_gate()
    if not gate['enabled']:
        return JsonResponse({'error': 'ticket sessions disabled', **gate}, status=400)
    body = _json_body(request)
    tid = body.get('ticket')
    ticket = Ticket.objects.filter(pk=int(tid)).first() if tid else None
    if ticket is None:
        return JsonResponse({'error': 'ticket required / not found'}, status=400)
    try:
        sess = services.open_ticket_session(ticket)
    except Exception as exc:  # ValidationError etc.
        return JsonResponse({'error': str(exc)}, status=400)
    return JsonResponse({'session': _serialize(sess)}, status=201 if sess else 200)


@csrf_exempt
@require_POST
def session_close(request, pk=None):
    """POST /sessions/<pk>/close/ {"reason": ..., "summary": ...}."""
    gate = _bridge_gate()
    if not gate['enabled']:
        return JsonResponse({'error': 'ticket sessions disabled', **gate}, status=400)
    sess = MemorySession.objects.filter(pk=int(pk)).first()
    if sess is None:
        return JsonResponse({'error': 'session not found'}, status=404)
    body = _json_body(request)
    if sess.status == MemorySession.Status.ENDED:
        return JsonResponse({'session': _serialize(sess), 'note': 'already ended'})
    sess = services.close_ticket_session(
        sess,
        reason=body.get('reason', 'manual'),
        summary=(body.get('summary') or None),
    )
    return JsonResponse({'session': _serialize(sess)})


@require_GET
def session_snapshot(request, pk=None):
    """GET /sessions/<pk>/snapshot/ — live monitor view for one session."""
    sess = MemorySession.objects.filter(pk=int(pk)).first()
    if sess is None:
        return JsonResponse({'error': 'session not found'}, status=404)
    out = {'session': _serialize(sess)}
    if sess.status == MemorySession.Status.ENDED:
        out['note'] = 'session ended; live window closed (archive persists)'
        try:
            h = bridge.ticket_harness(sess.ticket)
            out['archive'] = h.archive_size()
        except Exception:
            pass
        return JsonResponse(out)
    if not bridge.is_enabled():
        return JsonResponse({'error': 'bridge disabled', **out}, status=400)
    try:
        h = bridge.ticket_harness(sess.ticket)
        snap = h.snapshot()
        out.update(snap)
    except bridge.MemoryUnavailable as exc:
        out['error'] = str(exc)
    return JsonResponse(out)
