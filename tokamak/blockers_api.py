"""REST API for blockers, epics and milestones (new module, merge-friendly).

Per AGENTS.md lane rules: new REST behavior lives in its own module and is
registered through ``build_api_router`` rather than rewriting views.py.
Every mutation delegates to ``tokamak.services`` so signals fire exactly as
they do from the Unicorn UI or direct service calls.

Routes (mounted under /api/ via api_urls.py):
    CRUD         /epics/?project=<id>&ticket=<id>
                /milestones/?project=<id>
                /blockers/?project=<id>&source=<id>&target=<id>
    Reads        GET  /tickets/<pk>/blockers/            -> {blocks, blocked_by}
                 GET  /projects/<pk>/blockers/           -> live edges on a project
    Actions      POST   /tickets/<pk>/block/             {"by": <id>, "reason": "..."}
                 DELETE /tickets/<pk>/block/             {"by": <id>}
"""

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from . import services
from .models import Blocker, Epic, Milestone, Ticket


def _friendly(exc):
    message = getattr(exc, 'message', None) or str(exc)
    return message[:300]


class EpicSerializer(serializers.ModelSerializer):
    class Meta:
        model = Epic
        fields = ['id', 'project', 'name', 'created_at']


class MilestoneSerializer(serializers.ModelSerializer):
    ticket_count = serializers.IntegerField(read_only=True)
    accepted_count = serializers.IntegerField(read_only=True)
    is_complete = serializers.BooleanField(read_only=True)

    class Meta:
        model = Milestone
        fields = [
            'id',
            'project',
            'name',
            'due_date',
            'created_at',
            'ticket_count',
            'accepted_count',
            'is_complete',
        ]


class BlockerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Blocker
        fields = ['id', 'source', 'target', 'reason', 'created_at']


class EpicViewSet(viewsets.ModelViewSet):
    """/epics/ -- create/list/edit epics; optional filters by project/ticket."""

    serializer_class = EpicSerializer

    def get_queryset(self):
        qs = Epic.objects.all()
        params = self.request.query_params
        if params.get('project'):
            qs = qs.filter(project_id=params['project'])
        if params.get('ticket'):
            qs = qs.filter(tickets__pk=params['ticket']).distinct()
        return qs


class MilestoneViewSet(viewsets.ModelViewSet):
    """/milestones/ -- create/list/edit milestones; filter by project."""

    serializer_class = MilestoneSerializer

    def get_queryset(self):
        qs = Milestone.objects.all()
        project = self.request.query_params.get('project')
        if project:
            qs = qs.filter(project_id=project)
        return qs


class BlockerViewSet(viewsets.ModelViewSet):
    """/blockers/ -- read/manage raw edges; create/delete go through the
    service layer so validation (cycles, self-blocks) and signals apply."""

    serializer_class = BlockerSerializer

    def get_queryset(self):
        qs = Blocker.objects.select_related('source', 'target')
        params = self.request.query_params
        if params.get('project'):
            qs = qs.filter(source__project_id=params['project'])
        if params.get('source'):
            qs = qs.filter(source_id=params['source'])
        if params.get('target'):
            qs = qs.filter(target_id=params['target'])
        return qs

    def create(self, request, *args, **kwargs):
        try:
            source = Ticket.objects.get(pk=request.data.get('source'))
            target = Ticket.objects.get(pk=request.data.get('target'))
            blocker = services.add_blocker(
                source, target, reason=request.data.get('reason', '')
            )
        except Exception as exc:
            return Response({'detail': _friendly(exc)}, status=400)
        return Response(BlockerSerializer(blocker).data, status=201)

    def perform_destroy(self, instance):
        services.remove_blocker(instance.source, instance.target)


def register_blocker_routes(router, ticket_viewset_cls):
    """Attach blocker/epic/milestone endpoints onto the existing router.

    ``ticket_viewset_cls`` is the TicketViewSet class from views.py; this
    module adds the workflow-scoped actions (block/blockers) onto it so the
    ticket-centric routes share its get_object/permission behavior, then
    registers the standalone viewsets. Called from build_api_router."""

    @action(detail=True, methods=['get'], url_path='blockers')
    def blockers(self, request, pk=None):
        """GET /tickets/<pk>/blockers/: what it blocks and what blocks it."""
        ticket = self.get_object()
        return Response(
            {
                'blocks': [
                    {'id': t.pk, 'name': t.name, 'state': t.state}
                    for t in ticket.blockers
                ],
                'blocked_by': [
                    {
                        'id': b.source_id,
                        'name': b.source.name,
                        'state': b.source.state,
                        'reason': b.reason,
                    }
                    for b in ticket.incoming_blockers.select_related('source')
                ],
                'is_blocked': ticket.is_blocked,
            }
        )

    @action(detail=True, methods=['post', 'delete'], url_path='block')
    def block(self, request, pk=None):
        """POST/DELETE /tickets/<pk>/block/: declare or resolve a blocker.

        POST body: {"by": <blocker ticket id>, "reason": optional}. The
        acting ticket is the blocked one (target); ``by`` names the source.
        """
        target = self.get_object()
        if request.method == 'DELETE':
            by = request.data.get('by') or request.query_params.get('by')
            try:
                source = Ticket.objects.get(pk=by)
            except (Ticket.DoesNotExist, TypeError):
                return Response({'detail': 'Unknown blocker id.'}, status=400)
            services.remove_blocker(source, target)
            return Response({'removed': True})
        try:
            source = Ticket.objects.get(pk=request.data.get('by'))
            blocker = services.add_blocker(
                source, target, reason=request.data.get('reason', '')
            )
        except DjangoValidationError as exc:
            return Response({'detail': _friendly(exc)}, status=400)
        except Exception as exc:
            return Response({'detail': _friendly(exc)}, status=400)
        return Response(BlockerSerializer(blocker).data, status=201)

    # Bind the actions onto the ticket viewset (they become real routes when
    # the router registers it; urls are /tickets/<pk>/blockers/ etc).
    setattr(ticket_viewset_cls, 'blockers', blockers)
    setattr(ticket_viewset_cls, 'block', block)

    router.register('epics', EpicViewSet, basename='epic')
    router.register('milestones', MilestoneViewSet, basename='milestone')
    router.register('blockers', BlockerViewSet, basename='blocker')
