"""REST API for ticket threads (new module, merge-friendly).

Per AGENTS.md lane rules: new REST behavior lives in its own module and is
registered through ``build_api_router`` rather than rewriting views.py.
Every mutation delegates to ``tokamak/services.py`` so the same
``comment.added`` / ``comment.updated`` / ``comment.removed`` events fire no
matter whether a browser, a script or an automated processor made the call.

Routes (mounted under /api/ via api_urls.py):
    CRUD         /comments/?ticket=<id>&author=<handle>&kind=human|system
                /comments/<pk>/                 DELETE is a *soft* delete
    Reads        GET   /tickets/<pk>/thread/            live thread + counts
    Actions      POST  /tickets/<pk>/comment/          {"body": ..., "author": ...}
                PATCH /comments/<pk>/                  body-only edit
    Stats        GET   /projects/<pk>/threads/         discussion health

Auth note: like the rest of this project's API, no permission layer exists
yet (tracked idea `auth-permissions`); authors are free-form handles.
"""

from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import JsonResponse

from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from . import services
from .models import Comment, Ticket


def _friendly(exc):
    message = getattr(exc, 'message', None) or str(exc)
    return message[:300]


class CommentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Comment
        fields = [
            'id',
            'ticket',
            'author',
            'body',
            'kind',
            'deleted',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['deleted', 'created_at', 'updated_at']

    def create(self, validated_data):
        try:
            return services.add_comment(
                validated_data['ticket'],
                validated_data.get('body', ''),
                author=validated_data.get('author', ''),
                kind=validated_data.get('kind', Comment.Kind.HUMAN),
            )
        except DjangoValidationError as exc:
            raise serializers.ValidationError(
                getattr(exc, 'message_dict', {'detail': str(exc)})
            )

    def update(self, instance, validated_data):
        body = validated_data.get('body')
        if body is None or instance.deleted:
            # Nothing to rewrite: bodies are the only editable part of a
            # comment, and removed ones are frozen.
            return instance
        return services.edit_comment(instance, body)


class CommentViewSet(viewsets.ModelViewSet):
    """/comments/ — read/list comments; create/update/delete through services.

    Filters: ?ticket=<id>, ?author=<handle>, ?kind=human|system and
    ?deleted=true|false (default hides removed comments)."""

    serializer_class = CommentSerializer

    def get_queryset(self):
        qs = Comment.objects.select_related('ticket')
        params = self.request.query_params
        if params.get('ticket'):
            qs = qs.filter(ticket_id=params['ticket'])
        if params.get('author'):
            qs = qs.filter(author=params['author'])
        if params.get('kind'):
            qs = qs.filter(kind=params['kind'])
        if params.get('deleted') not in ('true', '1', 'all'):
            qs = qs.filter(deleted=False)
        return qs

    def perform_destroy(self, instance):
        services.delete_comment(instance)


def register_comment_routes(router, ticket_viewset_cls):
    """Attach thread endpoints onto the router and the ticket viewset.

    Mirrors ``blockers_api.register_blocker_routes``: the ticket-scoped
    actions bind to TicketViewSet so they share its get_object behavior,
    then the standalone comment viewset registers."""

    @action(detail=True, methods=['get'], url_path='thread')
    def thread(self, request, pk=None):
        """GET /tickets/<pk>/thread/: live comments plus removed count."""
        ticket = self.get_object()
        live = services.ticket_thread(ticket)
        return Response(
            {
                'ticket': ticket.pk,
                'comments': CommentSerializer(live, many=True).data,
                'removed': ticket.comments.filter(deleted=True).count(),
            }
        )

    @action(detail=True, methods=['post'], url_path='comment')
    def comment(self, request, pk=None):
        """POST /tickets/<pk>/comment/: {"body": "...", "author": optional}.

        The simplest write surface for automation: one call, no serializer
        ceremony, service-layer validation and signals exactly as the UI."""
        ticket = self.get_object()
        try:
            made = services.add_comment(
                ticket,
                request.data.get('body', ''),
                author=request.data.get('author', ''),
                kind=request.data.get('kind', Comment.Kind.HUMAN),
            )
        except Exception as exc:
            return Response({'detail': _friendly(exc)}, status=400)
        return Response(CommentSerializer(made).data, status=201)

    setattr(ticket_viewset_cls, 'thread', thread)
    setattr(ticket_viewset_cls, 'comment', comment)

    router.register('comments', CommentViewSet, basename='comment')


def project_thread_stats(request, pk):
    """GET /projects/<pk>/threads/: discussion health for one project."""
    from .models import Project

    try:
        project = Project.objects.get(pk=pk)
    except Project.DoesNotExist:
        return JsonResponse({'detail': 'Unknown project.'}, status=404)
    stats = services.comment_stats(project)
    stats['quiet_backlog'] = [
        {'id': t.pk, 'name': t.name, 'state': t.state} for t in stats['quiet_backlog']
    ]
    return JsonResponse({'project': project.pk, **stats})
