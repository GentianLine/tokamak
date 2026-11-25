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

# Wrap EventLog.objects.create to trace
orig_create = EventLog.objects.create
def traced_create(*a, **k):
    try:
        obj = orig_create(*a, **k)
        print(f"  [EventLog.create] name={k.get('name')} -> pk={obj.pk}")
        return obj
    except Exception as e:
        print(f"  [EventLog.create] RAISED {k.get('name')}: {e!r}")
        raise
EventLog.objects.create = traced_create

p = make_project('P')
t = services.create_ticket(p, name='A')
print("--- after create_ticket ---")
services.transition_ticket(t, Ticket.State.STARTED)
print("--- after transition ---")
print("final EventLog count:", EventLog.objects.count())
runner.teardown_databases(old)
