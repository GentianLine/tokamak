# Tokamak — Agent Handbook (READ FIRST, THEN START WORK)

This file is the single source of truth for setup, conventions, and how to
collaborate. It is committed to git; every agent gets it by cloning/checking out.
**Do not message each other about setup — read this file.** You are one agent in
a swarm working on `tokamak` concurrently. Follow the workspace protocol
exactly; it exists so nobody steps on anybody else.

## 1. What this project is

A minimal agile sprint board: Django + django-unicorn (reactive board) + DRF
(REST API), where **every** mutation flows through one service layer
(`tokamak/services.py`) that validates the workflow and fires custom signals,
and all signals are mirrored into an append-only `EventLog`.

Key files to know before touching anything:

| Area | File(s) | Notes |
|---|---|---|
| Models | `tokamak/models.py` | Project, Iteration, Ticket, EventLog |
| Service layer (single source of truth) | `tokamak/services.py` | mutations + workflow rules + velocity/cutoff math |
| Signal contract | `tokamak/signals.py` | 5 signals; payload kwarg names are part of the public API |
| Automation receivers | `tokamak/automation.py` | example processors; registered via AppConfig.ready() |
| REST API | `tokamak/views.py`, `tokamak/api_urls.py` | DRF viewsets + action endpoints |
| Reactive UI components | `tokamak/components/*.py` | UnicornViews: board, velocity, project_list |
| Templates | `templates/unicorn/*.html`, `templates/tokamak/*.html` | component templates |
| Client JS (drag & drop) | `static/js/board.js` | calls the Unicorn component; never touches DB directly |
| Styles | `static/css/app.css` | plain CSS, no build step |
| Tests | `tests/*` | pytest suite (125 tests): services+signals, forecast math, REST, unicorn components, page renders, markdown, reorder |

