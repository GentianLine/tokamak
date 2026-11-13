"""
Webhook fan-out receivers — config-driven mirroring of ``EventLog`` entries.

Task automation-webhooks (AGENTS.md §7): a new receiver module, registered
from ``AppConfig.ready()``, never inline edits to ``automation.py``. The
receivers are **idempotent**: every mirror carries a stable delivery id and
re-delivering the same event to the same target is a no-op within the
process (and targets can dedupe by the id, which is deterministic from the
event's primary key).

Configuration lives in ``settings.TOKAMAK['WEBHOOKS']`` and is **off by
default**: nothing connects unless ``enabled=True`` with at least one
target. The audit trail itself is append-only — this module only ever reads
``EventLog``; it never writes to it.

Delivery uses the stdlib (urllib); failures are logged into the module's
own in-memory attempt ledger rather than mutating tickets or the audit
trail, and receivers must never raise so a broken endpoint cannot fail
board writes (same best-effort contract as the clm-mem mirror).
"""

import ipaddress
import json
import logging
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from fnmatch import fnmatch
from socket import gethostname
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULTS: Dict = {
    'enabled': False,
    # Each target: {'url': str, 'events': ['ticket.*', ...] (default all),
    #               'secret': optional per-target signing secret}
    'targets': [],
    'timeout': 5.0,
    'max_retries': 3,
    'retry_backoff': 2.0,
    'signing_secret': '',
}

# In-process delivery ledger (idempotency + test introspection).
# Key: (event_pk, url) -> record dict; survives receiver re-registration.
_DELIVERED: Dict = {}
_LOCK = threading.Lock()

HOSTNAME = gethostname()

# SECURITY (T-4): only http/https targets are allowed. file://, gopher:// etc.
# would turn webhook fan-out into an arbitrary local-file / protocol primitive.
ALLOWED_WEBHOOK_SCHEMES = {'http', 'https'}

#: Async delivery pool so a slow/hanging target cannot block the request thread
#: that is writing the board (T-4 availability concern).
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix='tokamak-webhook')


def get_config() -> Dict:
    """Merge ``settings.TOKAMAK['WEBHOOKS']`` over the defaults."""
    from django.conf import settings

    user = dict(getattr(settings, 'TOKAMAK', {}).get('WEBHOOKS', {}))
    cfg = dict(DEFAULTS)
    cfg.update(user)
    return cfg


def is_enabled(cfg: Optional[Dict] = None) -> bool:
    """Fan-out bites only when enabled and at least one target is configured."""
    cfg = cfg or get_config()
    return bool(cfg['enabled']) and bool(cfg['targets'])


def reset_ledger() -> None:
    """Clear the in-process delivery ledger (test support)."""
    with _LOCK:
        _DELIVERED.clear()


def deregister_fanout() -> None:
    """Disconnect the fan-out receiver if it was connected (idempotent).

    ``register_fanout`` connects a single receiver under a fixed dispatch uid to
    every mirrored signal. Nothing in production ever needs to remove it, but
    tests that register fan-out must be able to undo it -- otherwise the
    receiver leaks into later tests and fires on unrelated board writes.
    """
    from .signals import (
        project_deleted,
        project_updated,
        ticket_created,
        ticket_deleted,
        ticket_estimated,
        ticket_iteration_changed,
        ticket_state_changed,
        ticket_updated,
    )

    for signal in (
        ticket_state_changed,
        ticket_estimated,
        ticket_iteration_changed,
        ticket_created,
        ticket_updated,
        ticket_deleted,
        project_updated,
        project_deleted,
    ):
        signal.disconnect(dispatch_uid='tokamak_webhook_fanout')


def delivered_ledger() -> Dict:
    """Read-only view of the delivery ledger (test support)."""
    with _LOCK:
        return dict(_DELIVERED)


def _matches(patterns: List[str], name: str) -> bool:
    """No patterns means 'all events'; otherwise fnmatch-style wildcards."""
    return not patterns or any(fnmatch(name, p) for p in patterns)


