"""
REST API (djangorestframework).

Every mutating endpoint delegates to ``tokamak.services``, so the same Django
signals fire whether the change comes from a browser click (Unicorn), a
curl call, or an automated processor. This API is the integration surface
for future triage/automation tooling.

Routes (mounted under /api/ in tokamak/api_urls.py):
    CRUD   /projects/
           /projects/<pk>/iterations/
           /projects/<pk>/tickets/
           /tickets/<pk>/
    Actions POST /tickets/<pk>/transition/  {"state": "started"}
            POST /tickets/<pk>/estimate/    {"points": 5}
            POST /tickets/<pk>/assign/      {"iteration": 3 | null}
    Reads   GET  /projects/<pk>/velocity/
            GET  /projects/<pk>/cutoff/
            GET  /projects/<pk>/board/
            GET  /projects/<pk>/blockers/       live edges + blocked tickets
            GET  /tickets/<pk>/blockers/        {blocks, blocked_by, is_blocked}
            POST   /tickets/<pk>/block/         {"by": <id>, "reason": optional}
            DELETE /tickets/<pk>/block/         {"by": <id>}
            GET  /epics/?project=<id>&ticket=<id>   (+ CRUD)
            GET  /milestones/?project=<id>          (+ CRUD)
            GET  /events/?project=<id>&name=ticket.state_changed
"""

import logging

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import routers, serializers, viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response

logger = logging.getLogger(__name__)


def _error_response(exc):
    """Map an exception from a ticket/blocker action to a safe response (L-4).

    Domain/validation errors are 400 with the operator-facing message; anything
    unexpected is logged and returned as a generic 500 so internal exception
    text (paths, SQL, etc.) never leaks to the client.
    """
    if isinstance(exc, (DjangoValidationError, serializers.ValidationError)):
        return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    logger.exception('tokamak API action failed: %s', exc)
    return Response(
        {'detail': 'Unexpected error while processing this request.'},
        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )

from . import services
from .blockers_api import register_blocker_routes
from .models import Blocker, EventLog, Iteration, Project, Ticket


class ProjectSerializer(serializers.ModelSerializer):
    backlog_points = serializers.IntegerField(read_only=True)
    average_velocity = serializers.FloatField(read_only=True)

    class Meta:
        model = Project
        fields = [
            'id',
            'name',
            'description',
            'archived',
            'created_at',
            'backlog_points',
            'average_velocity',
        ]


class IterationSerializer(serializers.ModelSerializer):
    accepted_points = serializers.IntegerField(read_only=True)
    committed_points = serializers.IntegerField(read_only=True)
    is_active = serializers.BooleanField(read_only=True)
    is_completed = serializers.BooleanField(read_only=True)

    class Meta:
        model = Iteration
        fields = [
            'id',
            'project',
            'number',
            'name',
            'goal',
            'start_date',
            'end_date',
            'accepted_points',
            'committed_points',
            'is_active',
            'is_completed',
        ]


class TicketSerializer(serializers.ModelSerializer):
    class Meta:
        model = Ticket
        fields = [
            'id',
            'project',
            'iteration',
            'name',
            'description',
            'type',
            'state',
            'story_points',
            'external_ref',
            'position',
            'created_at',
            'updated_at',
            'completed_at',
        ]
        read_only_fields = ['state', 'created_at', 'updated_at', 'completed_at']
        extra_kwargs = {'project': {'required': False}}

    def create(self, validated_data):
        if 'project' not in validated_data:
            raise serializers.ValidationError(
                {'project': 'project is required (or POST to /projects/<id>/tickets/).'}
            )
        try:
            return services.create_ticket(**validated_data)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(
                getattr(exc, 'message_dict', {'detail': str(exc)})
            )

    def update(self, instance, validated_data):
        # Iteration changes go through the service so signals fire.
        if (
            'iteration' in validated_data
            and validated_data['iteration'] != instance.iteration
        ):
            services.assign_iteration(instance, validated_data.pop('iteration'))
        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save()
        return instance


class TicketActionSerializer(serializers.Serializer):
    state = serializers.ChoiceField(choices=Ticket.State.choices)
    points = serializers.IntegerField(required=False, allow_null=True)
    iteration = serializers.PrimaryKeyRelatedField(
        queryset=Iteration.objects.all(), required=False, allow_null=True
    )