Non-negotiable conventions (violating these breaks other agents' work):

* **All writes go through `tokamak/services.py`.** Never mutate Ticket/Project
  state directly from views/components/receivers — call the service function.
  This is what makes signals fire consistently across UI, API, admin, and bots.
* Story points are Fibonacci only: 1, 2, 3, 5, 8, 13, 21 (`settings.TOKAMAK`).
* Transitions follow `services.ALLOWED_TRANSITIONS` (one step forward/back,
  reject from finished/accepted, rejected→started). Use `transition_path()` for
  multi-hop moves. A feature/bug must be estimated before acceptance.
* Unicorn component computed values are **methods**, not properties (Unicorn
  hydrates every public attribute; read-only properties crash on round-trip).
* Signal payload kwarg names (`ticket`, `old_state`, `new_state`, ...) are a
  contract — do not rename them.

## 2. Setup & preferences (verified working)

Python 3.13 · Django 6.1.2 · djangorestframework 3.18.3 · django-unicorn 0.67.x.
Requirements live in `requirements.txt`. Dependencies are already installed in
the sandbox environment (`pip install -r requirements.txt` on a fresh machine).

```bash
cd tokamak
python manage.py migrate            # creates db.sqlite3 (gitignored)
python manage.py seed_demo          # optional demo data; idempotent
pytest                                # the full suite, must pass before you finish
python manage.py runserver          # home / · board /projects/<id>/ · api /api/ · admin /admin/
```

* There is **no frontend build step**. CSS/JS are hand-written and served by
  Django staticfiles.
* `db.sqlite3` is gitignored on purpose: each workspace builds its own DB via
  migrate + seed_demo. Never commit database files or `__pycache__`.
* Tests run under pytest with pytest-django (see `pytest.ini`); the suite
  lives under `tests/` as function-based modules. Add new tests there rather
  than reintroducing class-based unittests.
* The optional memory bridge needs `clm-mem`, which is **not on PyPI** —
  install it from GitHub: `pip install "git+https://github.com/glassresistor/clm-mem.git"`.

### The base demo venv (`.venv/`, gitignored)

The base checkout carries a ready-to-run virtualenv at `.venv/` so humans can
demo the app (`.venv/bin/python manage.py migrate && .venv/bin/python manage.py
seed_demo && .venv/bin/python manage.py runserver`). It is *not* tracked and
lives only in the base repo directory. If it's missing, rebuild it from the
base root:

```bash
python -m venv .venv
PYTHONPATH="" PIP_TARGET="" .venv/bin/python -m pip install --ignore-installed \
    -r requirements.txt -r requirements-dev.txt \
    "git+https://github.com/glassresistor/clm-mem.git"
```

* **Keep it up to date:** if you change `requirements.txt` or
  `requirements-dev.txt`, refresh the base venv from the base repo root as
  part of your definition of done, e.g.:
  `.venv/bin/python -m pip install --ignore-installed -r requirements.txt -r requirements-dev.txt`
  (add the clm-mem git URL too). CI cannot do this for you — a stale venv is
  a broken demo.
* **Never use it from a workspace.** Agent worktrees under `workspaces/` run
  on their own interpreter (the agent sandbox environment, or a per-worktree
  venv you create there). Sharing the base `.venv` across worktrees hides
  missing dependency declarations and confuses path assumptions; the merge
  agent will treat it as a workspace hygiene violation.
* Sandbox quirk: some sandboxes export `PIP_TARGET`/`PYTHONPATH` at a shared
  package dir, which makes pip think everything is installed and silently
  skips installs. Clear them when managing any venv:
  `PYTHONPATH="" PIP_TARGET="" .venv/bin/python -m pip install --ignore-installed ...`.

## 3. Workspace protocol (anti-collision rules)

**Rule zero — claim your task before writing any code.** See §7 (catalog) and
§7.1 (runtime board + message-prefix disciplines). The runtime board is a note
(or several notes, one per prefix entry) attached to whatever commit currently
carries them: find it with `git notes --ref tasks list`, take that object id,
read the full board (`git notes --ref tasks show <that-id>`), pick an unclaimed
task from the catalog or a `TASK+` entry, then add **your own** claim note on
the same anchor commit (use `-m`; never overwrite existing notes):

```bash
# robust lookup: find where the board lives today
ANCHOR=$(git notes --ref tasks list | head -1 | awk '{print $2}')
git notes --ref tasks show "$ANCHOR"          # read full current board + claims
# claim by adding your own note on that same anchor commit:
git notes --ref tasks add -m "claimed by <agent-id>: <task> (branch agent/<...>)" "$ANCHOR"
```

**Rule one — never work in the shared checkout.** The base repo directory is a
shared coordination area owned by nobody. Create your own git workspace:

```bash
cd tokamak                                       # from inside the base repo
BR=agent/<your-id>-<slug>                      # e.g. agent/r2-burndown-chart
git worktree add ../workspaces/$BR -b $BR      # isolated checkout + its own branch
cd ../workspaces/$BR && python manage.py migrate   # your own local db.sqlite3
```

Work ONLY inside `../workspaces/<your-branch>/`. Your branch name must contain
your unique agent id, which guarantees no two agents ever share a branch.

**Rule two — commit as you go.** Make small commits with meaningful messages
(`feat: ...`, `fix: ...`, `test: ...`). Do not leave long uncommitted stretches;
other agents and the merge agent rely on your history. Commit only from your own
worktree, never against `main`.

**Rule three — file ownership lanes.** Avoid touching files outside your task's
lane. The hot files (most likely to collide) are:
`tokamak/services.py`, `tokamak/views.py`, `tests/_utils.py` (shared fixtures),
`templates/unicorn/board.html`, `static/css/app.css`, `config/settings.py`.

Lane guidance — prefer additions over edits so merges stay trivial:

* New REST behavior → new module under `tokamak/` + register in api_urls; add a
  separate test file under `tests/` (e.g. `tests/test_burndown.py`) rather
  than editing an existing one.
* New automation processor → new receiver module registered from `apps.py`, not
  inline edits to `automation.py`.
* New UI surface → new Unicorn component file + its template; do not rewrite the
  board component.
* Styles → append selectors at the END of `static/css/app.css`; never reorder or
  rename existing classes (other agents' templates depend on them).

**Rule four — finish clean.** Before finishing, your branch must: pass the full
test suite; be a coherent unit with a good final commit message summarizing what
it delivers. Then push your branch and post your result as a git note (§5). Do
not merge into main yourself — merging is the merge agent's job.

## 4. Definition of done (per task)

1. `pytest` passes in YOUR workspace; run it on an unmodified
   baseline before starting, then again after your changes.
2. New behavior is covered by new tests; new endpoints documented in README or
   in your final commit message.
3. Final state: committed (`git status` clean), branch merge-ready, RESULTS note
   posted (§5).

## 5. Coordination artifacts (git notes)

All cross-agent communication is via **git notes** so it lives in git and never
conflicts with code merges:

* `tasks` ref — task board + claims. The seed note lives on commit `main`; each
  claim is a separate note added on that same commit (`git notes --ref tasks add
  -m "..." main`). Mark done by adding another note when finished.
* `results` ref — per-agent completion summary added on **your final commit**
  (branch tip): what was delivered, test evidence (paste the final test-run
  tail), risks/notes for the merge agent. Use `-f` if your branch already has a
  note and you need to replace it.

Example (run from inside your worktree after committing your final change):

```bash
git notes --ref results add -m "<summary>" HEAD   # note on your branch tip commit
```

## 6. Merge agent expectations

The merge agent integrates branches onto `main` after the swarm finishes: apply
each branch (`git merge` or cherry-picks), resolve conflicts by respecting
service-layer/signal conventions, run the combined suite, and record integration
notes under `--ref merges`. Agents must leave history clean enough that this is
mechanical.

## 7. Task board (seeded)

Canonical catalog of planned tasks. Agents claim from this list via git notes
(see Rule Zero); humans extend it by editing this table + committing.

| id | task | lane | notes |
|---|---|---|---|
| burndown-chart | Per-iteration burndown visualization on the velocity component | UI/templates+css | use `services.velocity_series` committed vs accepted |
| cutoff-tweaks | Surface pessimistic cutoff/confidence in board template + API docs | templates | forecast fields already exist in `estimate_cutoff` |
| api-docs-refresh | Document REST endpoints incl. discovery behavior (README/docstrings) | docs | no logic changes |
| automation-webhooks | Webhook fan-out receiver mirroring EventLog entries (config-driven, off by default) | automation | new module; receivers idempotent |
| drag-reorder | Drag-reorder within a column using `Ticket.position` | board+services | add service fn + tests |
| test-coverage-lift | Add missing edge-case tests in separate files (rejected→started, cross-project assign) | tests | do not edit existing test file |
| admin-event-analytics | Admin view: event analytics per project/name counts | admin | new proxies/queries only |
| css-polish | Append-only style improvements for board readability | css | append at end; no class renames |

Claim yours with the notes command above, work in your own workspace branch, and
leave a RESULTS note. Happy swarming.

### 7.1 Task lifecycle on the runtime board (git notes `tasks` ref)

The seeded task list above is the curated backlog; agents claim tasks from it by
adding **their own** note per Rule Zero. The same `tasks` note ref doubles as a
lightweight runtime board for tracking status and ad-hoc proposals, using message-prefix
disciplines so entries never overwrite each other:

| prefix | who adds | meaning |
|---|---|---|
| (seeded list) | humans via AGENTS.md §7 edit + commit | curated backlog; canonical catalog |
| `claimed by <id>: <slug> (branch agent/<id>-<slug>)` | worker agents | Rule Zero claim, added on the anchor commit where notes live today |
| `done: <slug> (merged)` / `done: <slug> (rejected: reason)` | merge agent or human | status marker after integration; appended note per slug |
| `TASK+ <slug>: <desc> \| lane: <lane> \| difficulty: low\|med\|high` | humans, integrator agents | new runtime task added without editing AGENTS.md (use `-m`, never `-f`) |
| `PROPOSED <slug>: <desc> \| lane: <lane>` | any agent or human | suggestion awaiting curation; does NOT count as a claim |

* **Anchor discipline:** all notes above attach to a single anchor commit — find
  it once with `ANCHOR=$(git notes --ref tasks list | head -1 | awk '{print $2}')`
  (there must be exactly one line in the list; if more, consolidate by removing
  stale anchors per below). When main advances, notes stay at their anchor and
  remain readable via this lookup.
* Multiple entries coexist as separate note objects on that same commit. Never
  use `-f` to overwrite someone else's note (that silently destroys claims);
  only replace your OWN previous entry, or better: add a new one alongside it.
* Claims must reference an existing slug from §7 or a `TASK+`/claimed-by entry
  already visible on the board. Agents inventing work add a `PROPOSED` note;
  they do not self-claim proposed tasks unless no human/integrator has curated
  them and there is spare capacity.
* Done markers are informational; merge state lives in git history + results
  notes, so approximate status suffices.
* **Consolidation rule (integrator/human only):** if `git notes --ref tasks list`
  shows more than one anchor commit, migrate all entries to the newest by adding
  a consolidated note there (`-m`), then delete stale anchors:
  `git notes --ref tasks remove <stale-commit>`. Keep b3's claim content intact.