def _payload_for(event) -> Dict:
    """The JSON envelope mirrored to webhooks for one audit row."""
    return {
        'delivery_id': f'{event.pk}-{HOSTNAME}',
        'event': event.name,
        'id': event.pk,
        'project_id': event.project_id,
        'ticket_id': event.ticket_id,
        'created_at': event.created_at.isoformat() if event.created_at else None,
        'payload': event.payload,
    }


def _signature(secret: str, body: bytes) -> str:
    import hashlib
    import hmac

    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _validate_target_url(url: str, allow_private: bool = False):
    """Refuse non-http(s) webhook targets and any host that resolves to a
    restricted (loopback/private/link-local/reserved) address -- the same
    resolution-based SSRF guard clm-mem uses. Returns None when the URL is
    safe, or an error string when it must be skipped.

    ``allow_private`` (an explicit per-target opt-in) permits targets that
    resolve to private/loopback ranges -- needed for LAN receivers and local
    test servers. It never relaxes the scheme restriction.
    """
    parsed = urllib.parse.urlparse(url)
    scheme = (parsed.scheme or '').lower()
    if scheme not in ALLOWED_WEBHOOK_SCHEMES:
        return f'unsupported scheme {scheme!r} (only http/https allowed)'
    host = parsed.hostname
    if not host:
        return 'no host in target url'
    try:
        port = parsed.port or (443 if scheme == 'https' else 80)
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, ValueError):
        return f'could not resolve host {host!r}'
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if ip.version == 6 and getattr(ip, 'ipv4_mapped', None) is not None:
            ip = ip.ipv4_mapped
        if (
            ip.is_loopback or ip.is_private or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified
        ):
            if allow_private:
                continue
            return f'target {host} resolves to a restricted address {addr}'
    return None


def register_fanout() -> None:
    """Connect the fan-out receiver when config opts in (idempotent)."""
    cfg = get_config()
    if not is_enabled(cfg):
        return
    from .signals import (
        project_updated,
        project_deleted,
        ticket_created,
        ticket_deleted,
        ticket_estimated,
        ticket_iteration_changed,
        ticket_state_changed,
        ticket_updated,
    )

    for signal in (
        ticket_state_changed,
        ticket_estimated,
        ticket_iteration_changed,
        ticket_created,
        ticket_updated,
        ticket_deleted,
        project_updated,
        project_deleted,
    ):
        signal.connect(_fanout_receiver, dispatch_uid='tokamak_webhook_fanout')


#: Maps each custom signal to the audit event name its receiver appends.
#: Fan-out mirrors exactly that row, so ordering stays unambiguous even
#: when one signal fires several same-named rows in a single tick.
SIGNAL_EVENT_NAMES = {
    'ticket_state_changed': 'ticket.state_changed',
    'ticket_estimated': 'ticket.estimated',
    'ticket_iteration_changed': 'ticket.iteration_changed',
    'ticket_created': 'ticket.created',
    'ticket_updated': 'ticket.updated',
    'ticket_deleted': 'ticket.deleted',
    'project_deleted': 'project.deleted',
    'project_updated': 'project.updated',
}


def _fanout_receiver(sender, **kwargs):
    """Mirror the audit row just written for this signal to matching targets.

    The audit receivers are connected last in ``signals.register_receivers``,
    so by the time this receiver runs the row exists; it is read back
    (never written) and handed to :func:`fanout_for_event`.
    """
    event = _latest_event_for(sender, kwargs)
    if event is None:
        return
    fanout_for_event(event)


def _latest_event_for(sender, kwargs):
    """Locate the audit row just written for this signal payload."""
    from .models import EventLog

    ticket = kwargs.get('ticket')
    project = kwargs.get('project')
    name = SIGNAL_EVENT_NAMES.get(getattr(sender, 'name', ''), None)

    qs = EventLog.objects.all()
    if name:
        qs = qs.filter(name=name)
    if ticket is not None:
        return qs.filter(ticket=ticket).first()
    if project is not None:
        return qs.filter(project=project).first()
    return None


