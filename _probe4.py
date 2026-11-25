import os, django, traceback
os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings')
os.environ['DJANGO_SECRET_KEY']='x'; os.environ['DJANGO_ALLOWED_HOSTS']='localhost'
django.setup()
from django.test.utils import setup_test_environment, get_runner
setup_test_environment()
runner = get_runner(django.conf.settings)()
old = runner.setup_databases()
import logging; logging.disable(logging.CRITICAL)
from tokamak.models import Ticket, EventLog
from tests._utils import make_project
from tokamak import services

# Wrap every receiver to catch exceptions
import django.dispatch
orig_send = django.dispatch.Signal.send
def traced_send(self, *a, **k):
    sender = a[0] if a else k.get('sender')
    for r in self.receivers:
        pass
    return orig_send(self, *a, **k)

# Simpler: connect a probe receiver that logs, then manually fire and catch
from tokamak.signals import ticket_state_changed
def probe(sender, **kw):
    print("  [probe] ticket_state_changed fired")
ticket_state_changed.connect(probe, dispatch_uid='probe')

p = make_project('P')
t = services.create_ticket(p, name='A')
print("EventLog after create:", list(EventLog.objects.values_list('name',flat=True)))
try:
    services.transition_ticket(t, Ticket.State.STARTED)
except Exception as e:
    print("transition raised:", repr(e)); traceback.print_exc()
print("EventLog after transition:", list(EventLog.objects.values_list('name',flat=True)))
runner.teardown_databases(old)
