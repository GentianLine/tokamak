"""Template filters exposing the shared markdown renderer.

Usage in templates::

    {% load markdown_tags %}
    {{ ticket.description|md_render }}      # safe GFM HTML for detail pages
    {{ ticket.description|md_excerpt:140 }} # plain-text digest for cards
"""

from django import template

from ..markdown import excerpt as _excerpt
from ..markdown import render as _render

register = template.Library()


@register.filter(is_safe=True)
def md_render(value):
    """GFM + Tokamak refs rendered to safe HTML (never mutates)."""
    return _render(str(value or ''))


@register.filter
def md_excerpt(value, limit=140):
    """Plain-text digest of a markdown blob (cards, rows, titles)."""
    return _excerpt(str(value or ''), int(limit))