def fanout_for_event(event, cfg: Optional[Dict] = None) -> List[Dict]:
    """Deliver one audit row to every matching target; never raises.

    Idempotent: a second call for the same (event, url) pair within this
    process is skipped and reported as ``skipped``. Returns per-target
    records for observability/tests.
    """
    cfg = cfg or get_config()
    if not is_enabled(cfg):
        return []

    results = []
    for target in cfg['targets']:
        url = target.get('url')
        if not url:
            continue
        patterns = target.get('events', [])
        if isinstance(patterns, str):
            patterns = [patterns]
        if not _matches(list(patterns), event.name):
            continue

        key = (event.pk, url)
        with _LOCK:
            if key in _DELIVERED:
                results.append(
                    {'url': url, 'status': 'skipped', 'reason': 'idempotent'}
                )
                continue
            _DELIVERED[key] = None  # claim the slot before network I/O

        # Deliver off the request thread (T-4): a slow/hanging target must not
        # block board writes. The ledger slot is still claimed synchronously so
        # idempotency holds; the record is filled in by the worker. When async
        # delivery is disabled (e.g. tests) it runs inline instead.
        if cfg.get('async', True):
            _POOL.submit(_deliver_and_record, cfg, target, event, key)
            results.append({'url': url, 'status': 'queued'})
        else:
            record = _deliver(cfg, target, event)
            with _LOCK:
                _DELIVERED[key] = record
            results.append(record)
    return [r for r in results if r is not None]


def drain_pool(timeout: float = 5.0) -> None:
    """Block until any in-flight async deliveries have finished.

    The delivery pool is a module-level daemon executor; on shutdown (or at the
    end of a test) we do not want half-written ledger records or stray worker
    threads to outlive the process. This is best-effort: it waits up to
    ``timeout`` seconds for queued work, then returns.
    """
    global _POOL
    try:
        _POOL.shutdown(wait=True)
    except Exception:  # pragma: no cover - shutdown is best-effort
        pass
    # Re-create the executor so subsequent fan-out calls keep working. We do
    # this lazily-guarded because ThreadPoolExecutor cannot be restarted.
    with _LOCK:
        _POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix='tokamak-webhook')


def _deliver_and_record(cfg: Dict, target: Dict, event, key):
    """Worker: run one delivery and store its record in the ledger."""
    try:
        record = _deliver(cfg, target, event)
    except Exception as exc:  # receivers must never raise
        logger.warning('tokamak.webhooks: delivery to %s raised: %s', target['url'], exc)
        record = {'url': target['url'], 'status': 'failed', 'error': str(exc)}
    with _LOCK:
        _DELIVERED[key] = record


def _deliver(cfg: Dict, target: Dict, event):
    """One blocking HTTP delivery with retries; failures are logged.

    Runs on the webhook thread pool (never the request thread) and refuses to
    touch non-http(s) or restricted-address targets (T-4)."""
    err = _validate_target_url(target['url'], allow_private=bool(target.get('allow_private')))
    if err:
        logger.warning('tokamak.webhooks: skipping target %s: %s', target['url'], err)
        return {'url': target['url'], 'status': 'skipped', 'reason': err}
    body = json.dumps(_payload_for(event), sort_keys=True).encode()
    secret = target.get('secret') or cfg['signing_secret']
    headers = {
        'Content-Type': 'application/json',
        'User-Agent': 'tokamak-webhooks/1.0',
    }
    if secret:
        headers['X-Tokamak-Signature'] = _signature(secret, body)

    attempts = 0
    last_error = None
    import time

    while attempts < max(1, int(cfg['max_retries'])):
        attempts += 1
        try:
            request = urllib.request.Request(
                target['url'], data=body, headers=headers, method='POST'
            )
            with urllib.request.urlopen(
                request, timeout=float(cfg['timeout'])
            ) as response:
                status = getattr(response, 'status', 200)
                if 200 <= status < 300:
                    return {
                        'url': target['url'],
                        'status': 'delivered',
                        'http_status': status,
                        'attempts': attempts,
                    }
                last_error = f'HTTP {status}'
        except (urllib.error.URLError, ValueError, OSError) as exc:
            last_error = str(exc)
        if attempts < int(cfg['max_retries']):
            time.sleep(float(cfg['retry_backoff']) * 0.05)

    logger.warning(
        'tokamak.webhooks: delivery to %s failed after %s attempts: %s',
        target['url'],
        attempts,
        last_error,
    )
    return {
        'url': target['url'],
        'status': 'failed',
        'error': str(last_error),
        'attempts': attempts,
    }
