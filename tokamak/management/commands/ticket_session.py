"""Manage ticket-scoped clm-mem sessions (see INTEGRATION.md lifecycle).

    python manage.py ticket_session --list --ticket 3
    python manage.py ticket_session --open-ticket 3
    python manage.py ticket_session --close-session 1 --reason manual
    python manage.py ticket_session --summary 1            # print distilled summary

Run: python manage.py ticket_session
"""

from django.core.management.base import BaseCommand, CommandError

from tokamak import memory as bridge
from tokamak import services


class Command(BaseCommand):
    help = 'Open/close/list clm-mem sessions attached to tickets.'

    def add_arguments(self, parser):
        parser.add_argument('--list', action='store_true', help='List sessions.')
        parser.add_argument('--ticket', type=int, help='Filter list by ticket id.')
        parser.add_argument('--status', default='', help='open|ended filter.')
        parser.add_argument(
            '--open-ticket', type=int, help='Open a session for this ticket.'
        )
        parser.add_argument('--close-session', type=int, help='Close this session id.')
        parser.add_argument('--reason', default='manual')
        parser.add_argument(
            '--summary', type=int, help='Print the summary of a session id.'
        )

    def handle(self, *args, **options):
        from tokamak.models import MemorySession, Ticket

        if options['summary'] is not None:
            sess = MemorySession.objects.filter(pk=options['summary']).first()
            if sess is None:
                raise CommandError('session not found')
            self.stdout.write(sess.summary or '(no summary)')
            return

        if options['open_ticket'] is not None:
            ticket = Ticket.objects.filter(pk=options['open_ticket']).first()
            if ticket is None:
                raise CommandError('ticket not found')
            try:
                sess = services.open_ticket_session(ticket)
            except Exception as exc:
                raise CommandError(str(exc))
            self.stdout.write(
                self.style.SUCCESS(
                    f'opened {sess.session_key} -> monitor {sess.monitor_url}'
                )
            )
            return

        if options['close_session'] is not None:
            sess = MemorySession.objects.filter(pk=options['close_session']).first()
            if sess is None:
                raise CommandError('session not found')
            try:
                sess = services.close_ticket_session(
                    sess, reason=options['reason'] or 'manual'
                )
            except Exception as exc:
                raise CommandError(str(exc))
            self.stdout.write(
                self.style.SUCCESS(
                    f'closed #{sess.pk} reason={sess.end_reason} '
                    f'summary={len(sess.summary)} chars, archive='
                    f"{sess.payload.get('archive', '?')}"
                )
            )
            return

        if options['list']:
            if not bridge.is_enabled():
                self.stdout.write('(bridge disabled; sessions listed from DB anyway)')
            qs = MemorySession.objects.all()
            if options['ticket']:
                qs = qs.filter(ticket_id=options['ticket'])
            if options['status']:
                qs = qs.filter(status=options['status'])
            for s in qs:
                self.stdout.write(
                    f"#{s.pk} {s.session_key} [{s.status}] "
                    f"ticket={s.ticket_id} reason={s.end_reason or '-'} "
                    f"monitor={s.monitor_url}"
                )
            if not qs:
                self.stdout.write('(no sessions)')
            return

        raise CommandError(
            'nothing to do: use --list / --open-ticket / ' '--close-session / --summary'
        )
