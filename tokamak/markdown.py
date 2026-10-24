"""
Server-side Markdown rendering for everything a human writes in a description.

The renderer implements GitHub Flavored Markdown (tables, task lists,
strikethrough, autolinks, footnotes, alerts, fenced code) on top of
markdown-it-py, plus Tokamak-native references to tickets and projects:

* ``#12``                          -- bare ticket reference; only linked when
  ticket 12 exists, so prose like "color #f5f5f5" or an unresolvable id stays
  plain text
* ``ticket:12`` / ``tkt:12``       -- bare ticket reference (same existence rule)
* ``project:3``                    -- bare project reference (existence rule)
* ``[Label](ticket:12)``           -- explicit reference; always linked, even
  when the target is missing, because it is deliberate rather than incidental
* ordinary markdown links to internal paths (``[Board](/projects/3/)``) work
  unchanged.

Safety: raw HTML is never parsed (markdown-it's ``html=False`` escapes it), and
markdown-it's link validation rejects ``javascript:`` / ``data:`` style targets.
External links additionally get ``rel="noopener nofollow external"`` and open
in a new tab; internal links stay in the same tab.

Read-only by design: this module queries names to build labels but never
mutates -- all writes still live in ``tokamak/services.py``.
"""

from __future__ import annotations

import re

from django.utils.safestring import mark_safe
from markdown_it import MarkdownIt
from markdown_it.token import Token
from mdit_py_plugins.gfm import gfm_plugin

__all__ = ['render', 'excerpt', 'MAX_INPUT_CHARS']

# Guard against a runaway textarea POSTing megabytes into the renderer.
MAX_INPUT_CHARS = 64_000


def _compile_bare():
    """Build the bare-reference regex with explicit lookarounds.

    A bare reference must start at the beginning of the text token or after
    whitespace/an opening delimiter, and cannot be glued to a word char
    (``#12abc``, ``color #f5f5f5`` -- the second has no leading delimiter
    problem but fails the hex check only incidentally; existence checks below
    handle it: #f5f5f5 is not a pure number so it never matches).
    """
    return re.compile(
        r"(?:^|(?<=[\s(\[{<>]))"
        r"(?:"
        r"#(?P<hash>\d{1,9})"
        r"|(?:ticket|tkt):(?P<colon>\d{1,9})"
        r"|(?:project|proj):(?P<pnum>\d{1,9})"
        r")(?![\w])",
    )


BARE_REF_RE = _compile_bare()

# An explicit reference written as a markdown link target: [x](ticket:12).
SCHEME_RE = re.compile(r'^(?:(ticket|tkt|project|proj):(\d{1,9}))$')

_TAG_RE = re.compile(r'<[^>]+>')
_WS_RE = re.compile(r'\s+')

# ---------------------------------------------------------------------------
# Reference resolution (read-only DB lookups)
# ---------------------------------------------------------------------------


def _ticket_label(pk: int):
    """Return a hover title for ticket ``pk`` when it exists, else None."""
    from .models import Ticket  # lazy import keeps this module import-safe

    row = Ticket.objects.filter(pk=pk).values_list('name', flat=True).first()
    return f'#{pk} · {row}' if row is not None else None


def _project_label(pk: int):
    """Return a hover title for project ``pk`` when it exists, else None."""
    from .models import Project

    row = Project.objects.filter(pk=pk).values_list('name', flat=True).first()
    return f'project {pk} · {row}' if row is not None else None


def _url_for(is_project: bool, pk: int) -> str:
    return f'/projects/{pk}/' if is_project else f'/tickets/{pk}/'


def _resolve(match):
    """Map a bare-reference match to ``(url, title)``, or None when missing.

    The existence rule lives here: an unresolvable id stays plain text so
    prose like ``color #f5f5f5`` (not numeric) or a stale ``#99`` never
    becomes a link.
    """
    groups = match.groupdict()
    raw = groups['hash'] or groups['colon']
    is_project = raw is None
    pk = int(groups['pnum'] if is_project else raw)
    title = _project_label(pk) if is_project else _ticket_label(pk)
    if title is None:
        return None
    return _url_for(is_project, pk), title


def _text_token(content: str) -> Token:
    return Token('text', '', 0, content=content)


def _split_refs_in(content: str):
    """Split one text token's content into text + link tokens on bare refs.

    Returns ``None`` when there is nothing to convert (cheap signal for the
    caller); otherwise a fresh token list that replaces the single token.
    """
    if '#' not in content and ':' not in content:
        return None
    out = []
    last = 0
    for match in BARE_REF_RE.finditer(content):
        resolved = _resolve(match)
        if resolved is None:
            continue
        url, title = resolved
        if match.start() > last:
            out.append(_text_token(content[last : match.start()]))
        link_open = Token('link_open', 'a', 1, {'href': url, 'class': 'tokamak-ref'})
        link_open.attrs['title'] = title
        out += [
            link_open,
            _text_token(match.group(0)),
            Token('link_close', 'a', -1),
        ]
        last = match.end()
    if not out:
        return None
    if last < len(content):
        out.append(_text_token(content[last:]))
    return out


