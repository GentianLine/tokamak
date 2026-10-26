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
  `accepted`), while a *rejected* blocker simply stops counting. Signals:
  `blocker_added` / `blocker_removed`, mirrored into `EventLog`.
* **Epics** are lightweight per-project umbrellas (`Ticket.epic`);
  membership changes audit as `ticket.epic_changed`. The board header and
  ticket rows show epic badges with accepted/total rollups.
* **Milestones** are date-targeted delivery markers (`Ticket.milestone`,
  PT release-style); completion is derived — a milestone is complete when
  every attached ticket is accepted. Membership audits as
  `ticket.milestone_changed`.

### Ticket threads

A thread is the conversation that happens around a ticket: what an estimate
meant, why a blocker matters, who picked up what. Comments are markdown (the
same renderer and tracker refs as descriptions) and, like every other write,
flow through the service layer (`services.add_comment` / `edit_comment` /
`delete_comment`), firing `comment_added` / `comment_updated` /
`comment_removed` into `EventLog` as `comment.added` / `.updated` / `.removed`.

* **Soft delete by design**: removing a comment hides its body but keeps the
  row, so the timeline never develops a hole — the detail page shows a
  `[removed]` tombstone and counts removals in the header. (Hard-deleting
  tickets is still unserviced; see TODO `delete-ticket-service`.)
* **Human vs machine**: `Comment.kind` separates people from automation. Opt-in
  receivers in `tokamak/thread_automation.py` let the board take notes on
  itself — a system comment when a blocker edge appears, and how it cleared
  (`resolved manually` / `delivered`) when it goes away. Gated by
  `TOKAMAK_THREADS['auto_comment_blockers']`, off by default so the baseline
  signal graph stays byte-identical; receivers are best-effort and never raise.
* **Discussion health**: `services.comment_stats(project)` reports live vs
  removed comments and the **quiet backlog** — live tickets nobody has talked
  about (a system-only thread still counts as silence). Read at
  `GET /api/projects/<id>/threads/`.

The ticket detail page hosts the human surface: post a comment, edit inline via
`?edit=<comment-id>`, or remove one; bodies use the markdown editor widget.
Authors are free-form handles until auth lands (TODO `auth-permissions`).

## Sprint cutoff estimation

`tokamak.services.estimate_cutoff(project)`:

* **velocity** = mean points *accepted* per completed iteration
* **iteration length** = median historical sprint span (fallback: 14d)
* `iterations_needed = ceil(backlog_points / velocity)`
* `estimated_cutoff` = last known iteration end + needed × length
* when velocity is noisy (stdev/mean > 0.35) a **pessimistic cutoff** one
  sprint later is added, plus a 0–1 confidence score

Shown live in the velocity panel's "Backlog forecast" list (pessimistic cutoff
and confidence included) and at `GET /api/projects/<id>/cutoff/`.

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
| `ticket_state_changed`    | `ticket, old_state, new_state, user` |
| `ticket_estimated`        | `ticket, old_points, new_points`     |
| `ticket_iteration_changed`| `ticket, old_iteration, new_iteration` |
| `project_updated`         | `project`                            |
| `blocker_added`           | `blocker`                            |
| `blocker_removed`         | `blocker, reason` (`manual` / `accepted` / `cycle`) |
| `comment_added`           | `comment`                            |
| `comment_updated`         | `comment, old_body, new_body`        |
| `comment_removed`         | `comment, reason`                    |

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

`tokamak/thread_automation.py` adds two more dormant receivers on the same
carve: **auto_comment_blocker** and **auto_comment_unblock**, which leave system
comments on a blocked ticket when an edge appears or clears. They are gated by
`TOKAMAK_THREADS['auto_comment_blockers']` (off by default) and, like the memory
receivers, write through the service layer rather than touching the ORM.

Every signal is also mirrored into `EventLog` (`/api/events/`) so automations
can poll or replay history; swapping those audit rows onto a webhook fan-out is
planned but **not merged yet** — see TODO `automation-webhooks`.

### Adding your own processor

```python
# tokamak/automation.py (or any AppConfig.ready())
from tokamak.signals import ticket_created

@receiver(ticket_created)
def triage_bot(sender, ticket, **kwargs):
    if "login" in ticket.name.lower():
        services.estimate_ticket(ticket, 3)   # signals fire again, safely
```

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
ORDER   POST   /api/reorder/                     {"ticket": id, "position": n}
THREAD  CRUD   /api/comments/?ticket=&author=&kind=  thread read/manage (DELETE is soft)
        GET    /api/tickets/<id>/thread/            live comments + removed count
        POST   /api/tickets/<id>/comment/           {"body": "...", "author": optional}
        GET    /api/projects/<id>/threads/          discussion health + quiet backlog
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
  **223 collected / 211 passing + 12 skipped** (the skips are the served-mode
  clm-mem suite, which needs the installed SDK's explicit session API). Coverage:
  signal payloads and workflow validation (incl. rejected→started recovery and
  cross-project assign guards), velocity/cutoff math, burndown series plus the
  component's SVG geometry, the admin event-analytics dashboard (query bounds,
  zero-state, read-only), every REST action, markdown rendering plus the
  preview/suggest endpoints and creation pages, drag-reorder, ticket detail POST
  flows for blockers/epics/milestones, ticket threads (soft delete, quiet
  backlog, opt-in blocker note-taking, REST + detail-page POSTs), memory
  mirroring/recall and the
  ticket-session lifecycle, and driving the Unicorn board component directly
  (add/estimate/move_to incl. partial-walk errors). Per-area test files:
  `test_blockers.py` (26), `test_comments.py` (28), `test_markdown.py` (19),
  `test_burndown.py` (16),
  `test_admin_analytics.py` (16), `test_memory.py` (15), `test_ticket_sessions.py`
  (13), `test_reorder.py` (13), `test_memory_served.py` (12),
  `test_services_edge.py` (11), `test_creation_pages.py` (11),
  `test_md_views.py` (8), plus smaller suites for services/api/pages/components/
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
| `[Label](ticket:12)` | explicit reference — always links, even if missing |
| `[Board](/projects/3/)` | ordinary markdown link to an internal path, unchanged |

