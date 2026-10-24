"""AJAX endpoints powering the markdown editor widget.

Routes (registered in ``tokamak/urls.py``):
    POST /markdown/preview/   markdown_preview   name='markdown-preview'
    GET  /markdown/suggest/   markdown_suggest   name='markdown-suggest'

Both are read-only: preview renders through ``tokamak.markdown`` (never
mutates, HTML-escaped by design), suggest answers reference-autocomplete
queries from ticket/project names. The preview endpoint is CSRF-protected;
the widget sends the token from the cookie the page already sets.
"""

from django.http import JsonResponse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from . import markdown as mdw
from .models import Project, Ticket


@require_POST
@ensure_csrf_cookie
def markdown_preview(request):
    """Render one markdown blob into HTML for the live preview pane."""
    text = request.POST.get('text', '')
    return JsonResponse({'html': mdw.render(text)})


def _numeric_or_name(model, q):
    """Exact-pk hit plus name matches for a suggest query ``q``."""

    if not q:
        return model.objects.none()
    hits = model.objects
    if q.isdigit():
        hits = hits.filter(pk=int(q[:9])) | hits.filter(name__icontains=q)
    else:
        hits = hits.filter(name__icontains=q)
    return hits.order_by('name')


@require_GET
def markdown_suggest(request):
    """Name-suggestions for ``#``/``ticket:``/``project:`` autocomplete.

    Query params: ``kind`` (ticket|project, default ticket) and ``q`` -- a
    case-insensitive substring of the name, or a bare number that also
    matches an exact id.
    """
    kind = request.GET.get('kind', 'ticket')
    q = (request.GET.get('q') or '').strip()[:100]

    if kind == 'project':
        rows = _numeric_or_name(Project, q)
        items = [
            {
                'id': p.pk,
                'label': p.name,
                'ref': f'project:{p.pk}',
                'url': f'/projects/{p.pk}/',
            }
            for p in rows[:10]
        ]
    else:
        items = [
            {
                'id': t.pk,
                'label': t.name,
                'ref': f'#{t.pk}',
                'url': f'/tickets/{t.pk}/',
            }
            for t in _numeric_or_name(Ticket, q)[:10]
        ]
    return JsonResponse({'items': items})
