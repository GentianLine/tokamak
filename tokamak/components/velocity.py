"""Unicorn component: velocity history, burndown and sprint cutoff forecast.

The burndown geometry lives here (component-side presentation math) while the
series data comes from ``tokamak.burndown``, which itself defers to
``services.velocity_series`` — the service layer stays authoritative for all
aggregates (AGENTS.md §1).
"""

from django_unicorn.components import UnicornView

from .. import burndown, services
from ..models import Project

# SVG chart geometry (viewBox units; the svg element scales responsively).
CHART_W = 520
CHART_H = 190
PAD_L = 46
PAD_R = 14
PAD_T = 12
PAD_B = 30


class VelocityView(UnicornView):
    template_name = 'unicorn/velocity.html'

    project_id: int = None

    # Computed values are methods, not properties (see BoardView note).
    def project(self):
        if not self.project_id:
            return None
        return Project.objects.get(pk=self.project_id)

    def series(self):
        project = self.project()
        if project is None:
            return []
        return services.velocity_series(project)

    def forecast(self):
        project = self.project()
        if project is None:
            return {}
        return services.estimate_cutoff(project)

    def max_points(self):
        points = [row['points'] for row in self.series()] or [0]
        return max(max(points), 1)

    # ------------------------------------------------------------------
    # Burndown: committed scope vs accepted delivery, per iteration.
    # Computed values are methods (see note above), never properties.
    # ------------------------------------------------------------------
    def burndown(self):
        """Scope-vs-delivered bars plus summary facts for the panel."""
        project = self.project()
        if project is None:
            return {
                'rows': [],
                'summary': {
                    'iterations': 0,
                    'scope_points': 0,
                    'delivered_points': 0,
                    'hit_rate': None,
                    'best': 0,
                    'worst': 0,
                    'ideal_hits': 0,
                },
            }
        return burndown.burndown_series(project)

    def remaining(self):
        """Classic burndown line: points still open after each iteration."""
        project = self.project()
        if project is None:
            return {'rows': [], 'start_points': 0, 'end_points': 0, 'max_points': 1}
        return burndown.remaining_series(project)

    def burndown_svg(self):
        """Inline-SVG geometry for the chart: no JS library, no build step.

        Returns ``{'view_box', 'bars': [...], 'ideal_line', 'burn_line',
        'axis': [...]}`` where every coordinate is a plain number/string
        ready to drop into template attributes. Bars carry committed scope
        (background) and accepted delivery (foreground); the burn line tracks
        remaining points against the ideal pace.
        """
        data = self.burndown()
        rem = self.remaining()
        rows = data['rows']
        inner_w = CHART_W - PAD_L - PAD_R
        inner_h = CHART_H - PAD_T - PAD_B
        if not rows:
            return {
                'view_box': f'0 0 {CHART_W} {CHART_H}',
                'bars': [],
                'ideal_line': None,
                'burn_line': None,
                'axis': [],
                'empty': True,
            }

        max_pts = max([r['committed'] for r in rows] + [1])
        count = len(rows)
        slot = inner_w / count
        bar_w = min(26.0, slot * 0.5)

        def y(points):
            return PAD_T + inner_h * (1.0 - float(points) / max_pts)

        bars = []
        for idx, row in enumerate(rows):
            cx = PAD_L + slot * idx + slot / 2.0
            scope_h = inner_h * float(row['committed']) / max_pts
            deliv_h = inner_h * float(row['accepted']) / max_pts
            bars.append(
                {
                    'x': round(cx - bar_w / 2.0, 2),
                    'scope_y': round(PAD_T + inner_h - scope_h, 2),
                    'scope_h': round(scope_h, 2),
                    'deliv_y': round(PAD_T + inner_h - deliv_h, 2),
                    'deliv_h': round(deliv_h, 2),
                    'label_x': round(cx, 2),
                    'label_y': round(CHART_H - PAD_B + 14, 2),
                    'number': row['number'],
                    'committed': row['committed'],
                    'accepted': row['accepted'],
                    'unaccepted': row['unaccepted'],
                    'ideal': row['ideal'],
                    'ratio': row['ratio'],
                    'active': row['active'],
                }
            )

        rem_rows = rem['rows']
        burn_line = None
        ideal_line = None
        if len(rem_rows) >= 1:
            pts = []
            ideal_pts = []
            for idx, r in enumerate(rem_rows):
                cx = PAD_L + slot * idx + slot / 2.0
                pts.append(f"{round(cx, 2)},{round(y(r['remaining']), 2)}")
                ideal_pts.append(f"{round(cx, 2)},{round(y(r['ideal_remaining']), 2)}")
            burn_line = ' '.join(pts)
            if len(rem_rows) >= 2:
                ideal_line = ' '.join(ideal_pts)

        ticks = 4
        axis = []
        for t in range(ticks + 1):
            val = max_pts * t / ticks
            axis.append(
                {
                    'y': round(y(val), 2),
                    'label': (int(val) if float(val).is_integer() else round(val, 1)),
                }
            )

        return {
            'view_box': f'0 0 {CHART_W} {CHART_H}',
            'bars': bars,
            'ideal_line': ideal_line,
            'burn_line': burn_line,
            'axis': axis,
            'empty': False,
        }

    def recalculate(self):
        """Explicit refresh hook (the button); renders are already live."""
        pass
