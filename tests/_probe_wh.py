import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from django.test.utils import override_settings

import pytest

from tokamak import services, webhooks
from tests._utils import make_project


@pytest.mark.django_db
def test_probe(db):
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get('Content-Length', 0))
            self.rfile.read(n)
            print('SERVER GOT POST', flush=True)
            self.send_response(202)
            self.end_headers()

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(('127.0.0.1', 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{httpd.server_address[1]}/hook'
    cfg = {
        'enabled': True,
        'targets': [{'url': url, 'allow_private': True}],
        'timeout': 2.0,
        'max_retries': 3,
        'retry_backoff': 0.01,
        'async': False,
    }
    from tokamak import signals
    from tokamak.models import EventLog
    # Is the audit receiver for ticket_created alive right after ready()?}
    status = []
    for key, ref, _sender, _weak in signals.ticket_created.receivers:
        uid = key[0] if isinstance(key, tuple) else 'id'
        status.append((uid, 'ALIVE' if ref() is not None else 'DEAD'))
    print('RECEIVER STATUS:', status, flush=True)
    with override_settings(TOKAMAK={'WEBHOOKS': cfg}):
        webhooks.register_fanout()
        rcv = signals.ticket_created.receivers
        print('ticket_created receiver keys:', list(rcv), flush=True)
        project = make_project('Probe proj')
        ticket = services.create_ticket(project, name='Probe story')
        print('EVENTLOG rows:', list(EventLog.objects.values_list('name', 'ticket_id')), flush=True)
        print('LEDGER:', dict(webhooks._DELIVERED), flush=True)
    httpd.shutdown()