def _process_inline(inline_tok: Token) -> None:
    """Rewrite explicit refs and expand bare refs inside one inline token.

    Explicit ``[label](ticket:12)`` links always link (deliberate, even when
    the target is missing). Bare refs only become links when they resolve;
    text already inside a link is never re-processed so we cannot nest <a>
    tags.
    """
    out = []
    in_link = 0
    for child in inline_tok.children or []:
        if child.type == 'link_open':
            in_link += 1
            href = (child.attrs or {}).get('href', '')
            scheme = SCHEME_RE.match(href)
            if scheme:
                is_project = scheme.group(1) in ('project', 'proj')
                pk = int(scheme.group(2))
                child.attrs['href'] = _url_for(is_project, pk)
                child.attrs.setdefault('class', 'tokamak-ref')
            elif href.startswith(('http://', 'https://')):
                child.attrs.setdefault('target', '_blank')
                child.attrs.setdefault('rel', 'noopener nofollow external')
            out.append(child)
            continue
        if child.type == 'link_close':
            in_link -= 1
            out.append(child)
            continue
        if child.type == 'text' and in_link == 0 and (child.content or ''):
            pieces = _split_refs_in(child.content)
            if pieces:
                out.extend(pieces)
                continue
        out.append(child)
    inline_tok.children = out


def _walk_inline(tokens) -> None:
    for tok in tokens:
        if tok.type == 'inline' and tok.children:
            _process_inline(tok)
        elif tok.children:
            _walk_inline(tok.children)


def _refs_rule(state) -> None:
    _walk_inline(state.tokens)


# ---------------------------------------------------------------------------
# Renderer instance (configured once; render() is the only public entry)
# ---------------------------------------------------------------------------


def _build():
    # html=False is the safety anchor: raw HTML in user text is escaped,
    # never parsed (the commonmark preset alone would allow it!).
    md = MarkdownIt('commonmark', {'html': False}).use(gfm_plugin)
    # Run last so every core/inline rule (linkify, text_join, tables,
    # footnotes) has already produced its tokens.
    md.core.ruler.after('text_join', 'tokamak_refs', _refs_rule)
    return md


_MD = _build()


def render(text: str):
    """Render GFM + Tokamak refs into safe HTML (never mutates)."""
    if not text:
        return ''
    src = text[:MAX_INPUT_CHARS]
    return mark_safe(_MD.render(src))


# ---------------------------------------------------------------------------
# Plain-text excerpts (board cards, archive rows): never HTML, never safe.
# ---------------------------------------------------------------------------
_LINK_RE = re.compile(r'(!?)\[([^\]]*)\]\([^\)]*\)')
_REF_LINK_RE = re.compile(r'\[([^\]]*)\]\[[^\]]*\]')
_FENCE_BLOCK = re.compile(r'^(```|~~~)[^\n]*$[\s\S]*?^(?:```|~~~)', re.M)
_FENCE_LINE = re.compile(r'^(```|~~~)[^\n]*$', re.M)  # orphan fences
_TABLE_SEP = re.compile(r'^[ \t]*[-: |]+[ \t]*$', re.M)  # lines like |---|:--|
_CODE_SPANS = re.compile(r'`{1,3}[^`]+`{1,3}')
_EMPH_RE = re.compile(r'(?:\*\*|__|~~)(.+?)(?:\*\*|__|~~)', re.S)
_TASK_RE = re.compile(r'^([ \t]*(?:[-*+]|\d+\.)) ?\[([ xX])\]', re.M)
_QUOTE_RE = re.compile(r'^[ \t]*>[ ]?', re.M)
_HEADING_RE = re.compile(r'^[ \t]*#{1,6}[ 	]+')
_TABLE_PIPE = re.compile(r'\|')  # pipes -> spaces
_FOOTNOTE_REF = re.compile(r'\[\^[^\]]+\]')
_ALERT_MARK = re.compile(r'\[!(?:NOTE|TIP|WARNING|IMPORTANT|CAUTION)\]', re.I)
_DEF_LINE = re.compile(r'^\[[^\]]+\]:[^\n]+$')  # link definitions


def excerpt(text: str, limit: int = 140) -> str:
    """One-line plain-text digest of a markdown blob (for cards/rows).

    Strips the syntax rather than rendering it: links keep their label,
    code keeps its content, fences and footnote definitions vanish.
    """
    if not text:
        return ''
    src = text[:MAX_INPUT_CHARS]
    src = _FENCE_BLOCK.sub(' ', src)
    src = _FENCE_LINE.sub(' ', src)
    src = _TABLE_SEP.sub(' ', src)
    src = _DEF_LINE.sub(' ', src)
    src = _FOOTNOTE_REF.sub('', src)
    src = _CODE_SPANS.sub(lambda m: m.group(0).strip('`').strip(), src)
    src = _LINK_RE.sub(lambda m: m.group(2), src)
    src = _REF_LINK_RE.sub(lambda m: m.group(1), src)
    src = _TASK_RE.sub(r'\1 ', src)
    src = _EMPH_RE.sub(lambda m: m.group(1), src)
    src = _QUOTE_RE.sub('', src)
    src = _ALERT_MARK.sub('', src)
    src = _HEADING_RE.sub('', src)
    src = _TAG_RE.sub(' ', src)
    src = _TABLE_PIPE.sub(' ', src)
    src = _WS_RE.sub(' ', src).strip()
    if len(src) <= limit:
        return src
    cut = src[:limit]
    floor = max(cut.rfind(' '), int(limit * 0.6))
    end = cut[:floor].rstrip(' ,;:') if floor > 0 else cut.rstrip(' ,;:')
    return end + '…'
