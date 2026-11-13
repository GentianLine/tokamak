# Tokamak — Security Audit & Bug Report

**Scope:** the Django project-management app (`config/`, `tokamak/`) as of `main`
(commit `d09b448`), including the REST API, Unicorn components, admin, markdown
renderer, webhooks, and the clm-mem memory bridge.
**Method:** manual review of every view/viewset/template and the settings module;
cross-checking auth/CSRF posture against each route; targeted review of the
markdown renderer and webhook delivery for injection/SSRF.
**Date:** 2026-11-09

> This is a *review* report, not a patch. Each finding lists severity, affected
> code, why it matters, and a concrete remediation.

---

## 1. Executive summary

Tokamak's **application logic is well-hardened against the classic web bugs**:
there is no raw SQL, no `eval`/`exec`, no `pickle`/`yaml.load`, no template XSS
(no `|safe` on user data — the one `mark_safe` is applied to output of a
renderer that disables raw HTML), and the markdown renderer explicitly sets
`html=False` so injected markup is escaped. That is a strong baseline.

The problems are almost entirely **deployment posture**, not code bugs:

1. **There is no authentication or authorization anywhere.** The DRF API uses
   `SessionAuthentication` with **no** `permission_classes`, and the custom JSON
   views use `csrf_exempt`. Any unauthenticated client can create, modify, and
   delete every project/ticket/iteration/event. This is the single biggest issue.
2. **`DEBUG = True` and a hardcoded insecure `SECRET_KEY` are committed**, and
   `ALLOWED_HOSTS = ['*']`. Shipped as-is to any reachable host, this yields the
   Django debug page (stack traces + settings dump) and trivially forged session
   cookies.
3. **CSRF is disabled on all mutating JSON endpoints** (`@csrf_exempt`), so with
   no auth there is nothing stopping a cross-site request from a logged-in browser
   once sessions exist — or from any script at all today.
4. **Webhook fan-out is an SSRF primitive**: operator-configured `targets[].url`
is fetched with no scheme/host restriction, and the delivery runs in-process on a
   signal (synchronous with board writes).
5. The **clm-mem memory bridge** re-exposes clm-mem's exec/file surface through
   Tokamak's (unauthenticated) API — see the companion report for those details.

The good news: markdown rendering, the audit trail (append-only), and the service
layer are clean. The fixes below are mostly about adding the auth/CSRF/production
settings layer that the code already anticipates but does not enforce.

---

## 2. Findings

### CRITICAL / HIGH

#### T-1 — No authentication or authorization on any API or view
**Where:** `config/settings.py` (`REST_FRAMEWORK['DEFAULT_AUTHENTICATION_CLASSES']`
= `SessionAuthentication`, no `permission_classes`); every viewset in
`tokamak/views.py`; all `@csrf_exempt` JSON views; `tokamak/pages.py`.

The REST API is a full CRUD surface over projects, tickets, iterations, epics,
milestones, blockers and the event log. There is **no** `permission_classes`, no
`login_required`, no token auth. `SessionAuthentication` only enforces CSRF *for
authenticated* sessions; anonymous requests are simply allowed. The code even
passes `user=request.user if request.user.is_authenticated else None` into the
service layer — i.e. it *expects* auth to exist but never requires it.

**Impact:** anyone who can reach the app can read all data and create/modify/delete
all objects (projects, tickets, audit log entries). Combined with T-2 this is
critical on any non-private deployment.
**Remediation:
- Add `DEFAULT_PERMISSION_CLASSES = ['rest_framework.permissions.IsAuthenticated']`
  (or a role model) to `REST_FRAMEWORK`, and protect the JSON views with
  `@login_required` / an explicit permission check.
- Require authentication for all mutating operations; keep reads restricted too if
  projects are sensitive.
- The admin site already gates on Django auth — make the app match it.

#### T-2 — `DEBUG = True`, hardcoded insecure `SECRET_KEY`, `ALLOWED_HOSTS = ['*']` committed
**Where:** `config/settings.py` (top of file).

