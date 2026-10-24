"""Per-iteration burndown series: committed scope vs accepted delivery.

The velocity panel already shows *velocity* (accepted points per completed
iteration). A burndown answers the complementary question: how much of the
scope committed into each iteration actually burned down to accepted, and
how did that compare against an ideal pace? This module builds those series
purely from read models — it never mutates anything, and it delegates all
per-iteration aggregates to ``tokamak.services`` / the model helpers so the
service layer stays authoritative (see AGENTS.md §1).

Two views of the same history are produced:

* **scope vs delivered** bars per iteration: committed points (every ticket
  in the sprint, estimated or not) versus accepted points — plus an ideal
  straight-line pace scaled from the observed delivery rate.
* **remaining** line: scope minus accepted, i.e. what was still open at the
  end of each iteration; the classic burndown "burn" against a flat zero
  target.

Only completed iterations participate (``Iteration.is_completed``), matching
``services.velocity_series``; an optional *active* row can be included for
the in-flight sprint so the chart never stops dead mid-sprint.
"""

from . import services


def _start_for(project, number):
    """Start date (ISO) of the iteration numbered ``number``, or None."""
    for it in project.iterations.all():
        if it.number == number:
            return it.start_date.isoformat()
    return None


def _series(project, limit=10, include_active=False):
    """Completed-iteration rows (oldest first) from the authoritative
    ``services.velocity_series``, plus an optional in-flight row.

    ``include_active`` appends a row for the currently running iteration (if
    any) so the burndown extends to today instead of stalling at the last
    finished sprint.
    """
    rows = list(services.velocity_series(project, limit=limit))
    if not include_active:
        return rows

    active = [it for it in project.iterations.all() if it.is_active]
    if not active:
        return rows
    current = sorted(active, key=lambda it: it.number)[-1]
    numbers = {row['number'] for row in rows}
    if current.number in numbers:
        return rows
    rows.append(
        {
            'number': current.number,
            'end_date': current.end_date.isoformat(),
            'points': current.accepted_points,
            'committed': current.committed_points,
            'start': current.start_date.isoformat(),
            'active': True,
        }
    )
    return rows


def _summary(rows, scope, delivered):
    """Aggregate facts shown above the chart; honest about no-history."""
    hit_rate = round(delivered / scope, 3) if scope else None
    ideal_hits = sum(
        1
        for r in rows
        if r['ideal'] is not None and r['committed'] > 0 and r['accepted'] >= r['ideal']
    )
    return {
        'iterations': len(rows),
        'scope_points': scope,
        'delivered_points': delivered,
        'hit_rate': hit_rate,
        'best': max((r['accepted'] for r in rows), default=0),
        'worst': min((r['accepted'] for r in rows), default=0) if rows else 0,
        'ideal_hits': ideal_hits,
    }


def burndown_series(project, limit=10):
    """Scope-vs-delivered bars for every completed iteration.

    Each row: ``number``, ``start``/``end`` (ISO dates), ``committed`` (scope
    line), ``accepted`` (what actually burned), ``unaccepted`` (scope minus
    accepted — the carry-over debt), ``ideal`` (a straight pace scaled to the
    observed mean delivery rate; None when there is no history) and ``ratio``
    (accepted/committed, 0..1) used for bar shading. A trailing summary
    reports totals plus how often the team hit its ideal pace.
    """
    rows = _series(project, limit=limit)
    if not rows:
        return {'rows': [], 'summary': _summary([], 0, 0)}

    delivered = [int(row['points']) for row in rows]
    mean = sum(delivered) / len(delivered) if delivered else 0.0

    out = []
    for row in rows:
        committed = int(row['committed'])
        accepted = int(row['points'])
        ideal = round(mean, 2) if mean else None
        start = row.get('start') or _start_for(project, row['number'])
        out.append(
            {
                'number': row['number'],
                'start': start,
                'end': row['end_date'],
                'committed': committed,
                'accepted': accepted,
                'unaccepted': max(0, committed - accepted),
                'ideal': ideal,
                'ratio': round(accepted / committed, 2) if committed else 0.0,
                'active': bool(row.get('active', False)),
            }
        )
    scope = sum(r['committed'] for r in out)
    delivered_total = sum(r['accepted'] for r in out)
    return {'rows': out, 'summary': _summary(out, scope, delivered_total)}


def remaining_series(project, limit=10):
    """Classic burndown line: points still open per iteration.

    ``remaining`` is committed minus accepted at each iteration end; the
    ideal line walks linearly from the first scope down to zero across the
    observed window (a flat target of "burn everything by the last sprint").
    Returns rows plus axis endpoints and the axis maximum.
    """
    rows = burndown_series(project, limit=limit)['rows']
    if not rows:
        return {'rows': [], 'start_points': 0, 'end_points': 0, 'max_points': 1}

    start_points = rows[0]['committed']
    end_points = rows[-1]['unaccepted']
    span = max(1, len(rows) - 1)
    out = []
    for idx, row in enumerate(rows):
        ideal_remaining = round(start_points - (start_points / span) * idx, 2)
        out.append(
            {
                'number': row['number'],
                'end': row['end'],
                'remaining': row['unaccepted'],
                'committed': row['committed'],
                'accepted': row['accepted'],
                'ideal_remaining': ideal_remaining,
                'active': row['active'],
            }
        )
    return {
        'rows': out,
        'start_points': start_points,
        'end_points': end_points,
        'max_points': max([r['committed'] for r in rows] + [1]),
    }
