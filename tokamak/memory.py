
clm-mem (https://github.com/glassresistor/clm-mem) is a zero-training
long/short-term memory harness for open chat models: every message lives in a
token-budgeted window as a block tagged longterm / shortterm / transient, and
a deterministic three-phase eviction engine enforces the budget, offloading

This module lets tokamak act as a *memory producer*: ticket/project signals
(and replayable EventLog history) are fed into per-project memory sessions so
an LLM agent working on this repo can recall what happened on the board
without re-reading every event row. It is entirely optional - the package is
imported lazily and the bridge is OFF unless ``settings.TOKAMAK_MEMORY``


* ``local``   - embeds :class:`clm_mem.MemoryHarness` in-process with any
  OpenAI-compatible backend (``StubChat`` works fully offline; tests use it).
* ``served``  - attaches a :class:`clm_mem.RemoteHarness` to a running
  ``clm-mem serve`` proxy, so swarm agents sharing a server share one live

Served mode speaks the **explicit session API** (clm-mem branch
``spike/session-api``, see clm-mem/docs/library-migration.md): memory is no
longer a header side effect of ``/v1/chat/completions``. A memory session is
a *branch* that you open deliberately -
``POST /v1/clm/sessions {"session": "ws:<name>", "options": {...}}`` - and
converse with through ``POST /v1/clm/sessions/<id>/turns``. The bridge opens
branches eagerly at :func:`open_session` (fail fast at connect time, so
creation-time budget knobs actually reach the server), keeps them in the
process cache, and drops them deliberately via


* Session sandboxes live under ``<TOKAMAK_MEMORY['workdir']>/projects/<pk>``
  - inside YOUR worktree, so each agent workspace owns its memory files; they
  are gitignored, never merged. Agents coordinate via git notes instead;
  the served mode is what makes memory cross-workspace when a team shares

All functions raise :class:`MemoryUnavailable` when the bridge (or the
optional dependency) is not usable, so callers can degrade gracefully.


import json
import os

from django.conf import settings
from django.core.cache import caches


DEFAULTS: Dict = {
    'enabled': False,
    'mode': 'local',  # local | served
    'workdir': os.path.join('scratch', 'memory'),  # relative to PROJECT_ROOT
    'max_context_tokens': 4096,
    'shortterm_keep_pct': 0.5,
    'backend_base_url': '',  # e.g. http://127.0.0.1:8080/v1 (served mode)
    'backend_model': 'memory-host',
    'api_key': 'EMPTY',
    'mirror': 'off',  # off | live  (live = signal receivers mirror events)
    'tokenize': False,  # exact HF-tokenizer counting needs clm-mem[tokens]
    # --- ticket-scoped chat sessions (sub-switch inside the bridge) --------
    'ticket_sessions': False,  # tickets opening start a session; ending closes it
    'session_start_states': ['started'],
    'session_end_states': ['finished', 'accepted', 'rejected'],
    'session_distill_model': False,  # True = ask the backend to write the summary

#: Durability policy for mirroring EventLog rows into memory blocks.
EVENT_TAGS: Dict[str, str] = {
    'project.updated': 'longterm',
    'ticket.created': 'longterm',
    # Deletion facts outlive eviction - they are the reason a subject is gone.
    'ticket.deleted': 'longterm',
    'project.deleted': 'longterm',
    'ticket.updated': 'shortterm',
    'ticket.state_changed': 'shortterm',
    'ticket.estimated': 'transient',
    'ticket.iteration_changed': 'transient',

_SESSIONS: Dict[int, object] = {}
class MemoryUnavailable(RuntimeError):
class UnknownSession(MemoryUnavailable):

    Turns never create sessions on the new contract; callers must open()
    first. Silently re-creating on this would resurrect the old "sessions
class MirrorRecord:

    Deliberately duck-compatible with :model:`tokamak.models.EventLog` so the
    same formatting/mirroring code serves both paths.

    def __init__(self, name, project_id, payload, created_at=None, ticket=None):
        self.name = name
        self.project_id = project_id
        self.payload = payload
        self.created_at = created_at or timezone.now()
def get_config() -> Dict:
    """Merge ``settings.TOKAMAK_MEMORY`` over the defaults (cached per settings)."""
    user = getattr(settings, 'TOKAMAK_MEMORY', None) or {}
    key = json.dumps(user, sort_keys=True, default=str)
    cached = _CONFIG_CACHE.get('cfg')
    if cached is not None and _CONFIG_CACHE.get('key') == key:
        return cached
    cfg = dict(DEFAULTS)
    cfg.update(user)
    root = str(getattr(settings, 'BASE_DIR', ''))
    if not os.path.isabs(cfg['workdir']):
        cfg['workdir'] = os.path.abspath(os.path.join(root, cfg['workdir']))
    _CONFIG_CACHE.clear()
    _CONFIG_CACHE['key'] = key
    _CONFIG_CACHE['cfg'] = cfg
def is_enabled() -> bool:
def clm_mem_available() -> bool:
    try:
        import clm_mem  # noqa: F401
    except ImportError:
        return False
def _session_dir(cfg: Dict, project_id: int) -> str:
def _open_remote(harness, options: Optional[Dict] = None):

    The explicit contract never creates sessions implicitly: turns 409 on
    unknown branches, and creation-time knobs (budget/keep-pct/classify/
    decay) only reach the server through ``open(options=...)``. Reopening is
    idempotent (never reshapes a live branch), so this is safe to call from
    every cache-miss path."""
    try:
        resp = harness.open(options=options)
    except RuntimeError as exc:
        raise MemoryUnavailable(f'cannot open memory branch: {exc}') from exc
    # ``created`` is advisory on the current spike server (a reopened branch
    # may still answer created:true); treat any successful open as "opened"
    # and rely on the reopen-is-never-a-reshape rule, not the flag.
    resp['accepted'] = True
def _serve_remote(cfg: Dict, session: str):
    """Build *and open* a served RemoteHarness (options ride on open())."""
    from shatterling.clients.sdk import RemoteHarness

    harness = RemoteHarness(
        cfg['backend_base_url'],
        session=session,
        api_key=cfg.get('api_key', 'EMPTY'),
    )
    options = {
        'max_context_tokens': int(cfg['max_context_tokens']),
        'shortterm_keep_pct': float(cfg['shortterm_keep_pct']),
        'classify_mode': 'post',
    }
    _open_remote(harness, options)
    harness._opened = True  # SDK sugar (step) must not re-open per turn
def remote_ingest(harness, text: str):

    A turn on a branch that vanished server-side (daemon restart drops the
    live window) is a 409 through the SDK's RuntimeError: reopen once and
    retry - an *open* branch, never an implicit-creation surprise."""
    try:
        return harness.turn(text)
    except UnknownSession as exc:
        _open_remote(harness)
        raise
    except MemoryUnavailable:
        raise
    except RuntimeError as exc:
        code = str(exc).split(':', 1)[0]
        if code == '409':
            _open_remote(harness)
            raise UnknownSession(str(exc)) from exc
def open_session(project_id: int):

    local mode  -> clm_mem.MemoryHarness over StubChat unless a backend URL
                   is configured (then OpenAIChat), workdir = session dir.
    served mode -> clm_mem.RemoteHarness against a live ``clm-mem serve``.
    """
    if not is_enabled():
        raise MemoryUnavailable('Tokamak memory bridge is disabled')
    if not clm_mem_available():
        raise MemoryUnavailable(
            'clm-mem is not installed (pip install clm-mem); see INTEGRATION.md'
        )
    cfg = get_config()
    if project_id in _SESSIONS:

from shatterling.engine.config import HarnessConfig
    from shatterling.engine.harness import MemoryHarness
    from shatterling.clients.sdk import RemoteHarness
    from shatterling.engine.llm import StubChat

    if cfg['mode'] == 'local':
        hcfg = HarnessConfig(
            max_context_tokens=int(cfg['max_context_tokens']),
            shortterm_keep_pct=float(cfg['shortterm_keep_pct']),
            workdir=_session_dir(cfg, project_id),
            memory_preamble=False,  # event streams do not need the chat preamble
            classify_mode='post',
        )
        if cfg.get('tokenize'):
            hcfg.tokenizer_name = cfg.get('tokenizer_name', '')
        if cfg.get('backend_base_url') and cfg.get('backend_model'):
            from shatterling.engine.llm import OpenAIChat

            llm = OpenAIChat(
                cfg['backend_base_url'],
                cfg['backend_model'],
                api_key=cfg.get('api_key', 'EMPTY'),
            )
        else:
            llm = StubChat()
        harness = MemoryHarness(llm, hcfg)
    elif cfg['mode'] == 'served':
        if not cfg.get('backend_base_url'):
            raise MemoryUnavailable(
                "served mode needs TOKAMAK_MEMORY['backend_base_url']"
            )
        harness = _serve_remote(cfg, f'ws:project-{project_id}')
    else:

    _SESSIONS[project_id] = harness
def close_session(project_id: Optional[int] = None) -> None:
    """Drop cached sessions (one or all); used by tests and config reloads."""
    if project_id is None:
        _SESSIONS.clear()
    else:
def drop_session(project_id: int) -> bool:

    Branches are visible server-side (``GET /v1/clm/sessions``, ``/health``)
    and persist until dropped - be a good citizen instead of leaving the
    daemon holding memory nobody owns. The SDK ships no ``drop()``, so the
    bridge mirrors its DELETE verb directly; local harnesses have no
    server-side lifecycle, so this just releases the cached instance."""
    harness = _SESSIONS.pop(project_id, None)
    if harness is None:
        return False
    drop = getattr(harness, 'drop', None)
    if drop is not None:  # a future SDK that owns the verb
        try:
            drop()
        except Exception:
            return False
        return True
    if hasattr(harness, '_snap_path'):  # RemoteHarness
        try:
            harness._req('DELETE', harness._snap_path)
        except Exception:
            return False
        return True
def format_event(event) -> str:
    """One-line durable sentence for an EventLog row (memory block text)."""
    label = event.name.replace('.', ' ')
    bits = [f'{label} at {event.created_at:%Y-%m-%d %H:%M}']
    ticket = getattr(event, 'ticket', None)
    if ticket is not None and getattr(ticket, 'id', None):
        bits.append(f"ticket='{ticket.name}'")
    payload = event.payload or {}
    for k in ('old', 'new', 'user', 'type', 'name'):
        v = payload.get(k)
        if v is not None:
            bits.append(f'{k}={v}')
def remember_event(event, tag: Optional[str] = None):

    local mode admits it directly as a tagged block (no LLM call), then runs
    the eviction engine + context-as-a-file mirror so the budget holds even
    when nobody ever *chats* with the harness; served mode feeds it through
    the server pipeline via an explicit turn so the live model tags it.
    """
    session_key = getattr(event, 'project_id', None) or -1
    harness = open_session(session_key)
    text = format_event(event)
    if tag is None:
        tag = EVENT_TAGS.get(event.name, 'shortterm')
    if hasattr(harness, 'add'):  # local MemoryHarness
        # Advance the turn counter per mirrored event: phase-1 eviction is
        # turn-window based, and a chat-less mirror session would otherwise
        # keep every transient block forever (turn stuck at 0 exempts all).
        harness.turn += 1
        block = harness.add(role='user', content=text, tag=tag)
        harness.evict()
        harness.mirror_files()
        return block
    # RemoteHarness: turn() so the server classifies it on ingest; the reply
    # carries harness truth under ``clm_mem`` (event chips replace polling).
def mirror_event(event) -> None:
    """Best-effort mirror used by live receivers: never break board writes."""
    if get_config()['mirror'] != 'live':
        return
    try:
        remember_event(event)
    except MemoryUnavailable:
def recall(project_id: int, query: str = '', limit: int = 50) -> List[Dict]:

    Live hits are marked ``live=True`` so callers can distinguish what is
    still in context from what must be reinjected before it answers.
    """
    harness = open_session(project_id)
    q = (query or '').lower()
    hits: List[Dict] = []
    for b in harness.live_blocks():
        if not q or q in str(b.content).lower():
            hits.append(
                {
                    'id': b.id,
                    'role': b.role,
                    'content': b.content,
                    'tag': b.tag,
                    'turn': b.turn,
                    'live': True,
                }
            )
    if q:
        # archive search only with a real query: an empty one would dump
        # the whole archived history alongside the live window.
        hits.extend(harness.recall(query, limit=limit))
def reinject(project_id: int, block_id: int) -> bool:
    harness = open_session(project_id)
def session_status(project_id: int) -> Dict:
    """Snapshot of the live window: counts, tokens, events tail."""
    harness = open_session(project_id)
    if hasattr(harness, 'snapshot'):
        snap = harness.snapshot()
        try:
            snap['counts'] = harness.counts()
        except Exception:
            pass
        return snap
    # RemoteHarness snapshot already carries counts/budget from the server.
def _watermark_path(cfg: Dict) -> str:
def load_watermark() -> Optional[str]:
    try:
        with open(_watermark_path(get_config())) as fh:
            return json.load(fh).get('last_event')
    except (OSError, ValueError):
def save_watermark(last_event_id: int) -> None:
    cfg = get_config()
    os.makedirs(cfg['workdir'], exist_ok=True)
    with open(_watermark_path(cfg), 'w') as fh:
        json.dump(
            {'last_event': str(last_event_id), 'at': timezone.now().isoformat()}, fh
# --- ticket-scoped sessions --------------------------------------------------
# Design (see INTEGRATION.md "Session lifecycle"): a session is opened per
# ticket attempt, monitored through the bridge REST surface or (served mode)
# the clm-mem web studio workspace `ticket-<pk>`, and ended deterministically.
# On end, live long/short memory is OFFLOADED into the ticket-scoped archive
# so the next attempt can recall it ("memory handoff"), and a distilled

def sessions_enabled() -> bool:
    """Ticket-session automation is a sub-switch inside the bridge."""
    cfg = get_config()
def session_key(ticket_id: int) -> str:
def ticket_harness(ticket):
    """Harness for one ticket attempt (cached). Workdir persists per ticket,
    so archives carry context across attempts even in local mode."""
    if not is_enabled():
        raise MemoryUnavailable('Tokamak memory bridge is disabled')
    if not clm_mem_available():
        raise MemoryUnavailable('clm-mem is not installed (pip install clm-mem)')

    cfg = get_config()
    tid = ticket.id if hasattr(ticket, 'id') else int(ticket)
    if tid in _TICKET_SESSIONS:
        return _TICKET_SESSIONS[tid]
    sid = session_key(tid)
    if cfg['mode'] == 'local':
        hcfg = HarnessConfig(
            max_context_tokens=int(cfg['max_context_tokens']),
            shortterm_keep_pct=float(cfg['shortterm_keep_pct']),
            workdir=os.path.join(cfg['workdir'], 'tickets', str(tid)),
            memory_preamble=False,
            classify_mode='post',
        )
        if cfg.get('backend_base_url') and cfg.get('backend_model'):

            llm = OpenAIChat(
                cfg['backend_base_url'],
                cfg['backend_model'],
                api_key=cfg.get('api_key', 'EMPTY'),
            )
        else:
            from shatterling.engine.llm import StubChat

            llm = StubChat()
        harness = MemoryHarness(llm, hcfg)
    elif cfg['mode'] == 'served':
        if not cfg.get('backend_base_url'):
            raise MemoryUnavailable(
                "served mode needs TOKAMAK_MEMORY['backend_base_url']"
            )
        # Eager open at connect time (plan rule 2): creation-time options
        # reach the server, and creation failures surface here instead of
        # mid-conversation. A reopen is idempotent and never reshapes.
        harness = _serve_remote(cfg, sid)
    else:
        raise MemoryUnavailable(f"unknown TOKAMAK_MEMORY mode {cfg['mode']!r}")
    _TICKET_SESSIONS[tid] = harness
def close_ticket_harness(ticket_id: int) -> None:
    """Release a cached ticket harness. Served branches stay open on the
    daemon until :func:`drop_ticket_branch` - closing the cache entry is
    not the end of a branch's server-side lifecycle."""
def drop_ticket_branch(ticket_id: int) -> bool:
    """DELETE /v1/clm/sessions/<id>: end-of-lifecycle cleanup for a served
    ticket branch (be a good citizen; branches are visible in /health).
    Local harnesses have no server-side lifecycle - cache release only."""
    tid = int(ticket_id)
    harness = _TICKET_SESSIONS.pop(tid, None)
    if harness is None:
        return False
    drop = getattr(harness, 'drop', None)
    if drop is not None:  # a future SDK that owns the verb
        try:
            drop()
        except Exception:
            return False
        return True
    if hasattr(harness, '_snap_path'):  # RemoteHarness: mirror DELETE
        try:
            harness._req('DELETE', harness._snap_path)
        except Exception:
            return False
        return True
def monitor_url_for(mode: str, base_url: str = '') -> str:
    """Deep link for humans to watch the chat/memory session."""
    if mode == 'served' and base_url:
        root = base_url[: -len('/v1')] if base_url.endswith('/v1') else base_url
        return root.rstrip('/') + '/?session='
def archive_live(harness) -> int:
    """End-of-session offload: every live long/short block goes to the ticket

    Local harnesses only: a served session's memory lives on the daemon (its
    own eviction engine owns the window), so remote ends are a no-op here."""
    blocks = getattr(harness, 'blocks', None)
    if blocks is None:
        return 0
    live = [b for b in blocks if not b.pinned and b.tag in ('longterm', 'shortterm')]
    if not live:
        return 0
    harness._archive(live)
    doomed_ids = {id(b) for b in live}
    harness.blocks = [b for b in harness.blocks if id(b) not in doomed_ids]
    harness.mirror_files()
def distill_summary(harness, ticket_name: str = '', limit_chars: int = 1200) -> str:

    Reads longterm blocks via ``live_blocks()`` which both the local harness
    and the served RemoteHarness expose; content is read attribute- or dict-
    style depending on what the transport hands back.
    """
    facts = []
    try:
        for b in harness.live_blocks('longterm'):
            if isinstance(b, dict):
                content = b.get('content', '')
                pinned = b.get('pinned', False)
            else:
                content = getattr(b, 'content', '')
                pinned = getattr(b, 'pinned', False)
            if content and not pinned:
                facts.append(str(content).strip())
    except Exception:
        facts = []
    head = f"Session notes for '{ticket_name}': {len(facts)} durable fact(s)."
    body = '\n'.join(f'- {f}' for f in facts)
    full = f'{head}\n{body}'.strip()
def model_distill(harness, ticket_name: str = '') -> str:

    Only meaningful with a real (non-stub) backend configured; failures are
    reported as empty string so callers keep the deterministic summary.
    Served branches answer via turn() (reply under ``choices``); local
    harnesses answer with the bare reply string.
    """
    prompt = (
        f"Summarize what we learned about ticket '{ticket_name}' in at "
        "most 5 bullet lines, past tense, no preamble."
    )
    try:
        if hasattr(harness, 'turn'):  # RemoteHarness (explicit session API)
            resp = remote_ingest(harness, prompt)
            reply = resp['choices'][0]['message']['content']
        else:
            reply = harness.step(prompt)
        return str(reply).strip()[:1200]
    except Exception:
def seed_from_archive(harness, query: str = '', limit: int = 8) -> int:

    Local harnesses admit them directly as shortterm blocks. Served branches
    get the same effect through an explicit application-level copy (plan
    rule 6: isolation is structural - there is no broadcast semantics):
    each archived fact is replayed as an ingest turn so the server-side
    classifier re-tags it inside this branch.
    """
    n = 0
    if hasattr(harness, 'add'):  # local MemoryHarness
        for rec in harness.recall(query or '', limit=limit):
            harness.add(
                'user',
                f"[seed from previous session] {rec['content']}",
                tag='shortterm',
            )
            n += 1
        harness.evict()
        harness.mirror_files()
        return n
    for rec in harness.recall(query or '', limit=limit):
        remote_ingest(harness, f"[seed from previous session] {rec['content']}")
        n += 1
