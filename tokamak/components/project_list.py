"""Unicorn component: home page project list + inline project creation."""

from django_unicorn.components import UnicornView

from .. import services
from ..models import Project


class ProjectListView(UnicornView):
    template_name = 'unicorn/project_list.html'

    new_name: str = ''
    new_description: str = ''
    error: str = ''

    # Method (not property) so Unicorn does not try to serialize/hydrate it.
    def projects(self):
        return Project.objects.prefetch_related('tickets', 'iterations')

    def create_project(self):
        try:
            project = services.create_project(self.new_name, self.new_description)
        except Exception as exc:  # ValidationError etc.
            messages = getattr(exc, 'messages', None)
            self.error = (messages[0] if messages else str(exc))[:300]
            return
        self.new_name = ''
        self.new_description = ''
        self.error = ''
        self.redirect(f'/projects/{project.pk}/')
