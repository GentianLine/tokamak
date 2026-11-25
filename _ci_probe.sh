#!/usr/bin/env bash
# CI simulation for tokamak (mirrors .github/workflows/test.yml)
cd "$(dirname "$0")"
export DJANGO_SECRET_KEY="ci-test-secret-key-not-for-production"
export DJANGO_ALLOWED_HOSTS="testserver,localhost,127.0.0.1"
PY="$(dirname "$0")/.civenv/bin/python"
"$PY" manage.py check || exit 1
"$PY" manage.py makemigrations --check --dry-run || exit 1
"$PY" -m pytest "${@:--q}"