```
SECRET_KEY = 'django-insecure-roc4k6*3s$...'
DEBUG = True
ALLOWED_HOSTS = ['*']
```

- **Hardcoded secret key in VCS**: anyone with repo access can forge session
  cookies, reset passwords (if password-reset is added), and tamper with signed
  data. The key must come from the environment.
- **`DEBUG = True`** in a reachable deployment exposes the technical debugger:
  full stack traces, local variables, and a settings dump on any unhandled error —
  an information-disclosure and (in some setups) code-execution amplifier.
- **`ALLOWED_HOSTS = ['*']`** disables host validation (the defense against
  host-header attacks / cache poisoning). The comment says "the deployed app will
  supply its own settings module," but there is no such override in the repo —
  this file *is* what runs.

**Impact:** credential/session forgery, sensitive info disclosure, host-header
abuse.
**Remediation:
- `SECRET_KEY = os.environ['DJANGO_SECRET_KEY']` (fail fast if unset).
- `DEBUG` and `ALLOWED_HOSTS` from the environment; never commit `True`/`'*'` as
  the effective production values. Add a real settings override or a
  `.env`-driven module, and delete the committed key (rotate it).

#### T-3 — CSRF disabled on all mutating JSON endpoints (`@csrf_exempt`)
**Where:** `tokamak/memory_views.py`, `tokamak/sessions_views.py`,
`tokamak/reorder_views.py`.

Every POST/DELETE-style JSON endpoint is decorated `@csrf_exempt`. With no auth
(T-1) this is moot today, but the *moment* sessions/auth are introduced, these
endpoints remain fully CSRF-able: a malicious page can drive state changes from a
victim's authenticated browser. DRF's `SessionAuthentication` enforces CSRF for
DRF routes, but these custom function views opt out explicitly.
**Remediation:** remove `@csrf_exempt` and rely on Django's CSRF middleware +
`ensure_csrf_cookie` (already used on the page views), or use a token/JSON API key
for non-browser clients. If browser JSON POSTs are needed, send the CSRF token.

#### T-4 — Webhook fan-out is an SSRF / unvalidated-outbound-URL primitive
**Where:** `tokamak/webhooks.py` (`_deliver`).

`target['url']` from `settings.TOKAMAK['WEBHOOKS']['targets']` is passed straight to
`urllib.request.urlopen` with no scheme or host restriction. Targets are
operator-configured (so it is not *directly* user-controlled), but:
- an operator can misconfigure a target to an internal/metadata address and the
code will happily fetch `http://169.254.169.254/...`, `file://`, etc.;
- delivery runs **synchronously inside the signal handler** (on the request thread
  that is writing the board), with retries — a slow/hanging target blocks user
  writes (availability), and `max_retries`/`timeout` amplify it.
- There is no allowlist of schemes or hosts, unlike the (also weak) guard in the
  clm-mem web tool.
**Impact:** SSRF to internal services / cloud metadata if targets are ever
user-influenced or misconfigured; request-thread blocking → DoS on board writes.
**Remediation:
- Allow only `https://` (and `http://` for localhost in dev); resolve and reject
  private/link-local ranges before connecting.
- Deliver **asynchronously** (queue / thread pool) so a slow target cannot stall
  the write path; keep the existing idempotency ledger.

---

### MEDIUM

#### T-5 — `memory_note` writes arbitrary long-term memory with no auth and no content validation
**Where:** `tokamak/memory_views.py` (`memory_note`), `tokamak/memory.py`
(`open_session`, `remember_event`).

`POST /api/memory/note/` is `@csrf_exempt` and unauthenticated; it injects an
arbitrary text block into a project's clm-mem session (and, in served mode,
sends it to the remote harness as a turn). This is the "agent writes memory" path.
With no auth, any client can poison a project's long-term memory — which then feeds
back into future model turns. It is also the bridge by which Tokamak reaches
clm-mem's exec/file surface (see companion report C-1).
**Remediation:** require authentication + authorization for the memory write
endpoints; validate/size-limit `text`; keep the bridge disabled in production until
auth lands (the code comment already says this — enforce it).