class ProjectViewSet(viewsets.ModelViewSet):
    queryset = Project.objects.all()
    serializer_class = ProjectSerializer

    def destroy(self, request, *args, **kwargs):
        """DELETE goes through the service layer: edges + audit first,
        not a bare cascade."""
        services.delete_project(
            self.get_object(),
            user=request.user if request.user.is_authenticated else None,
        )
        return Response(status=status.HTTP_204_NO_CONTENT)

    def get_tickets(self):
        return Ticket.objects.filter(project_id=self.kwargs['pk'])

    @action(detail=True, methods=['get'], url_path='board')
    def board(self, request, pk=None):
        """Whole board in one payload: {state: [tickets...]} plus forecast."""
        columns = {
            state: TicketSerializer(
                self.get_tickets().filter(state=state), many=True
            ).data
            for state in Ticket.State.values
        }
        return Response(
            {
                'project': ProjectSerializer(self.get_object()).data,
                'columns': columns,
                'forecast': services.estimate_cutoff(self.get_object()),
            }
        )

    @action(detail=True, methods=['get'])
    def velocity(self, request, pk=None):
        project = self.get_object()
        return Response(
            {
                'average_velocity': services.average_velocity(project),
                'series': services.velocity_series(project),
                'iteration_length_days': services.iteration_length_days(project),
            }
        )

    @action(detail=True, methods=['get'])
    def cutoff(self, request, pk=None):
        return Response(services.estimate_cutoff(self.get_object()))

    @action(detail=True, methods=['get'], url_path='blockers')
    def blockers(self, request, pk=None):
        """Live blocker edges on this project (red-flag view)."""
        project = self.get_object()
        edges = (
            Blocker.objects.filter(source__project=project)
            .exclude(source__state__in=(Ticket.State.ACCEPTED, Ticket.State.REJECTED))
            .select_related('source', 'target')
        )
        blocked = [
            t
            for t in project.tickets.exclude(
                state__in=(Ticket.State.ACCEPTED, Ticket.State.REJECTED)
            ).prefetch_related('incoming_blockers__source')
            if t.is_blocked
        ]
        return Response(
            {
                'edges': [
                    {
                        'id': b.pk,
                        'source': {'id': b.source_id, 'name': b.source.name},
                        'target': {'id': b.target_id, 'name': b.target.name},
                        'reason': b.reason,
                    }
                    for b in edges
                ],
                'blocked_tickets': [
                    {
                        'id': t.pk,
                        'name': t.name,
                        'state': t.state,
                        'reasons': t.blocker_reasons,
                    }
                    for t in blocked
                ],
            }
        )


class ProjectNestedIterationViewSet(viewsets.ModelViewSet):
    """/projects/<project_pk>/iterations/ — create/list sprints per project."""

    serializer_class = IterationSerializer

    def get_queryset(self):
        return Iteration.objects.filter(project_id=self.kwargs['project_pk'])

    def perform_create(self, serializer):
        serializer.save(project_id=self.kwargs['project_pk'])


class ProjectNestedTicketViewSet(viewsets.ModelViewSet):
    """/projects/<project_pk>/tickets/ — create/list tickets per project."""

    serializer_class = TicketSerializer

    def get_queryset(self):
        return Ticket.objects.filter(project_id=self.kwargs['project_pk'])

    def perform_create(self, serializer):
        serializer.save(project=Project.objects.get(pk=self.kwargs['project_pk']))


class TicketViewSet(viewsets.ModelViewSet):
    """/tickets/<pk>/ plus the workflow actions."""

    queryset = Ticket.objects.all()
    serializer_class = TicketSerializer

    def _user(self, request):
        return request.user if request.user.is_authenticated else None

    def destroy(self, request, *args, **kwargs):
        """DELETE goes through the service layer (edges + audit), not a bare
        queryset delete."""
        services.delete_ticket(self.get_object(), user=self._user(request))
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=['post'])
    def transition(self, request, pk=None):
        new_state = request.data.get('state')
        try:
            ticket = services.transition_ticket(
                self.get_object(), new_state, user=self._user(request)
            )
        except Exception as exc:
            return _error_response(exc)
        return Response(TicketSerializer(ticket).data)

    @transition.mapping.get
    def transition_choices(self, request, pk=None):
        """Lets automation discover which states are legal right now."""
        from .services import ALLOWED_TRANSITIONS

        ticket = self.get_object()
        return Response(
            {
                'current': ticket.state,
                'allowed': sorted(ALLOWED_TRANSITIONS[ticket.state]),
            }
        )

    @action(detail=True, methods=['post'])
    def estimate(self, request, pk=None):
        try:
            points = request.data.get('points')
            ticket = services.estimate_ticket(self.get_object(), points)
        except Exception as exc:
            return _error_response(exc)
        return Response(TicketSerializer(ticket).data)

    @action(detail=True, methods=['post'])
    def assign(self, request, pk=None):
        try:
            iteration_id = request.data.get('iteration')
            # The service layer expects an Iteration instance (or None to
            # unassign); the REST payload carries a primary key. Resolve it so
            # cross-project / unknown ids surface as a 400 ValidationError
            # rather than an AttributeError from attribute access on an int.
            iteration = (
                Iteration.objects.filter(pk=iteration_id).first()
                if iteration_id is not None
                else None
            )
            if iteration_id is not None and iteration is None:
                raise DjangoValidationError({'iteration': 'Iteration not found.'})
            ticket = services.assign_iteration(self.get_object(), iteration)
        except Exception as exc:
            return _error_response(exc)
        return Response(TicketSerializer(ticket).data)


class EventLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = EventLog
        fields = ['id', 'name', 'project', 'ticket', 'payload', 'created_at']


class EventLogViewSet(viewsets.ReadOnlyModelViewSet):
    """Audit trail of everything the signals observed (automation-friendly)."""

    queryset = EventLog.objects.all()
    serializer_class = EventLogSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        project = self.request.query_params.get('project')
        name = self.request.query_params.get('name')
        if project:
            qs = qs.filter(project_id=project)
        if name:
            qs = qs.filter(name=name)
        return qs


def build_api_router():
    router = routers.DefaultRouter()
    register_blocker_routes(router, TicketViewSet)
    router.register('projects', ProjectViewSet, basename='project')
    router.register(
        'projects/(?P<project_pk>[^/.]+)/iterations',
        ProjectNestedIterationViewSet,
        basename='project-iteration',
    )
    router.register(
        'projects/(?P<project_pk>[^/.]+)/tickets',
        ProjectNestedTicketViewSet,
        basename='project-ticket',
    )
    router.register('tickets', TicketViewSet, basename='ticket')
    router.register('events', EventLogViewSet, basename='event')
    return router
