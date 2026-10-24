"""REST surface for the clm-mem memory bridge (new module, merge-friendly).

Routes (registered in ``tokamak/api_urls.py`` under /api/):

    GET   /memory/status/?project=<id>          live-window snapshot or config
    POST  /memory/recall/                       {"project": 1, "query": "...",
                                                 "reinject_block": 7}
    POST  /memory/note/                          {"project": 1, "text": "..."}

GET answers 200 with an ``enabled`` flag so callers can always tell whether
memory is wired in; write endpoints return 400 when the bridge is off — it
never becomes a silent failure.

Auth note: like the rest of this project's API, no permission layer exists
yet ("auth/permissions before deploying" is a tracked idea); keep the bridge
disabled in production until that lands.
"""

import json

from django.http import JsonResponse, QueryDict
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from . import memory


def _json_body(request):
    try:
        return json.loads(request.body.decode('utf-8') or '{}')
    except ValueError:
        return {}


@require_GET
def memory_status(request):
    """GET /memory/status/?project=<id> — config, availability, live snapshot."""
    cfg = memory.get_config()
    out = {
        'enabled': memory.is_enabled(),
        'mode': cfg['mode'],
        'clm_mem_installed': memory.clm_mem_available(),
        'mirror': cfg['mirror'],
    }
    qd = QueryDict(request.META.get('QUERY_STRING', ''))
    pid = qd.get('project')
    if memory.is_enabled() and pid:
        try:
            out['snapshot'] = memory.session_status(int(pid))
        except memory.MemoryUnavailable as exc:
            out['error'] = str(exc)
    return JsonResponse(out)


@csrf_exempt
@require_POST
def memory_recall(request):
    """POST /memory/recall/ — search; optionally reinject an archived block."""
    body = _json_body(request)
    pid = body.get('project')
    if not pid:
        return JsonResponse({'error': 'project required'}, status=400)
    if not memory.is_enabled():
        return JsonResponse({'enabled': False, 'error': 'bridge disabled'}, status=400)
    try:
        if body.get('reinject_block') is not None:
            ok = memory.reinject(int(pid), int(body['reinject_block']))
            return JsonResponse({'enabled': True, 'reinjected': ok})
        hits = memory.recall(int(pid), body.get('query', ''), 50)
        return JsonResponse({'enabled': True, 'hits': hits})
    except memory.MemoryUnavailable as exc:
        return JsonResponse({'error': str(exc)}, status=400)


@csrf_exempt
@require_POST
def memory_note(request):
    """POST /memory/note/ — inject a durable longterm block into a session.

    This is the "agent writes memory" path: an LLM processor (or a human at
    the command line) plants a fact that survives as long-term memory,
    independent of the event mirror.
    """
    body = _json_body(request)
    pid, text = body.get('project'), (body.get('text') or '').strip()
    if not pid or not text:
        return JsonResponse({'error': 'project and text required'}, status=400)
    if not memory.is_enabled():
        return JsonResponse({'enabled': False, 'error': 'bridge disabled'}, status=400)
    try:
        harness = memory.open_session(int(pid))
        if hasattr(harness, 'add'):
            block = harness.add(role='user', content=text, tag='longterm')
            harness.evict()
            harness.mirror_files()
            block_id = block.id
        else:  # served mode: server-side classifier decides the tag
            harness.step(text)
            block_id = None
        return JsonResponse({'enabled': True, 'block_id': block_id})
    except memory.MemoryUnavailable as exc:
        return JsonResponse({'error': str(exc)}, status=400)
