import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings')
os.environ['DJANGO_SECRET_KEY']='x'; os.environ['DJANGO_ALLOWED_HOSTS']='localhost'
django.setup()
from django.test.utils import setup_test_environment, get_runner
setup_test_environment()
runner = get_runner(django.conf.settings)()
old = runner.setup_databases()
import logging; logging.disable(logging.CRITICAL)
from tokamak.models import Ticket
from tests._utils import make_project
from tokamak import services, signals
p = make_project('P')
t = services.create_ticket(p, name='A')
# call transition and see if EventLog appears
try:
    services.transition_ticket(t, Ticket.State.STARTED)
    print("transition OK")
except Exception as e:
    print("transition RAISED:", repr(e))
from tokamak.models import EventLog
print("EventLog rows after transition:", list(EventLog.objects.values_list('name', flat=True)))
runner.teardown_databases(old)
