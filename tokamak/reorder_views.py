"""REST surface for drag-reorder (registered in ``tokamak/api_urls.py``).

Routes:
    POST /reorder/                reorder_ticket_view   name='reorder-ticket'
        body: {"ticket": <id>, "position": <int>}
        -> 200 {"ok": true, "ticket": <id>, "group": ..., "order": [...]}

Thin view over ``tokamak.reorder`` — the same service the Unicorn board
component calls, so UI drags and automation produce identical writes and
audit rows.
"""

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import Ticket
from .reorder import group_for_state, reorder_ticket


def _json_body(request):
    import json

    try:
        return json.loads(request.body.decode('utf-8') or '{}')
    except ValueError:
        return {}


@csrf_exempt
@require_POST
def reorder_ticket_view(request):
    """Persist a manual ordering move; errors answer 400 like the API style."""
    from django.core.exceptions import ValidationError, ObjectDoesNotExist

    body = _json_body(request)
    ticket_id = body.get('ticket')
    position = body.get('position', body.get('target_position'))
    try:
        ticket = Ticket.objects.get(pk=ticket_id)
    except (ObjectDoesNotExist, TypeError, ValueError):
        return JsonResponse({'detail': 'Unknown ticket.'}, status=400)
    if position is None:
        return JsonResponse({'detail': 'position is required.'}, status=400)
    try:
        reorder_ticket(ticket, position)
    except ValidationError as exc:
        return JsonResponse({'detail': str(exc)}, status=400)
    group = group_for_state(ticket.state)
    from .reorder import _group_tickets

    order = [t.pk for t in _group_tickets(ticket.project, group)]
    return JsonResponse(
        {'ok': True, 'ticket': ticket.pk, 'group': group, 'order': order}
    )