#### T-6 — EventLog / audit trail is fully readable and writable via the API
**Where:** `tokamak/views.py` (`EventLogViewSet` is `ReadOnlyModelViewSet`, but
`ProjectViewSet.destroy` / `TicketViewSet.destroy` write audit rows through the
service layer; nothing gates either).

Because there is no auth (T-1), the append-only audit trail can be read in full by
anyone, and the *creation* of audit rows is driven by unauthenticated mutations.
The "append-only" guarantee is only about the code path, not about access control.
**Remediation:** gate reads behind auth; consider a dedicated read role for the
audit log so it can't be casually enumerated.

#### T-7 — `markdown_suggest` and several views trust client-supplied ids without validation
**Where:** `tokamak/md_views.py` (`_numeric_or_name`), `sessions_views.py`,
`memory_views.py`.

`int(tid)` / `int(pid)` conversions are used after a truthiness check but the value
comes straight from the request. Most call sites wrap in try/except and answer 400,
but `session_list` does `Ticket.objects.filter(pk=int(tid)).first()` inside a
ternary guarded only by `if tid` — a non-numeric `tid` raises `ValueError` → 500
(unhandled). Inconsistent error handling turns malformed input into server errors.
**Remediation:** validate numeric query params once (e.g. a small helper that
returns `None` on bad input) and answer 400 uniformly; never let a bad id reach the
ORM uncaught.

#### T-8 — Django admin is exposed at `/admin/` with no additional hardening
**Where:** `config/urls.py`, `tokamak/admin.py`.

The admin site is mounted and fully functional. It *is* gated by Django auth, but
there is no restriction on who can register superusers in a fresh DB (the first
`createsuperuser`), no rate limiting on the login form, and with T-2's `DEBUG`
posture the admin is reachable during development leaks. Not a code bug, but worth
calling out for deployment.
**Remediation:** put admin behind the same authN as the app (it already is) +
rate-limit/lockout on login; ensure no superusers are auto-created in production
migrations/seeders (`seed_demo` should be dev-only).

---

### LOW / BUGS

#### L-1 — `markdown_preview` has no size cap on the request body beyond the renderer's own limit
**Where:** `tokamak/md_views.py`, `tokamak/markdown.py` (`MAX_INPUT_CHARS`).

The renderer truncates to 64k chars, which is good. But the view reads
`request.POST.get('text', '')` with no explicit cap before that, and there is no
per-request body-size limit in settings (`DATA_UPLOAD_MAX_MEMORY_SIZE` default
applies). Low risk; add an explicit early reject for oversized bodies to fail fast.

#### L-2 — `excerpt()` regexes are applied to arbitrary user text (CPU cost)
**Where:** `tokamak/markdown.py` (`excerpt`).

Several `re.sub` calls with backtracking-prone patterns run on up to 64k of input
per card render. On a board with many tickets this is repeated per row. Not a
security vuln, but a potential CPU amplification / slow-render path under load.
Consider capping excerpt input lower for list views and memoizing.

#### L-3 — `db.sqlite3` (270 KB) is committed to the repository
**Where:** repo root (`tokamak/db.sqlite3`).

A populated development database is checked in. If it ever contains real or
sensitive data, that's a leak; even if demo data, it risks drift and accidental
commit of future secrets/sessions. It should be gitignored and generated by
migrations + a dev seeder.
**Remediation:** add `db.sqlite3` to `.gitignore`, remove from VCS, rely on
`migrate` + `seed_demo` (dev-only).

#### L-4 — Inconsistent exception handling in action endpoints returns 400 for all errors
**Where:** `tokamak/views.py` (`transition`, `estimate`, `assign`).

