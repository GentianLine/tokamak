# Tokamak — story points at plasma pressure (Django + Django Unicorn)

[![Tests](https://github.com/OWNER/tokamak/actions/workflows/test.yml/badge.svg)](https://github.com/OWNER/tokamak/actions/workflows/test.yml) [![Lint](https://github.com/OWNER/tokamak/actions/workflows/lint.yml/badge.svg)](https://github.com/OWNER/tokamak/actions/workflows/lint.yml)

A simple, fast story-point board built for the long game: **every action is
driven through one service layer**, exposed both as a reactive UI
([django-unicorn](https://www.django-unicorn.com)) and a REST API
(Django REST Framework), with **Django signals firing on every project and
ticket state change** so automated processors can plug in later.

## Quick start

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo      # optional: demo project w/ 5 sprints of history
python manage.py runserver
```

This checkout also carries a ready `.venv` (gitignored, base repo only) —
run everything with `.venv/bin/python` if you don't want to install anything:

```bash
.venv/bin/python manage.py migrate && .venv/bin/python manage.py seed_demo
.venv/bin/python manage.py runserver
```

The optional memory bridge needs `clm-mem`, which is not on PyPI: install it
from GitHub with `pip install "git+https://github.com/glassresistor/clm-mem.git"`.

* Home `/` — create/open projects (Unicorn)
* Board `/projects/<id>/` — linear-stream ticket flow, one-click story pointing
* Done archive `/projects/<id>/done/` — accepted/rejected tickets, grouped per iteration
* API `/api/` — browsable DRF root
* Admin `/admin/` — plus a read-only **Event Log** view of everything signals saw

## Core concepts

| Model       | Role                                                            |
|-------------|-----------------------------------------------------------------|
| `Project`   | Container for tickets and iterations                            |
| `Iteration` | Sprint with start/end dates; the end date is the "cutoff"       |
| `Ticket`    | feature / chore / bug moving through board states               |
| `Epic`      | Named grouping of tickets for scope organization              |
| `Milestone` | Date-targeted delivery goal; done when all its tickets accepted |
| `Blocker`   | Edge of the blocks/blocked_by many-to-many between tickets      |
| `EventLog`  | Append-only audit trail written by signal receivers             |

Board workflow (enforced in `tokamak.services.ALLOWED_TRANSITIONS`):

```
unstarted → started → finished → accepted
     ↑          ↑         │   ↑        │
     └──────────┴─────────┘   └────────┴→ rejected → started
```

Story points are Fibonacci-only (1, 2, 3, 5, 8, 13, 21) and a feature/bug
must be estimated before it can be accepted.

### Blockers, epics and milestones

* **Blockers** are a real many-to-many relationship between tickets: a
  `Blocker` row means *source blocks target* (the target is "blocked_by"
  the source), optionally carrying a short reason like "waiting on design".
  Blocked stories show a red flag on the board. The service layer
  (`services.add_blocker` / `remove_blocker`) enforces: no self-blocks,
  project-local edges, and an acyclic graph (a cycle would deadlock
  acceptance). A blocked ticket cannot be accepted until its blockers
  clear; **accepting** a blocker clears its edge automatically (reason
  `accepted`), while a *rejected* blocker simply stops counting. Signals:
  `blocker_added` / `blocker_removed`, mirrored into `EventLog`.
* **Epics** are lightweight per-project umbrellas (`Ticket.epic`);
  membership changes audit as `ticket.epic_changed`. The board header and
  ticket rows show epic badges with accepted/total rollups.
* **Milestones** are date-targeted delivery markers (`Ticket.milestone`,
  PT release-style); completion is derived — a milestone is complete when
  every attached ticket is accepted. Membership audits as
  `ticket.milestone_changed`.

## Sprint cutoff estimation

`tokamak.services.estimate_cutoff(project)`:

* **velocity** = mean points *accepted* per completed iteration
* **iteration length** = median historical sprint span (fallback: 14d)
* `iterations_needed = ceil(backlog_points / velocity)`
* `estimated_cutoff` = last known iteration end + needed × length
* when velocity is noisy (stdev/mean > 0.35) a **pessimistic cutoff** one
  sprint later is added, plus a 0–1 confidence score

Shown live in the board's "Backlog forecast" panel and at
`GET /api/projects/<id>/cutoff/`.

## Board UX: the linear stream and its sprint line

The board is **one ordered list**, not a kanban of columns:

* **Started** work sits at the very top (purple glow + "▶ started" pill);
  delivered-but-unaccepted work follows as **in review** (amber "⚗ in
  review"); then the unstarted backlog, flat and quiet.
* **Accepted/rejected tickets leave the board entirely.** They appear on
  the done archive page (`/projects/<id>/done/`), grouped by iteration
  with per-group accepted-point totals; rejects are struck through.
* A red **sprint line** cuts the list between the last ticket that fits in
  a sprint and what's coming next. `tokamak.services.sprint_line()` puts
  it at the average points per sprint (mean accepted points across
  completed iterations).
* With **fewer than two sprints of data** there is nothing to learn from:
  the line becomes a **dotted placeholder pinned at 7 points**
  (`SPRINT_LINE_FALLBACK_POINTS`).

## Signals — the automation contract

All mutations (UI click, REST call, admin, management command) go through
`tokamak/services.py`, which sends these custom signals (`tokamak/signals.py`):

| Signal                    | Payload                              |
|---------------------------|--------------------------------------|
| `ticket_created`          | `ticket`                             |
| `ticket_state_changed`    | `ticket, old_state, new_state, user` |
| `ticket_estimated`        | `ticket, old_points, new_points`     |
| `ticket_iteration_changed`| `ticket, old_iteration, new_iteration` |
| `project_updated`         | `project`                            |
| `blocker_added`           | `blocker`                            |
| `blocker_removed`         | `blocker, reason` (`manual` / `accepted`) |

Built-in receivers (`tokamak/automation.py`) show the pattern:

1. **auto_assign_active_iteration** — starting an unscheduled ticket pulls it
   into the currently active sprint.
2. **auto_estimate_chore** — chores created without estimate default to 1 pt.
3. **flag_unestimated_acceptance** — force-accepted unpointed tickets emit a
   repair event instead of silently skewing velocity.

Every signal is also mirrored into `EventLog` (`/api/events/`) so automations
can poll or replay history; swap that receiver for a webhook/queue later.

### Adding your own processor

```python
# tokamak/automation.py (or any AppConfig.ready())
from tokamak.signals import ticket_created

@receiver(ticket_created)
def triage_bot(sender, ticket, **kwargs):
    if "login" in ticket.name.lower():
        services.estimate_ticket(ticket, 3)   # signals fire again, safely
```

## REST API (each action has an endpoint)

```
CRUD    /api/projects/                     POST/GET/PATCH/DELETE
        /api/projects/<id>/tickets/        create tickets in a project
        /api/projects/<id>/iterations/     create sprints
        /api/tickets/<id>/                 read/update (state is immutable here)
ACTIONS POST /api/tickets/<id>/transition/ {"state": "started"}
        GET  /api/tickets/<id>/transition/ → allowed next states (discovery)
        POST /api/tickets/<id>/estimate/   {"points": 5}
        POST /api/tickets/<id>/assign/     {"iteration": 3 | null}
READS   GET  /api/projects/<id>/board/     whole board + forecast in one call
        GET  /api/projects/<id>/velocity/  per-iteration accepted points
        GET  /api/projects/<id>/cutoff/    backlog forecast (dates)
        GET  /api/projects/<id>/blockers/  live edges + blocked tickets
        GET  /api/tickets/<id>/blockers/   {blocks, blocked_by, is_blocked}
BLOCK   POST   /api/tickets/<id>/block/    {"by": <id>, "reason": optional}
        DELETE /api/tickets/<id>/block/    {"by": <id>}
        CRUD   /api/blockers/?project=&source=&target=  (validated via services)
GROUPS  CRUD   /api/epics/?project=&ticket=      epic grouping management
        CRUD   /api/milestones/?project=         milestone management
        GET  /api/events/?project=1&name=ticket.state_changed
```

Illegal transitions and non-Fibonacci estimates return `400` with a helpful
detail message. All writes fire the same signals as the UI.

## Architecture

```
 browser (Unicorn JS: fetch + morphdom, drag&drop)
    │  POST /unicorn/message/<component>
    ▼
 Unicorn components ──┐                DRF viewsets/views
 (board, velocity,     │                    │
  project_list)        ▼                    ▼
                  tokamak/services.py  ← single source of truth
                       │  validates workflow + Fibonacci points
                       ▼
                  Django ORM  +  custom signals ─→ automation receivers
                                                      └→ EventLog (audit)
```

## Development: lint, tests & CI

Install dev tooling alongside runtime deps (`requirements-dev.txt` pins the
formatter so local and CI agree):

```bash
python -m pip install -r requirements.txt -r requirements-dev.txt
black .            # format (house style keeps single quotes — see pyproject.toml)
pytest             # run the suite
```

* **Tests** — `pytest` (via pytest-django, config in `pytest.ini`): currently
  **125 tests** covering signal payloads, workflow validation (including
  rejected→started recovery and cross-project assign guards), velocity/cutoff
  math, every REST action, markdown rendering plus the preview/suggest
  endpoints and creation pages, drag-reorder, and driving the Unicorn board
  component directly (add/estimate/move_to incl. partial-walk errors). The suite lives under `tests/` as function-based
  modules; add new tests there rather than reintroducing class-based unittests.
* **Lint** — `black --check .`, configured in `pyproject.toml`
  (`skip-string-normalization` preserves the established single-quote style).

GitHub Actions runs both gates on every PR and push to `main`:

| Workflow | File | What it does |
|---|---|---|
| Tests | `.github/workflows/test.yml` | pytest matrix (py3.12, py3.13) + `manage.py check` + a makemigrations consistency guard |
| Lint | `.github/workflows/lint.yml` | `black --check .` using the pinned tooling |

### Setting this up as a GitHub project

The repo is ready to push: it has CI workflows, an MIT `LICENSE`, `.gitattributes`
(line-endings + language stats), `CONTRIBUTING.md`, and clean ignore rules. To
initialize the hosted project — create the repository on GitHub, add it as a
remote (`git remote add origin <url>`), then `git push -u origin main`. Turn on
branch protection for `main` requiring the **Tests** and **Lint** status checks
before merge. (Replace the `OWNER` placeholder in the badge URLs above with your
GitHub account/org once the repo exists.)

## Optional: clm-mem memory bridge

Tokamak can feed an LLM *memory* instead of just an audit log: every signal and
EventLog row optionally mirrors into per-project sessions of
[clm-mem](https://github.com/glassresistor/clm-mem), a zero-training
long/short-term context-memory harness (token-budgeted window, three-phase
eviction, archive + recall). And when `ticket_sessions` is on, tickets opening
for work start monitored clm-mem chat sessions (`/api/sessions/*` with monitor
links); ending them distills a summary plus durable facts back onto the ticket,
with archive handoff so reopened attempts inherit what previous ones learned.
Off by default; fully offline-testable. See
[INTEGRATION.md](INTEGRATION.md) for setup (`TOKAMAK_MEMORY`), the
`memory_sync` / `memory_recall` / `ticket_session` commands, `/api/memory/*`
and `/api/sessions/*` routes, and the git-repo conventions for sharing one
memory server across agent worktrees.

### Ticket session lifecycle at a glance

```text
 ticket enters "started"   (or manual open via REST / CLI)
        │
        ▼
 open_ticket_session(ticket)
        ├─ stale OPEN attempt closed first      ── reason: reopen
        ├─ window seeded from ticket archive    ── memory handoff in
        ├─ EventLog row session.opened
        ▼
 MemorySession [open]   ◄── paired clm-mem session ws:ticket-<pk>
        │                 monitor_url deep-links the web studio
        │                 (served mode) or the snapshot route +
        │                 mirrored context.md files (local mode)
        │
        │    ...chat happens; durable decisions are
        │    tagged longterm and stay in the window...
        ▼
 close_ticket_session(reason)   reasons: manual | ticket:<state> | withdrawn
        ├─ archive_live(): live long/short blocks ► ticket archive.jsonl
        ├─ distill_summary() from longterm facts (model pass if configured)
        ├─ EventLog row session.ended (+ payload offloaded / archive)
        ▼
 MemorySession [ended]  ── summary flows back onto the ticket attempt;
        │                 Ticket workflow state itself never mutates
        │                 silently — that stays service-layer only
        ▼
 rejected → started: a NEW attempt opens, and the previous attempts'
 archived facts are seeded into the fresh window (payload.seeded_blocks)
```

## Markdown richtext editors

All prose in Tokamak is GitHub-flavored markdown, rendered server-side through
`tokamak/markdown.py` (markdown-it + `mdit_py_plugins.gfm`: tables, task
lists, strikethrough, autolinks, footnotes, fenced code, and GitHub alerts
`> [!NOTE]` …). Raw HTML is never parsed (`html=False`); dangerous link
schemes are blocked by markdown-it's link validation.

**References to Tokamak objects** (work in every description, previews included):

| Syntax | Effect |
|---|---|
| `#12` | link to ticket 12 — only when that ticket exists (existence rule) |
| `ticket:12` / `tkt:12` | same as above |
| `project:3` / `proj:3` | link to project 3 — existence rule |
| `[Label](ticket:12)` | explicit reference — always links, even if missing |

**Creation pages** (the editors live here):

* `/projects/new/` — create a project (name + markdown description).
* `/projects/<pk>/tickets/new/` — create a ticket. Story types: feature and
  bug take a title plus an optional Fibonacci estimate; **a chore is nothing
  but a description** — the form hides name/points and the service layer
  enforces prose-only chores (title derived from the description when blank).

**Editor widget** (`static/js/markdown.js`, attaches to `textarea[data-md-editor]`):
toolbar with shortcuts (Ctrl/Cmd+B/I/E/K for bold, italic, code, link), line
prefix toggles for headings/quotes/lists/task items, a debounced live preview
(POSTs to `/markdown/preview/` so the preview is exactly what will render later),
and reference autocomplete: typing `#12…`, `ticket:…`, or `project:…` queries
`/markdown/suggest/`; Enter/Tab accepts, Esc dismisses.

## Notes & next steps

* django-unicorn **0.67** renamed its import path to `django_unicorn` and its
  message protocol uses `meta="checksum:hash:epoch"`; the JS client handles
  this, but note it if you hand-roll API tests.
* Computed component values are methods, not properties (Unicorn hydrates
  every public attribute — read-only properties crash on round-trip).
* The board supports **drag-reorder** inside a display group (rows are
  draggable; persisted via the reorder service and mirrored into `EventLog`,
  also as `POST /api/reorder/`). Cross-group drags are ignored on purpose —
  workflow moves stay on the buttons/API.
* Ideas: burndown chart, webhook fan-out from `EventLog`, auth/permissions
  before deploying.