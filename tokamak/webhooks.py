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

import json
import logging
import threading
import urllib.error
import urllib.request
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


def register_fanout() -> None:
    """Connect the fan-out receiver when config opts in (idempotent)."""
    cfg = get_config()
    if not is_enabled(cfg):
        return
    from .signals import (
        project_updated,
        ticket_created,
        ticket_estimated,
        ticket_iteration_changed,
        ticket_state_changed,
    )

    for signal in (
        ticket_state_changed,
        ticket_estimated,
        ticket_iteration_changed,
        ticket_created,
        project_updated,
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

        record = _deliver(cfg, target, event)
        with _LOCK:
            _DELIVERED[key] = record
        results.append(record)
    return [r for r in results if r is not None]


def _deliver(cfg: Dict, target: Dict, event):
    """One blocking HTTP delivery with retries; failures are logged."""
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
