"""Tests for tokamak.markdown: GFM rendering + ticket/project references."""

import pytest
from django.utils import http as django_http  # noqa: F401  (sanity import)

from tests._utils import make_project
from tokamak import markdown as mdw
from tokamak.models import Ticket


def make_ticket(project, name='Fix the thing', description=''):
    return Ticket.objects.create(project=project, name=name, description=description)


# ---------------------------------------------------------------------------
# GFM surface area
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_render_inline_markup():
    out = mdw.render('**bold**, _emphasis_, ~~gone~~, `code`')
    assert '<strong>bold</strong>' in out
    assert '<em>emphasis</em>' in out
    assert '<s>gone</s>' in out
    assert '<code>code</code>' in out


@pytest.mark.django_db
def test_render_tables_and_tasks():
    out = mdw.render('| a | b |\n|---|---|\n| 1 | 2 |\n\n' '- [x] done\n- [ ] todo\n')
    assert '<table>' in out
    assert 'task-list-item' in out
    assert 'checked' in out


@pytest.mark.django_db
def test_render_fenced_code_and_footnotes():
    out = mdw.render('```python\nx = 1\n```\n\ntext[^f].\n\n[^f]: note body\n')
    assert '<code class="language-python">' in out
    assert 'footnote-ref' in out
    assert 'note body' in out


@pytest.mark.django_db
def test_render_github_alerts():
    out = mdw.render('> [!NOTE]\n> Heads up.')
    assert 'markdown-alert-note' in out
    assert 'Heads up.' in out


@pytest.mark.django_db
def test_autolinks_open_external_safely():
    out = mdw.render('visit www.example.com or https://example.dev')
    assert '<a href="http://www.example.com"' in out
    assert 'rel="noopener nofollow external"' in out
    assert 'target="_blank"' in out


# ---------------------------------------------------------------------------
# Safety: html=False + link validation
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_raw_html_is_escaped_not_parsed():
    out = mdw.render('<script>alert(1)</script>')
    assert '<script>' not in out
    assert '&lt;script&gt;' in out


@pytest.mark.django_db
def test_dangerous_link_schemes_blocked():
    out = mdw.render('[x](javascript:alert(1)) [y](data:text/html,hack)')
    assert '<a ' not in out  # never becomes a link
    assert '[x](javascript:alert(1))' in out  # stays literal text


@pytest.mark.django_db
def test_render_clamps_huge_input():
    out = mdw.render('a' * (mdw.MAX_INPUT_CHARS + 5_000))
    # clamped input renders far fewer chars than the whole blob
    assert len(out) < mdw.MAX_INPUT_CHARS + 100
    assert 'aaaa' in out


# ---------------------------------------------------------------------------
# References: bare refs follow the existence rule, explicit always link
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_bare_ticket_ref_links_when_target_exists():
    project = make_project()
    ticket = make_ticket(project, name='Login bug')
    out = mdw.render(f'see #{ticket.pk} for details')
    assert f'href="/tickets/{ticket.pk}/"' in out
    assert '#%s · Login bug' % ticket.pk in out  # title attribute


@pytest.mark.django_db
def test_bare_ref_missing_stays_text():
    out = mdw.render('see #987654 someday')
    assert '<a' not in out
    assert '#987654' in out


@pytest.mark.django_db
def test_bare_project_ref_and_aliases():
    project = make_project(name='Relaunch')
    ticket = make_ticket(project)
    out = mdw.render(
        f'project:{project.pk} and proj:{project.pk} '
        f'and ticket:{ticket.pk} and tkt:{ticket.pk}'
    )
    assert out.count('href="/projects/') == 2
    assert out.count('href="/tickets/') == 2


@pytest.mark.django_db
def test_explicit_ref_links_even_when_target_missing():
    out = mdw.render('[gone ticket](ticket:4242)')
    assert 'href="/tickets/4242/"' in out


@pytest.mark.django_db
def test_explicit_internal_markdown_link_untouched():
    out = mdw.render('[Board](/projects/3/)')
    assert 'href="/projects/3/"' in out
    assert 'target=' not in out  # internal stays same-tab


@pytest.mark.django_db
def test_glued_or_nonnumeric_hash_stays_text():
    project = make_project()
    ticket = make_ticket(project)
    out = mdw.render(f'#{ticket.pk}abc color #f5f5f5 mailto#12 trailing x#{ticket.pk}')
    # only none of them link: glued, non-numeric hex, or no delimiter before
    assert '<a ' not in out


@pytest.mark.django_db
def test_refs_inside_links_and_code_not_nested():
    project = make_project()
    ticket = make_ticket(project)
    out = mdw.render(f'[outer #{ticket.pk} text](http://go.dev) and `#{ticket.pk}`')
    # exactly one anchor (the outer autolink-less markdown link), no nesting
    assert out.count('<a ') == 1
    assert 'code #' in out or '<code>' in out


@pytest.mark.django_db
def test_render_never_mutates():
    project = make_project()
    ticket = make_ticket(project)
    before = Ticket.objects.count()
    mdw.render(f'look at #{ticket.pk} and project:{project.pk}')
    assert Ticket.objects.count() == before


# ---------------------------------------------------------------------------
# Excerpts (plain text, never safe)
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_excerpt_strips_syntax():
    src = (
        '# Head\n**bold** [label](ticket:1) `x=y`\n```py\nfence body\n```\n'
        '- [ ] task one\n> quote\n| a | b |\n|---|---|\n| 1 | 2 |\n'
    )
    out = mdw.excerpt(src)
    assert not out.startswith('#')
    assert '**' not in out
    assert 'fence body' not in out
    assert '|---|' not in out
    assert '[label]' not in out
    assert 'label' in out
    assert '<' not in out


@pytest.mark.django_db
def test_excerpt_truncates_on_word_boundary():
    src = 'word ' * 60
    out = mdw.excerpt(src, limit=40)
    assert len(out) <= 41  # ellipsis allowed to extend by one
    assert out.endswith('…')
    assert not out.rstrip('…').endswith(' ')


@pytest.mark.django_db
def test_excerpt_empty_and_short():
    assert mdw.excerpt('') == ''
    assert mdw.excerpt('Short.') == 'Short.'
