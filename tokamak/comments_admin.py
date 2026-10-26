"""Admin surface for ticket threads (new module, merge-friendly).

Registered from ``tokamak/admin.py`` in one append line so the existing admin
classes stay byte-identical. Read-only by design: comments are written by the
service layer (detail page, REST API or automation), and letting Django admin
edit them directly would bypass the `comment.*` signals and desync the audit
trail — so every field is readonly here, the same posture as the append-only
``EventLogAdmin``.
"""

from django.contrib import admin

from .models import Comment


@admin.register(Comment)
class CommentAdmin(admin.ModelAdmin):
    """Read-only view over ticket threads."""

    list_display = ('created_at', 'ticket', 'author', 'kind', 'deleted')
    list_filter = ('kind', 'deleted', 'ticket__project')
    search_fields = ('body', 'author')
    readonly_fields = [f.name for f in Comment._meta.fields]

    # Every field is readonly, so nothing here can write: comments are created,
    # edited and removed by the service layer only (detail page, REST API or
    # automation), never by an admin form that would skip `comment.*` signals.