`except Exception as exc: return Response({'detail': str(exc)}, status=400)`
swallows *all* exceptions (including programmer errors / DB errors) as 400 and
leaks internal exception text to the client. Distinguish validation errors (400)
from unexpected ones (500, logged, generic message).
**Remediation:** catch specific `ValidationError`/domain errors → 400; let others
propagate to a controlled 500 handler that logs the detail but returns a generic
body.

#### L-5 — `UNICORN['AUTO_RELOAD'] = True` and minification off in settings
**Where:** `config/settings.py` (`UNICORN`).

Fine for development; ensure these are turned off in production (auto-reload adds
file-watching overhead and can mask errors). Tie to the same env-driven settings as
T-2.

---

## 3. What is done well (do not regress)

- **Markdown rendering is genuinely safe:** `MarkdownIt('commonmark', {'html': False})`
  escapes raw HTML; link targets are validated by markdown-it (rejects
  `javascript:`/`data:`); external links get `rel="noopener nofollow"` +
  `target=_blank`; the only `mark_safe` wraps *this* renderer's output. Bare-ref
  and explicit-ref rewriting never emits raw user HTML.
- **No injection primitives:** no raw SQL, no `eval`/`exec`, no `pickle`/
  `yaml.load`, no dynamic import of input. All persistence goes through the ORM
  and a service layer.
- **Audit trail is append-only by construction** (`EventLogAdmin` disables
  add/change/delete; receivers only read it back).
- **Service-layer indirection** means browser (Unicorn), API, and automation all
  mutate through the same code path with consistent signals — good for keeping one
  security-relevant write path to reason about.
- **Webhook signing** uses HMAC-SHA256 when a secret is configured — correct use of
  `hmac`.

---

## 4. Prioritized remediation checklist

1. **Add authentication + authorization everywhere** (DRF permission classes,
   `@login_required` on JSON views, role model). *(T-1)* — highest priority.
2. **Move `SECRET_KEY`/`DEBUG`/`ALLOWED_HOSTS` to the environment; rotate and
   remove the committed key.** *(T-2)*
3. **Remove `@csrf_exempt`** from mutating JSON endpoints (or move to token auth). *(T-3)*
4. **Harden webhooks:** allowlist schemes/hosts, resolve+range-check, deliver async. *(T-4)*
5. **Auth + validate the memory bridge write endpoints; keep it off in prod until auth lands.** *(T-5)*
6. **Gate audit-log reads behind auth.** *(T-6)*
7. **Validate numeric query params uniformly → 400, never 500 on bad ids.** *(T-7)*
8. **Rate-limit/lockout admin login; keep `seed_demo` dev-only.** *(T-8)*
9. **Distinguish validation vs unexpected errors in action endpoints (no leaked tracebacks).** *(L-4)*
10. **Gitignore + remove the committed `db.sqlite3`.** *(L-3)*

---

## 5. Cross-repository note (clm-mem ↔ Tokamak)

Tokamak's memory bridge (`tokamak/memory.py`, `memory_views.py`, `sessions_views.py`)
is a *client* of the clm-mem `purslane` server. Several clm-mem findings therefore
apply to Tokamak transitively:

- **clm-mem C-1/C-2 (unauthenticated RCE / weak jail):** if Tokamak runs in
  `mode='served'` against a non-loopback clm-mem proxy without an API key, the
  unauthenticated Tokamak API becomes a front door to clm-mem's exec/file surface.
  Keep the bridge on loopback + API key, or keep it disabled.
- **clm-mem C-3/C-4 (SSRF / arbitrary-scheme fetch):** reachable through the bridge
  when `web_fetch` is enabled on a session Tokamak opens.

**Net:** treat the clm-mem proxy as an untrusted execution endpoint. Do not expose
it beyond loopback without auth, and do not let the (currently unauthenticated)
Tokamak API reach it in production until both T-1 and clm-mem C-1 are fixed.

---

*Generated as part of a paired security review of `clm-mem` and `tokamak`. The
companion report for the memory harness lives in `../clm-mem/SECURITY_AUDIT.md`.*
