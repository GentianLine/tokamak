# Contributing to Tokamak

Thanks for helping! This project is a minimal agile planning board built on
Django + django-unicorn + DRF, where **every mutation flows through one service
layer** (`tokamak/services.py`) so signals fire consistently. Read `AGENTS.md`
for the full architecture and collaboration rules; the short version is below.

## Dev setup

```bash
python -m pip install -r requirements.txt -r requirements-dev.txt
python manage.py migrate
python manage.py seed_demo        # optional demo data (idempotent)
python manage.py runserver
```

## Lint & tests (what CI runs)

- **Format/lint:** `black --check .` — style is enforced by GitHub Actions. Run
  `black .` locally before pushing. Style config lives in `pyproject.toml`
  (single-quote house style preserved).
- **Tests:** `pytest` — the suite lives under `tests/` as function-based modules
  (see `pytest.ini`). Add new tests there rather than reintroducing class-based
  unittests. New behavior must be covered by a new test.

Both gates run on every pull request (`test.yml`, `lint.yml`).

## Conventions that matter

- All writes go through `tokamak/services.py` — never mutate model state directly
  from views/components/receivers; call the service function so signals fire.
- Story points are Fibonacci only (1, 2, 3, 5, 8, 13, 21).
- Transitions follow `services.ALLOWED_TRANSITIONS`; a feature/bug must be
  estimated before acceptance.
- Unicorn computed component values are **methods**, not properties.

## Pull requests

Branch from `main`, keep commits small and focused (`feat:` / `fix:` / `test:` /
`docs:` / `style:`), make sure `black --check .` and `pytest` pass, and describe
what the change delivers. Do not commit database files or caches.

## Setting up as a GitHub project

This repo is prepared to be pushed to GitHub: it has CI workflows under
`.github/workflows/`, an MIT `LICENSE`, `.gitattributes` for line-ending/language
stats, and pinned dev tooling. To initialize the hosted project: create the
repository on GitHub, add it as a remote (`git remote add origin <url>`), then
`git push -u origin main`. Configure branch protection to require the two status
checks (`tests`, `lint`) before merging into `main`.
