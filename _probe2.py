import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings')
os.environ['DJANGO_SECRET_KEY']='x'; os.environ['DJANGO_ALLOWED_HOSTS']='localhost'
django.setup()
from rest_framework.settings import api_settings
print("DEFAULT_PERMISSION_CLASSES:", api_settings.DEFAULT_PERMISSION_CLASSES)
print("DEFAULT_AUTHENTICATION_CLASSES:", api_settings.DEFAULT_AUTHENTICATION_CLASSES)
