from django.shortcuts import get_object_or_404, render
from django.urls import path
from django.views.decorators.csrf import ensure_csrf_cookie

from .md_views import markdown_preview, markdown_suggest
from .models import Project
from .pages import (
    project_create as project_create_view,
    project_done as project_done_view,
    ticket_create as ticket_create_view,
    ticket_detail as ticket_detail_view,
)


# Unicorn POSTs to /unicorn/message with the CSRF token taken from the
# cookie, so every page hosting a component must guarantee that cookie.
@ensure_csrf_cookie
def project_index(request):
    return render(request, 'tokamak/index.html')


@ensure_csrf_cookie
def project_detail(request, pk):
    project = get_object_or_404(Project, pk=pk)
    return render(request, 'tokamak/project.html', {'project': project})


urlpatterns = [
    path('', project_index, name='project-index'),
    path('projects/new/', project_create_view, name='project-create'),
    path('projects/<int:pk>/', project_detail, name='project-detail'),
    path(
        'projects/<int:pk>/tickets/new/',
        ticket_create_view,
        name='ticket-create',
    ),
    path('tickets/<int:pk>/', ticket_detail_view, name='ticket-detail'),
    path('projects/<int:pk>/done/', project_done_view, name='project-done'),
    path('markdown/preview/', markdown_preview, name='markdown-preview'),
    path('markdown/suggest/', markdown_suggest, name='markdown-suggest'),
]
