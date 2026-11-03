# Tokamak — story points at plasma pressure (Django + Django Unicorn)

[![Tests](https://github.com/GentianLine/tokamak/actions/workflows/test.yml/badge.svg)](https://github.com/GentianLine/tokamak/actions/workflows/test.yml) [![Lint](https://github.com/GentianLine/tokamak/actions/workflows/lint.yml/badge.svg)](https://github.com/GentianLine/tokamak/actions/workflows/lint.yml)

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

The optional memory bridge needs `clm-mem`, which is **pinned via a PEP 508
direct-reference in `requirements.txt`** (it is not on PyPI), so a plain
`pip install -r requirements.txt` brings it in and the memory tests run:

```bash
pip install "git+https://github.com/GentianLine/clm-mem.git@main"   # equivalent
```

* Home `/` — create/open projects (Unicorn)
* Board `/projects/<id>/` — linear-stream ticket flow, one-click story pointing,
  inline blocker controls, epic/milestone chips, drag-reorder
* Ticket detail `/tickets/<id>/` — description rendered as GFM, event timeline,
  memory sessions with monitor links, POST actions for blockers/epics/milestones
* Done archive `/projects/<id>/done/` — accepted/rejected tickets, grouped per iteration
* Creation pages `/projects/new/` and `/projects/<id>/tickets/new/` — markdown-first
  forms with live preview (see *Markdown richtext editors*)
* API `/api/` — browsable DRF root
* Admin `/admin/` — plus a read-only **Event Log** view of everything signals saw
  and an aggregated **Event Analytics** dashboard (see *Admin surfaces*)

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
| `MemorySession` | clm-mem chat/memory session attached to a ticket: attempts, monitor link, distilled summary |

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
  `accepted`), while a *rejected* blocker simply stops counting; deleting a
  ticket through the service layer clears every edge touching it (reason
  `deleted`). Signals: `blocker_added` / `blocker_removed`, mirrored into
  `EventLog`.
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

Shown live in the velocity panel's "Backlog forecast" list (pessimistic cutoff
and confidence included), as a compact chip strip under the board header
(`.forecast-strip`: remaining points, estimated cutoff, pessimistic variant,
confidence — same dict, rendered from `services.estimate_cutoff` by the board
component), and at `GET /api/projects/<id>/cutoff/`.

## Burndown: committed scope vs accepted delivery

`tokamak/burndown.py` builds per-iteration burndown series purely from read
models — it never mutates anything, and every per-iteration aggregate defers to
`services.velocity_series` / the model helpers so the service layer stays
authoritative. Two views of the same history:

* **scope vs delivered** (`burndown_series`) — committed points (every ticket in
  the sprint, estimated or not) against accepted points, plus an `ideal` straight
  pace scaled from the observed mean delivery rate; rows carry `unaccepted` (the
  carry-over debt) and a `ratio` used for bar shading. A trailing summary reports
  totals, `hit_rate`, `best`/`worst` and how often the team hit its ideal pace.
* **remaining** (`remaining_series`) — the classic burn line: scope minus accepted
  at each iteration end, walking against a linear ideal that drains to zero by the
  last sprint.

Only completed iterations participate (matching `velocity_series`), so the chart
draws once a sprint has closed; below that the panel shows an honest empty state.
The velocity component renders it as **inline SVG** — viewBox `0 0 520 190`, bars
plus polylines plus axis ticks, no JS chart library and no build step. Geometry
lives in the component (`burndown` / `remaining` / `burndown_svg`, computed
methods as ever); series data comes from `tokamak/burndown`.

## Board UX: the linear stream and its sprint line

The board is **one ordered list**, not a kanban of columns:

