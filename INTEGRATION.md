# clm-mem Integration — memory for a swarm that works in git

This repo can optionally be wired to
[clm-mem](https://github.com/GentianLine/clm-mem),
a zero-training long/short-term memory harness for open chat models (inspired by
Context Language Models, arXiv:2609.37725). The bridge lives in
`tokamak/memory.py` and never activates unless you opt it in — the dependency is
lazy-imported, so a checkout where clm-mem simply isn't installed still runs
everything.

## The served-mode contract (explicit session API)

Served mode speaks the **explicit session API** that now lives on
`GentianLine/clm-mem` [`main`](https://github.com/GentianLine/clm-mem/tree/main)
(spec: that repo's `README.md` "Client contract" + `clm_mem/remote.py`; full
migration plan: `docs/library-migration.md`). The contract **has landed** — the
original spike branch (`spike/session-api`) remains open as a historical
branch, but its content is merged onto `main`, and that is what
`requirements.txt` pins via the PEP 508 direct-reference
(`clm-mem @ git+https://github.com/GentianLine/clm-mem.git@main`), so tracking
`main` is sufficient; no branch-pinning or editable install is needed anymore.
The bridge still skips its served-mode tests when the installed SDK predates
`open()`/`turn()` (e.g. a stale shared site-packages dir).

What changed for consumers:

* **No header-driven memory.** The legacy `X-Cllm-Mem-Session` /
  `X-Cllm-Mem-Chat` headers are gone; sending them earns a **410 Gone**
  with a migration hint. `/v1/chat/completions` is pure passthrough — it
  never remembers.
* **Sessions are branches you manage**, not request decorations:

  ```http
  POST /v1/clm/sessions                    {"session": "ws:project-3", "options": {...}} -> 201 created / 200 reopened
  POST /v1/clm/sessions/ws%3Aproject-3/turns {"content": "...", "chat": "chat-001"}     -> reply + clm_mem truth
  DELETE /v1/clm/sessions/<id>             drop at end of lifecycle (good citizen)
  ```

* Creation-time options (`max_context_tokens`, `shortterm_keep_pct`,
  `classify_mode`, ...) ride on **open** only; a reopen never reshapes a
  live branch. Per-turn knobs (`temperature`, `max_tokens`) stay per turn.
* Turns never create sessions (**409** unknown branch); bare ids like
  `"alice"` name nothing (**400**, branch ids are `ws:<name>`).
* The bridge implements this in `tokamak/memory.py`: eager open at
  `open_session()`/`ticket_harness()` (fail fast at connect time, plan rule
  2), ingest via `turn()` (`remote_ingest()`), deliberate cleanup via
  `drop_session()` / `drop_ticket_branch()` (DELETE). Ticket-session ends
  drop the branch server-side; local-mode branches have no server-side
  lifecycle.
* Isolation is structural: a turn in one branch never sees another's
  blocks; cross-session continuity (`seed_from_archive` handoff) is an
  explicit application-level copy, replayed as ingest turns.

## Why memory here

Tokamak's `EventLog` already records *what happened*; clm-mem adds *what an agent
should keep around while working*: a token-budgeted context window per project
that survives eviction by offloading to an archive (never silent destruction).
Agents answering "what changed on this board?" recall from memory instead of
re-reading every event row; the harness decides durability for them.

## Enable it

```bash
# clm-mem is not on PyPI — pull it from GitHub via pip's PEP 508 direct-reference:
pip install "git+https://github.com/GentianLine/clm-mem.git@main"
# (or simply `pip install -r requirements.txt`, which pins exactly that)
```

Then in `config/settings.py` (`TOKAMAK_MEMORY`, defaults shown):

```python
TOKAMAK_MEMORY = {
    'enabled': True,
    'mode': 'local',   # 'local' embeds the harness; 'served' attaches to a server
    'workdir': str(BASE_DIR / 'scratch' / 'memory'),
    'max_context_tokens': 4096,
    'shortterm_keep_pct': 0.5,
    'backend_base_url': '',   # e.g. http://127.0.0.1:8080/v1 (served mode)
    'backend_model': '',      # set with a real OpenAI-compatible backend
    'api_key': 'EMPTY',
    'mirror': 'off',          # 'live' mirrors signals as they fire
    'tokenize': False,        # True = exact HF-tokenizer counts (clm-mem[tokens])
}
```

* `mirror='live'` — every signal also writes into the project's memory session
  (best-effort; receivers never fail board writes).
* `mirror='off'` — pull history explicitly with the sync command.

Durability policy for mirrored events (`tokamak/memory.py:EVENT_TAGS`):
project updates and ticket creation are **longterm**; state changes are
**shortterm**; estimates / iteration moves are **transient**.

## Commands

```bash
python manage.py memory_sync              # mirror new EventLog rows (watermark)
python manage.py memory_sync --replay-all # rebuild every project session
python manage.py memory_recall "deploy port" --project 3
python manage.py memory_recall --project 3 --reinject 7   # archive -> live
```

## REST surface

```
GET    /api/memory/status/?project=<id>      bridge config + live snapshot
POST   /api/memory/recall/   {"project":1,"query":"...","reinject_block":7|null}
POST   /api/memory/note/     {"project":1,"text":"fact to remember"}  -> longterm block
```

The status route reports `{enabled, mode, clm_mem_installed, mirror}` so any
caller (or CI probe) can tell whether memory is wired in; write routes answer
400 with `enabled:false` when disabled rather than silently no-opping.

## Git-repo conventions (swarm protocol for memory)

1. **Memory files are per-worktree runtime state, like `db.sqlite3`.** Local
   sessions live under `<workdir>/projects/<pk>` inside *your* workspace
   (`scratch/memory/...`, already gitignored). Never commit or merge them;
   each agent's worktree rebuilds its own from EventLog via `memory_sync`.
2. **One session per project, shared across worktrees via a server.** In
   served mode the session id is `ws:project-<pk>` on a single
   `clm-mem serve` daemon; run it once for the swarm and every workspace that
   sets `TOKAMAK_MEMORY['mode']='served'` + `backend_base_url` gets the same
   live window (and the web studio at the proxy root, where you can watch
   eviction phases fire as agents work).
3. **Agents may drive git through clm-mem's own agent tools.** `serve --tools`
   sandboxes (`run_command`, `write_file`, ...) are pointed at
   `<workdir>/workspaces/<name>/sandbox`; pointing the sandbox root at a repo
   checkout lets a memory session run `git log`, `git notes`, worktree setup —
   the same commands this handbook uses — while its scratchpad output obeys
   the context budget instead of exploding it.
4. **Coordination stays in git notes; memory is not a claim registry.**
   Claims/results/merges continue to use the `tasks` / `results` / `merges`
   note refs (AGENTS.md §5). Memory may *remember* claims (they are just
   event-shaped blocks), but authority lives in git.

## Session lifecycle: tickets that open start a chat session

When `ticket_sessions` is on, opening a ticket for work starts a clm-mem
chat/memory session attached to it; ending the session distills what was
learned back onto the ticket. The README renders this flow as a diagram
("Ticket session lifecycle at a glance"); the design decisions behind it:


| Question | Decision | Why |
|---|---|---|
| When does a session start? | Ticket enters `started` (configurable `session_start_states`), or explicit open via REST/CLI | "tickets open" = work becomes active; creation alone doesn't |
| How many per ticket? | One live at a time — a new attempt replaces any stale OPEN one (reason `reopen`) | Attempts are sequential; no overlapping windows |
| How is it monitored? | `MemorySession.monitor_url`: served mode deep-links the web-studio workspace `ticket-<pk>` (chat, badges, eviction chips); local mode points at `/api/sessions/<pk>/snapshot/` + mirrored `context.md` / `context.jsonl` files | Same blocks either way; studio is nicer, snapshot route works offline |
| How does it end? | Deterministically and idempotently: `manual`, `ticket:<state>` on finished/accepted/rejected (configurable), `withdrawn` on started→unstarted, `reopen`. Sessions never outlive their ticket. | Every path goes through the service layer; no silent zombie windows, no background timers (idle expiry is a served-daemon concern) |
| What flows back into the ticket? | **Summary** (`MemorySession.summary` + EventLog `session.ended`) distilled from longterm blocks — deterministic by default, optional model pass via `session_distill_model`; **archive offload** of live long/short memory so the next attempt is *seeded* from previous facts (`payload.seeded_blocks`) | The ticket gains a durable "what we learned" note and cross-attempt memory without any LLM silently editing workflow state |
| What does NOT flow back? | No silent mutation of Ticket fields. Estimates/state changes from a session must go through the existing service-layer actions (signals stay the contract). Action items found in a session do not auto-create tickets — proposal only, pending an approval policy. | Keeps one authoritative write path; avoids agents rewriting each other's boards |

```bash
python manage.py ticket_session --open-ticket 3      # manual open (+ monitor link printed)
python manage.py ticket_session --list --ticket 3
python manage.py ticket_session --close-session 1 --reason manual
python manage.py ticket_session --summary 1
```

```
GET    /api/sessions/?ticket=<id>&status=open|ended   list + latest summary
POST   /api/sessions/open/          {"ticket": <id>}
POST   /api/sessions/<pk>/close/    {"reason": "manual", "summary": "optional override"}
GET    /api/sessions/<pk>/snapshot/ live-window monitor view
```

## Example: a memory-backed processor

```python
# any AppConfig.ready() — same pattern as automation.py
from tokamak import memory
from tokamak.signals import ticket_state_changed

@receiver(ticket_state_changed)
def remember_context(sender, ticket, old_state, new_state, **kwargs):
    """When work starts, recall what we know about similar tickets."""
    if new_state == 'started':
        prior = memory.recall(ticket.project_id, ticket.name.lower())
        # ... feed `prior` into your triage prompt ...
```

## Roadmap ideas (not yet built)

* Expose tokamak REST actions as clm-mem MCP tools (`clm-mem serve` is already
  an MCP server; add tokamak's transition/estimate endpoints to its tool list).
* **Webhook fan-out** from EventLog feeding the bridge in one place (pairs with
  the `automation-webhooks` task): not built on `main`, but a worker branch
  (`agent/adm1-automation-webhooks`) already carries an implementation
  (`tokamak/webhooks.py` + tests) awaiting integration — see README TODO.
* A nightly job: `memory_sync --replay-all` then archive-search stats into
  the admin event-analytics view.

## Testing

`tests/test_memory.py` — 15 tests, fully offline via clm-mem's bundled
`StubChat` backend (no GPU, no server): gating, local mirroring + context
file mirror, eviction-to-archive + reinject round-trip, live signal mirrors,
both management commands, and every REST route.
