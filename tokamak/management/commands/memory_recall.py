"""Search a project's clm-mem memory session (live window + archive).

    python manage.py memory_recall "deploy port" --project 3
    python manage.py memory_recall --project 3 --limit 10        # list live window
    python manage.py memory_recall --project 3 --reinject 7      # pull a block back

Run: python manage.py memory_recall
"""

from django.core.management.base import BaseCommand, CommandError

from tokamak import memory


class Command(BaseCommand):
    help = 'Recall from (or reinject into) a project memory session.'

    def add_arguments(self, parser):
        parser.add_argument('query', nargs='?', default='', help='Substring to search.')
        parser.add_argument('--project', type=int, required=True)
        parser.add_argument('--limit', type=int, default=20)
        parser.add_argument(
            '--reinject', type=int, help='Reinject this archived block id and exit.'
        )

    def handle(self, *args, **options):
        if not memory.is_enabled():
            raise CommandError('bridge disabled: set settings.TOKAMAK_MEMORY')
        try:
            if options['reinject'] is not None:
                ok = memory.reinject(options['project'], options['reinject'])
                msg = 'reinjected' if ok else 'block id not in archive'
                style = self.style.SUCCESS if ok else self.style.WARNING
                self.stdout.write(style(f'{msg}: #{options["reinject"]}'))
                return
            hits = memory.recall(options['project'], options['query'], options['limit'])
        except memory.MemoryUnavailable as exc:
            raise CommandError(str(exc))
        if not hits:
            self.stdout.write('no hits')
            return
        for h in hits:
            where = 'LIVE' if h.get('live') else 'ARCHIVE'
            self.stdout.write(
                f"#{h.get('id')} [{where} {h.get('tag','?')}] {h.get('content','')[:160]}"
            )