* **Started** work sits at the very top (purple glow + "▶ started" pill);
  delivered-but-unaccepted work follows as **in review** (amber "⚗ in
  review"); then the unstarted backlog, flat and quiet.
* Ticket **names link to the ticket detail page** (card body click navigates too,
  drag gestures excluded), and cards carry a plain-text markdown excerpt of the
  description under the name (`markdown.excerpt`).
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
| `ticket_updated`          | `ticket, field, old_value, new_value, user` |
| `ticket_deleted`          | `ticket, user`                       |
| `ticket_state_changed`    | `ticket, old_state, new_state, user` |
| `ticket_estimated`        | `ticket, old_points, new_points`     |
| `ticket_iteration_changed`| `ticket, old_iteration, new_iteration` |
| `project_updated`         | `project`                            |
| `project_deleted`         | `project, user, blocker_edges`       |
| `blocker_added`           | `blocker`                            |
| `blocker_removed`         | `blocker, reason` (`manual` / `accepted` / `cycle` / `deleted`) |

Built-in receivers (`tokamak/automation.py`) show the pattern:

1. **auto_assign_active_iteration** — starting an unscheduled ticket pulls it
   into the currently active sprint.
2. **auto_estimate_chore** — chores created without estimate default to 1 pt.
3. **flag_unestimated_acceptance** — force-accepted unpointed tickets emit a
   repair event instead of silently skewing velocity.

Two more receivers exist but stay dormant unless the memory bridge opts in, so
the baseline signal graph is byte-identical without it: **live mirrors** (one per
custom signal, forwarding each event into clm-mem only when
`TOKAMAK_MEMORY['mirror']='live'`) and **auto_ticket_sessions** (opens/closes
clm-mem sessions as tickets enter/leave the active lanes; gated by
`TOKAMAK_MEMORY['ticket_sessions']`). Both are best-effort: they never raise, so
a broken or absent optional dependency cannot fail a board write.

Every signal is also mirrored into `EventLog` (`/api/events/`) so automations
can poll or replay history, and every `EventLog` row additionally fans out to
configured HTTP targets through `tokamak/webhooks.py` — config-driven, **off by
default** (`TOKAMAK['WEBHOOKS']`), idempotent delivery with HMAC signing and
retries; see *Webhook fan-out* below.

### Adding your own processor

```python
# tokamak/automation.py (or any AppConfig.ready())
from tokamak.signals import ticket_created

@receiver(ticket_created)
def triage_bot(sender, ticket, **kwargs):
    if "login" in ticket.name.lower():
        services.estimate_ticket(ticket, 3)   # signals fire again, safely
```

### Webhook fan-out

`tokamak/webhooks.py` (integrated from the dormant worker branch
`agent/adm1-automation-webhooks`) adds opt-in outbound delivery of every
`EventLog` row: `AppConfig.ready()` calls `webhooks.register_fanout()`, which
connects receivers **only** when `TOKAMAK['WEBHOOKS']['enabled']` is true and
targets are configured — the baseline signal graph stays byte-identical
otherwise (asserted in tests). Delivery is stdlib `urllib` POST of JSON with
optional HMAC-SHA256 signing (`X-Tokamak-Signature`), configurable timeout,
`max_retries` and backoff; receivers never raise, so a failing endpoint cannot
break a board write. Each delivery carries a stable `delivery_id` (event pk +
target host) and an in-process ledger keyed `(event_pk, url)` makes re-delivery
a skip — idempotent for consumers that dedupe on it. Targets may restrict
events with fnmatch patterns (`'project.*'`, `'ticket.state_changed'`). The
fan-out is read-only toward the audit trail: it mirrors `EventLog` rows but
**never writes to it**.

## Admin surfaces

Beyond the plain `Project`/`Iteration`/`Ticket` registrations and the read-only
`EventLogAdmin`, there are `Epic`/`Milestone`/`Blocker` admins in
`blockers_admin.py` (ticket-count, milestone-progress and resolved flags) and:

* **Event Analytics dashboard** at `/admin/tokamak/eventanalytics/` — a read-only
  proxy registration (`EventAnalytics`, shared table, state-only migration
  `0002`) whose changelist *is* the report: per-event-name counts (optional
  `?project=` and `?days=` window filters), a per-project rollup with top-5 event
  names, and a recent-activity feed (`?limit=`, clamped 1..100). Every figure
  comes from grouped aggregate queries — `project_counts` is one query,
  `activity_feed` two, `project_rollup` two regardless of row counts — and the
  registration denies add/change/delete entirely, so the audit trail stays
  append-only. Raw rows remain on the untouched `EventLogAdmin` at
  `/admin/tokamak/eventlog/`. Helpers live in `tokamak/event_analytics.py`, the
  admin in `tokamak/admin_analytics.py`, template under
  `templates/admin/tokamak/`.

## REST API (each action has an endpoint)

```
CRUD    /api/projects/                     POST/GET/PATCH/DELETE
        /api/projects/<id>/tickets/        create tickets in a project
        /api/projects/<id>/iterations/     create sprints
        /api/tickets/<id>/                 read/update/delete (DELETE = services.delete_ticket)
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
ORDER   POST   /api/reorder/                     {"ticket": id, "position": n}
MEMORY  GET    /api/memory/status/?project=<id>  bridge config + live snapshot
        POST   /api/memory/recall/               {"project":1,"query":"...","reinject_block":null}
        POST   /api/memory/note/                 {"project":1,"text":"fact to remember"}
SESS.   GET    /api/sessions/?ticket=&status=open|ended
        POST   /api/sessions/open/               {"ticket": <id>}
        POST   /api/sessions/<pk>/close/         {"reason": "manual", "summary": optional}
        GET    /api/sessions/<pk>/snapshot/      live-window monitor view (works offline in local mode)
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
                                                       ├→ clm-mem mirror (opt-in)
                                                       └→ ticket MemorySessions (opt-in)

 read-only surfaces (never mutate; every write still calls services.py):
   pages.py          ticket detail / done archive / creation forms
   markdown.py       GFM render + tracker refs (read-only DB lookups)
   burndown.py       series math for the velocity panel
   event_analytics.py  aggregate queries behind the admin dashboard
   reorder.py        display-group repositioning (its own EventLog row)
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
  **235 collected / 235 passing — nothing skipped**: the served-mode clm-mem
  suite runs against the pinned `clm-mem@main` direct-reference (explicit
  session API), verified locally after rebuilding the base `.venv` per
  AGENTS.md §2; remember to clear inherited `PYTHONPATH`/`PIP_TARGET` when
  running tools outside a venv, or the stale shared site-packages shadow.
  Coverage:
  signal payloads and workflow validation (incl. rejected→started recovery and
  cross-project assign guards), velocity/cutoff math, burndown series plus the
  component's SVG geometry, the admin event-analytics dashboard (query bounds,
  zero-state, read-only), every REST action, markdown rendering plus the
  preview/suggest endpoints and creation pages, drag-reorder, webhook fan-out
delivery/idempotency, ticket detail POST
  flows for blockers/epics/milestones, memory mirroring/recall and the
  ticket-session lifecycle, and driving the Unicorn board component directly
  (add/estimate/move_to incl. partial-walk errors). Per-area test files:
  `test_blockers.py` (26), `test_markdown.py` (19), `test_burndown.py` (16),
  `test_admin_analytics.py` (16), `test_memory.py` (15), `test_ticket_sessions.py`
  (13), `test_reorder.py` (13), `test_webhooks.py` (13), `test_memory_served.py`
  (12),
  `test_services_edge.py` (11), `test_creation_pages.py` (11),
  `test_md_views.py` (8), `test_project_delete.py` (5),
  `test_md_edit_pages.py` (7), `test_delete_service.py` (7),
  `test_md_integrity.py` (5), plus smaller suites for services/api/pages/components/
  forecast/ticket-pages. The suite lives under `tests/` as function-based
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
before merge. This repo is already hosted at
[`GentianLine/tokamak`](https://github.com/GentianLine/tokamak), so the badge
URLs above point at it; update them again only if the project ever moves.

## Optional: clm-mem memory bridge

Tokamak can feed an LLM *memory* instead of just an audit log: every signal and
EventLog row optionally mirrors into per-project sessions of
[clm-mem](https://github.com/GentianLine/clm-mem), a zero-training
long/short-term context-memory harness (token-budgeted window, three-phase
eviction, archive + recall). Served mode speaks clm-mem's **explicit session
API** — memory sessions are branches you open deliberately (`POST
/v1/clm/sessions` then `POST .../turns`), never a header side effect of
`/v1/chat/completions`, which stays pure passthrough; the bridge opens branches
eagerly at connect time so creation-time budget knobs actually reach the server,
and drops them deliberately (`drop_ticket_branch`) when Tokamak ends a session.
And when `ticket_sessions` is on, tickets opening for work start monitored
clm-mem chat sessions (`/api/sessions/*` with monitor links); ending them
distills a summary plus durable facts back onto the ticket, with archive handoff
so reopened attempts inherit what previous ones learned. Off by default; fully
offline-testable via clm-mem's bundled `StubChat` backend. See
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
| `[Label](ticket:12)` | explicit reference — always links, even if missing; a dangling target is flagged (`class="tokamak-ref stale"` + explanatory title), never for bare refs |
| `[Board](/projects/3/)` | ordinary markdown link to an internal path, unchanged |

External links additionally get `rel="noopener nofollow external"` and open in a
new tab; internal links stay in the same tab. All rendering is read-only — the
renderer queries names for labels but never mutates.

**Where it renders**: ticket detail, done archive rows (plain-text `excerpt`
tooltip + one-line digest), board cards (`desc-excerpt`), project page
description. **Amendment** flows render through the same widget: the ticket
detail page carries an inline editor POSTing `update_description` to
`services.update_ticket_description` (audited as `ticket.updated` via the new
`ticket_updated` signal), and the board page carries one for the project
description POSTing `/projects/<pk>/description/` through `save_project`.

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

## Notes & gotchas

* django-unicorn **0.67** renamed its import path to `django_unicorn` and its
  message protocol uses `meta="checksum:hash:epoch"`; the JS client handles
  this, but note it if you hand-roll API tests.
* Computed component values are methods, not properties (Unicorn hydrates
  every public attribute — read-only properties crash on round-trip).
* The board supports **drag-reorder** inside a display group (rows are
  draggable; persisted via the reorder service and mirrored into `EventLog` as
  `ticket.reordered`, also exposed as `POST /api/reorder/`). Cross-group drags
  are ignored on purpose — workflow moves stay on the buttons/API.
* **Deletion goes through the service layer now.** `services.delete_ticket`
  clears every blocker edge touching the ticket (each audited as
  `blocker.removed` with reason `deleted`), fires `ticket_deleted` while the
  instance is still readable, then deletes the row. `EventLog.ticket` is
  `SET_NULL`, so audit history outlives deleted tickets (rows persist with a
  null link plus their payload). Whole-project deletion goes through
  `services.delete_project` the same way: the project's blocker edges clear
  first (audited), then `project_deleted` fires while the instance is still
  readable — the audit row snapshots how many tickets, iterations, epics,
  milestones and edges are about to cascade — and `EventLog.project` is
  SET_NULL too, so even the project's history outlives the project.
* Transitioning a ticket to the state it already holds is a **silent no-op** —
  `transition_ticket` returns as soon as `new_state == old_state`, before the
  `ALLOWED_TRANSITIONS` check; re-assigning a ticket to the iteration it already
  sits in likewise audits nothing. Both documented in `tests/test_services_edge.py`
  rather than changed, since other code may rely on it.
* `ALLOWED_HOSTS = ['*']` in `config/settings.py` per human directive (this is a
  sample project; a deployed app supplies its own settings).

## TODO — what to do next

Curated from the AGENTS.md §7 catalog plus the runtime board (`git notes --ref
tasks`); status markers below reflect what actually landed on `main`.

### Done (integrated)

| task | delivered |
|---|---|
| burndown-chart | inline-SVG burndown on the velocity component (`burndown.py`, 16 tests) |
| drag-reorder | reorder service + REST + board.js drag handles (13 tests) |
| admin-event-analytics | read-only event analytics dashboard (proxy + aggregate queries, 16 tests) |
| test-coverage-lift | `test_services_edge.py` (rejected→started, cross-project assign, 11 tests) |
| ux-glass-fusion | clm-mem design tokens, fusion favicon/splash, board→ticket links |
| md-render-core / creation-pages / md-editor-ui / md-surfaces | GFM renderer, creation pages, editor widget, rendered surfaces |
| rename-tokamak | whole-repo rename onto the Tokamak identity: no `pivot`/`pivotal`/`aigle` branding and no `tracker/` package remain (verified with `git grep`) |
| blockers-epics-milestones | Blocker edge table + Epic/Milestone models, services, REST, board + detail UI (26 tests) |
| clm-mem session-API migration | served mode speaks the explicit session API (`open()`/`turn()`/DELETE verbs); mirrored in INTEGRATION.md and `tokamak/memory.py` |
| automation-webhooks | webhook fan-out receivers mirroring `EventLog` rows (`tokamak/webhooks.py`, config-driven, off by default, idempotent + HMAC/retries; cherry-picked from `25a88fe` via `integration/webhooks`, 13 tests) |
| cutoff-surface-board | forecast chip strip in the board header (`templates/unicorn/board.html` + append-only css; agent/ora-cutoff-board, 3 tests) |
| delete-ticket-service | `services.delete_ticket` + `ticket_deleted` signal, edges cleared with audit, surviving history via `EventLog.ticket` SET_NULL (migration 0004); REST destroy + board component path (agent/ora-delete-service, 7 tests) |
| md-ref-integrity | stale markers on dangling explicit refs (`tokamak/markdown.py`, css flag; bare-ref existence rule kept by decision; agent/ora-md-integrity, 5 tests) |
| md-edit-pages | description amendment flows: `services.update_ticket_description` + `ticket_updated` signal, inline editors on ticket detail + board pages, POST route through `save_project` (agent/ora-md-edit, 7 tests) |
| verify-served-mode-tests | base `.venv` rebuilt with the pinned git install; full suite runs served mode green (`230 passed`, zero skips); shared-site-packages shadowing documented in README dev notes |
| project-delete-audit | `services.delete_project` + `project_deleted` signal (edge counts snapshotted into the surviving audit row), `EventLog.project` SET_NULL via migration 0005, REST destroy override (agent/ora-project-delete, 5 tests) |

### Open backlog

1. **auth-permissions** *(lane: whole app; high, before any deploy)* — no
   permission layer anywhere: every REST endpoint and page is open, admin is
   reachable for anyone who can reach it, `ALLOWED_HOSTS=['*']`, memory write
   routes are `csrf_exempt`. Add DRF permission classes + Django auth on the
   pages, or consciously document this as a single-user tool.
2. **css-polish** *(lane: css; deprioritised)* — append-only readability
   improvements at the end of `static/css/app.css`; no class renames.
   Deprioritised because the design language was rewritten twice recently.
3. **memory-mcp-tools** *(lane: integration; med)* — expose Tokamak REST actions
   as clm-mem MCP tools (`clm-mem serve` is already an MCP server at `/mcp` with
   a tool list); pairs with the webhook fan-out so one place feeds both.
4. **memory-nightly-job** *(lane: ops; low)* — `memory_sync --replay-all` plus
   archive-search stats rolled into the admin event-analytics view (see
   INTEGRATION.md "Roadmap ideas").
5. **stale-worktree-cleanup** *(ops/human)* — leftover runtime state still
    lingers from finished runs: `workspaces/` (project-*/ticket-1 memory dirs),
    `scratch/retired/db_pre_rename.sqlite3`, and the worker worktrees the merge
    playbook leaves until humans confirm (`git worktree list`). The outer sandbox
    directory itself carries a legacy name outside git's control
    (`…/Projects/Tracker-2cdb178b`); flag to whoever owns that.

### Housekeeping while you're in here (resolved)

* `AGENTS.md` §1 refreshed: seven custom signals, ~195 collected tests
  (183 passing + 12 skipped served-mode); §2 installs clm-mem from
  `GentianLine/clm-mem@main` through the direct-reference in requirements.txt
  instead of the old `glassresistor/clm-mem.git` URL (which only survives as a
  rename redirect).
* `INTEGRATION.md` refreshed: no bare `pip install clm-mem` advice (it isn't on
  PyPI — use the pinned git direct-reference); harness/spec links point at
  `GentianLine/clm-mem` and its `main`, where the explicit-session-API contract
  has landed (`spike/session-api` remains as a historical branch, so the old
  "pin the branch until it lands" advice is gone); "Roadmap ideas" now flags
  the webhook fan-out as existing on a worker branch awaiting integration.
* README badge URLs carry the real host `GentianLine/tokamak` — the repo is
  hosted and CI runs there, so the `OWNER` placeholder is gone.
