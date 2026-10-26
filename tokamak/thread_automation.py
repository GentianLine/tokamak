"""Thread-aware automated processors (new module, merge-friendly).

One step further than the built-in receivers in ``automation.py``: these ones
*write back into the thread*. When a blocker edge appears or disappears, the
blocked ticket gets a system comment saying so — the sort of note people
expect a tracker to leave for itself, and a demonstration that automation can
use the same service layer as humans (``services.add_comment``) without ever
touching the ORM directly.

Opt-in, mirroring the memory bridge pattern (AGENTS.md §1): the receivers are
connected unconditionally but no-op unless ``TOKAMAK_THREADS`` turns them on,
so the default signal graph stays byte-identical and existing EventLog
counts are untouched. They are best-effort too: a receiver never raises, so a
threading hiccup cannot fail a board write.

Routes/behavior (no new HTTP surface):
    blocker_added    -> system comment on the blocked ticket
    blocker_removed  -> system comment recording how it cleared
"""

from django.conf import settings
from django.dispatch import receiver

from .models import Comment
from .signals import blocker_added, blocker_removed


def threads_enabled():
    """True when TOKAMAK_THREADS opts into automatic thread notes."""
    cfg = getattr(settings, 'TOKAMAK_THREADS', {}) or {}
    return bool(cfg.get('auto_comment_blockers', False))


@receiver(blocker_added)
def auto_comment_blocker(sender, blocker, **kwargs):
    """Leave a note on the blocked ticket: who is holding it down, and why."""
    if not threads_enabled():
        return
    from . import services

    reason = f' — {blocker.reason}' if blocker.reason else ''
    body = (
        f"Blocked by #{blocker.source_id} *{blocker.source.name}*{reason}.\n\n"
        "Accepting that ticket clears this one automatically."
    )
    try:
        services.add_comment(
            blocker.target,
            body,
            author='tokamak',
            kind=Comment.Kind.SYSTEM,
        )
    except Exception:  # best-effort: never fail the blocker write
        return


@receiver(blocker_removed)
def auto_comment_unblock(sender, blocker, reason='manual', **kwargs):
    """Record how a blocker cleared; 'accepted' means the cause delivered."""
    if not threads_enabled():
        return
    from . import services

    verb = {
        'accepted': 'delivered',
        'manual': 'resolved manually',
        'cycle': 'dropped (would have closed a cycle)',
    }.get(reason, reason)
    body = f"Unblocked: cause #{blocker.source_id} {verb}."
    try:
        services.add_comment(
            blocker.target,
            body,
            author='tokamak',
            kind=Comment.Kind.SYSTEM,
        )
    except Exception:  # best-effort, as above
        return
