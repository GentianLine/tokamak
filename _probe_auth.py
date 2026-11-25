import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings')
os.environ['DJANGO_SECRET_KEY']='x'; os.environ['DJANGO_ALLOWED_HOSTS']='localhost'
django.setup()
from django.test.utils import setup_test_environment, get_runner
setup_test_environment()
runner = get_runner(django.conf.settings)()
old = runner.setup_databases()
from django.contrib.auth.models import User
from rest_framework.test import APIClient
u = User.objects.create_user('t','t@t.com','pw')
c = APIClient(); c.force_authenticate(user=u)
print("authed GET /api/memory/status/ ->", c.get('/api/memory/status/').status_code)
c2 = APIClient()
print("unauth GET /api/memory/status/ ->", c2.get('/api/memory/status/').status_code)
runner.teardown_databases(old)
