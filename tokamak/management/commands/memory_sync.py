"""Mirror EventLog history into per-project clm-mem memory sessions.

Incremental by default (a watermark file remembers the last mirrored event);
use --replay-all to rebuild a session from scratch. Run it manually, from
cron, or after a live-mirror outage to catch a workspace up.

    python manage.py memory_sync                 # new events only
    python manage.py memory_sync --replay-all    # rebuild every project session
    python manage.py memory_sync --project 3     # one project
    python manage.py memory_sync --dry-run       # count without mirroring

Run: python manage.py memory_sync
"""

from django.core.management.base import BaseCommand, CommandError

from tokamak import memory
from tokamak.models import EventLog


class Command(BaseCommand):
    help = 'Feed EventLog rows into clm-mem project memory sessions.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--replay-all',
            action='store_true',
            help='Ignore the watermark and mirror all history again.',
        )
        parser.add_argument('--project', type=int, help='Limit to one project id.')
        parser.add_argument('--dry-run', action='store_true', help='Count only.')

    def handle(self, *args, **options):
        if not memory.is_enabled():
            raise CommandError(
                'bridge disabled: set settings.TOKAMAK_MEMORY = {enabled: True, ...}'
            )
        if not memory.clm_mem_available():
            raise CommandError('clm-mem is not installed (pip install clm-mem)')

        qs = EventLog.objects.order_by('id')
        if options['project']:
            qs = qs.filter(project_id=options['project'])
        if not options['replay_all'] and not options['dry_run']:
            last = memory.load_watermark()
            if last is not None:
                qs = qs.filter(id__gt=int(last))

        if options['dry_run']:
            self.stdout.write(f'would mirror {qs.count()} event(s)')
            return

        mirrored = 0
        last_id = None
        for event in qs:
            try:
                memory.remember_event(event)
            except memory.MemoryUnavailable as exc:
                raise CommandError(f'mirror failed on event {event.id}: {exc}')
            mirrored += 1
            last_id = event.id
        if last_id is not None and not options['replay_all']:
            memory.save_watermark(last_id)
        elif options['replay_all']:
            # full rebuild: persist the watermark at the newest row anyway
            newest = EventLog.objects.order_by('-id').first()
            if newest:
                memory.save_watermark(newest.id)
        self.stdout.write(self.style.SUCCESS(f'mirrored {mirrored} event(s)'))
