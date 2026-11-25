import os

os.environ['DJANGO_SECRET_KEY'] = 'ci-test-secret-key-not-for-production'
os.environ['DJANGO_ALLOWED_HOSTS'] = 'testserver,localhost,127.0.0.1'
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django

django.setup()
from tokamak import signals  # noqa: E402

status = []
for entry in list(signals.ticket_created.receivers):
    key, ref, weak = entry[0], entry[1], entry[-1]
    uid = key[0] if isinstance(key, tuple) else 'id'
    alive = True if not weak else ref() is not None
    status.append((uid, 'ALIVE' if alive else 'DEAD'))
print('PLAIN-PROCESS RECEIVER STATUS:', status)