External links additionally get `rel="noopener nofollow external"` and open in a
new tab; internal links stay in the same tab. All rendering is read-only — the
renderer queries names for labels but never mutates.

**Where it renders**: ticket detail, done archive rows (plain-text `excerpt`
tooltip + one-line digest), board cards (`desc-excerpt`), project page
description. Ticket **edit** surfaces are not built yet — see TODO `md-edit-pages`.

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
* **There is no delete service.** The board component's `delete_ticket` calls
  `Ticket.objects.filter(pk=...).delete()` directly, bypassing the service layer
  (no signals, no EventLog row). Flagged in TODO below.
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

### Open backlog

1. **verify-served-mode-tests** *(lane: env/CI; low)* — the 12 served-mode
    memory tests are currently **skipped** here ("clm-mem explicit session API
    not installed"): the checkout resolves `clm_mem` from a shared site-packages
    dir (`.unsloth-packages`) that predates `open()`/`turn()`, and the base
    `.venv` shebang points at a deleted path (`…/pivotboard/.venv/bin/python`).
    Rebuild the base venv per AGENTS.md §2 with the pinned git install so served
    mode actually runs in CI, or mark the skip as expected.
2. **automation-webhooks** *(lane: automation; branch exists, never merged)* —
   config-driven webhook fan-out receivers mirroring `EventLog` rows, off by
   default, idempotent delivery with HMAC signing + retries. The worker branch
   `agent/adm1-automation-webhooks` (commit `25a88fe`, 13 tests, `tokamak/webhooks.py`)
   is **not integrated on main** (`tokamak/webhooks.py` and `tests/test_webhooks.py`
   exist on that commit but nowhere on `main`) — merge or rebase it, then mark
   this done.
3. **cutoff-surface-board** *(lane: templates + board component; low)* — the
   pessimistic cutoff and confidence score already render in the velocity panel;
   decide whether the *board* template should also show remaining points /
   estimated cutoff inline, or drop the task as superseded.
4. **md-edit-pages** *(lane: pages + services; med)* — edit an existing project
   or ticket description through the editor widget on detail pages. Needs a new
   service fn for ticket description edits (none exists; `save_project` already
   covers projects). Must fire a signal (`ticket_updated`) and audit into
   `EventLog`.
5. **md-ref-integrity** *(lane: markdown; low)* — dangling tracker references are
   currently *silently unlinked* when the target is deleted (existence rule).
   Decide and indicate it: keep the read-only rule but surface a stale-ref marker
   or an admin report of refs pointing at deleted rows.
6. **delete-ticket-service** *(lane: services; med, new)* — introduce
   `services.delete_ticket(ticket)` so board deletes stop bypassing the service
   layer: fire a signal, write an EventLog row, and decide what should happen to
   blocker edges that point *at* the deleted ticket (today they cascade away via
   `on_delete=CASCADE` on both `Blocker.source` / `Blocker.target`, silently).
   Deleting a project additionally wipes tickets/iterations/epics/milestones by
   cascade with no audit trail at all.
7. **auth-permissions** *(lane: whole app; high, before any deploy)* — no
   permission layer anywhere: every REST endpoint and page is open, admin is
   reachable for anyone who can reach it, `ALLOWED_HOSTS=['*']`, memory write
   routes are `csrf_exempt`. Add DRF permission classes + Django auth on the
   pages, or consciously document this as a single-user tool.
8. **css-polish** *(lane: css; deprioritised)* — append-only readability
   improvements at the end of `static/css/app.css`; no class renames.
   Deprioritised because the design language was rewritten twice recently.
9. **memory-mcp-tools** *(lane: integration; med)* — expose Tokamak REST actions
   as clm-mem MCP tools (`clm-mem serve` is already an MCP server at `/mcp` with
   a tool list); pairs with the webhook fan-out so one place feeds both.
10. **memory-nightly-job** *(lane: ops; low)* — `memory_sync --replay-all` plus
   archive-search stats rolled into the admin event-analytics view (see
   INTEGRATION.md "Roadmap ideas").
11. **threads-next** *(lane: pages + services; med; spike exists on
    `spike/ticket-comments`, never merged)* — the discussion layer is already
    delivered there (Comment model with soft delete, service functions,
    three signals, REST surface, detail-page UI, 26 tests). Before merging,
    decide: real author identity once auth lands (`auth-permissions`), whether
    nested replies matter or a flat per-ticket thread is enough, board badges
    for comment counts, and whether `kind='system'` notes should be excluded
    from the quiet-backlog statistic entirely.
12. **stale-worktree-cleanup** *(ops/human)* — leftover runtime state still
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
