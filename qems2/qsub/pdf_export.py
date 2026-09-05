"""Packetized PDF export (moderator-ready).

The PDF and the Word export are meant to be the same document in two formats,
so this file deliberately mirrors the docx writer in views.py: Times New Roman
12pt on half-inch margins, bold power regions, underlined answers, grey
sans-serif pronunciation guides, and a question that never breaks across a
page.

Type is set in the PDF core Times face rather than an embedded one, which is
what gives the two formats the same shape on the page; core fonts can only
carry cp1252, so any run with a character outside it (Greek, Cyrillic, longer
diacritics) is set in the bundled DejaVu Serif instead. That keeps the fallback
to the few words that need it rather than the whole packet.

Kept separate from the python-docx exporter in views.py; the export view calls
build_packetized_pdf() and streams the bytes.
"""

import os
from html.parser import HTMLParser

from fpdf import FPDF
from fpdf.enums import XPos, YPos

from .utils import get_formatted_question_html

_FONT_DIR = os.path.join(os.path.dirname(__file__), 'static', 'fonts')

#: The body face. "Times" is one of the PDF core fonts, so it needs no
#: embedding and a reader shows it in Times New Roman -- the same face, and the
#: same metrics, as the Word export asks for.
_SERIF = 'Times'
#: Pronunciation guides, matching the docx's sans-serif aside.
_SANS = 'Helvetica'
#: What a run falls back to when Times cannot encode it.
_UNICODE = 'DejaVuSerif'
_UNICODE_FILES = {
    '': 'DejaVuSerif.ttf',
    'B': 'DejaVuSerif-Bold.ttf',
    'I': 'DejaVuSerif-Italic.ttf',
    'BI': 'DejaVuSerif-BoldItalic.ttf',
}

#: Matching new_docx() in views.py.
SIZE = 12
LINE_H = 14          # Word's single spacing for 12pt Times
GAP = 10             # space_after on a question paragraph
MARGIN = 36          # half an inch
HEAD_SIZE = 13       # "Tossups" / "Bonuses"
TITLE_SIZE = 16      # the packet's own heading

BLACK = (0, 0, 0)
#: A parenthesised pronunciation guide: grey, and never bold, so it doesn't
#: compete with the clue text around it. Mirrors PRONUNCIATION_GUIDE_COLOR.
GUIDE_GREY = (0x80, 0x80, 0x80)
#: A note to the moderator or players: read out, but not a clue.
NOTE_GREY = (0x55, 0x55, 0x55)
#: The word a guide is about, matching the web view's .pg-target.
PG_TEAL = (0x0B, 0x72, 0x85)


def _safe(v):
    return '' if v is None else str(v)


def _encodable(text):
    try:
        text.encode('cp1252')
        return True
    except UnicodeEncodeError:
        return False


def _runs_by_font(text):
    """Split text into (chunk, needs_fallback) pieces.

    A single Greek word in a clue shouldn't drag the packet out of Times, so
    the split is per character-run rather than per field.
    """
    out = []
    buf = []
    cur = None
    for ch in text:
        need = not _encodable(ch)
        if cur is not None and need != cur:
            out.append((''.join(buf), cur))
            buf = []
        cur = need
        buf.append(ch)
    if buf:
        out.append((''.join(buf), cur))
    return out


def _write_text(pdf, text, family, style, size, color=BLACK, line_h=LINE_H):
    """One formatted run, in the body face where it can be and the fallback
    where it can't."""
    if not text:
        return
    pdf.set_text_color(*color)
    for chunk, fallback in _runs_by_font(text):
        pdf.set_font(_UNICODE if fallback else family, style, size)
        pdf.write(line_h, chunk)
    pdf.set_text_color(*BLACK)


class _RunWriter(HTMLParser):
    """Walk the HTML from get_formatted_question_html and write each text run
    with the emphasis, colour and face that wrap it.

    The formatter marks a pronunciation guide and a note with a class rather
    than a tag of their own (`<strong class="pronunciation-guide">`), so the
    walk has to read classes: a guide inside a bolded power region is still an
    aside, and printing it bold would make it read as part of the clue.
    """

    #: tags whose nesting this cares about
    _TRACKED = {'b': 'b', 'strong': 'b', 'i': 'i', 'em': 'i', 'u': 'u', 'span': None}

    def __init__(self, pdf, size=SIZE):
        super().__init__(convert_charrefs=True)
        self.pdf = pdf
        self.size = size
        # (tag, kind) for every open tag, so the right one is closed even when
        # a guide is nested inside a power region.
        self.stack = []

    @staticmethod
    def _classes(attrs):
        for name, value in attrs:
            if name == 'class':
                return set((value or '').split())
        return set()

    def handle_starttag(self, tag, attrs):
        if tag not in self._TRACKED:
            return
        classes = self._classes(attrs)
        if 'pronunciation-guide' in classes:
            kind = 'guide'
        elif 'q-note' in classes:
            kind = 'note'
        elif 'pg-target' in classes:
            kind = 'pg'
        else:
            kind = self._TRACKED[tag]
        self.stack.append((tag, kind))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i]
                return

    def handle_data(self, data):
        if not data:
            return
        kinds = [k for _, k in self.stack]
        guide = 'guide' in kinds
        note = 'note' in kinds
        # A guide and a note are asides: never bold, whatever encloses them.
        bold = ('b' in kinds) and not (guide or note)
        italic = ('i' in kinds) or note
        style = ('B' if bold else '') + ('I' if italic else '') + ('U' if 'u' in kinds else '')
        # Order matters, and matches the Word export: a guide wins over
        # everything, and a guide's target word keeps its tint even inside a
        # note, since that is the word the guide is about.
        if guide:
            family, color = _SANS, GUIDE_GREY
        elif 'pg' in kinds:
            family, color = _SERIF, PG_TEAL
        elif note:
            family, color = _SERIF, NOTE_GREY
        else:
            family, color = _SERIF, BLACK
        _write_text(self.pdf, data, family, style, self.size, color)


def _write_qems(pdf, text, is_answer=False, size=SIZE, all_power=False, allow_superpower=True,
                quoted_guides=False):
    """Render one QEMS-markup field as inline formatted runs on the PDF."""
    html_text = get_formatted_question_html(
        _safe(text), True, True, False, not is_answer,
        allPower=all_power, allowSuperpower=allow_superpower,
        guidesRequireQuotes=quoted_guides)
    _RunWriter(pdf, size).feed(html_text)


def _meta_line(q, opts):
    """`<Author, Category> [Tags] ~id~ <Editor: Name>` with each piece optional.

    Category tags come from ``opts['tag_names']``, keyed ('tossup'|'bonus', id)
    -- the caller has them in one query, and an empty (or missing) map is how a
    set that keeps its tags out of its packets says so."""
    author = _credited_name(q) if opts['writers'] else ''
    cat = str(q.category).strip() if q.category else ''
    if author and cat:
        head = '<{0}, {1}>'.format(author, cat)
    elif author:
        head = '<{0}>'.format(author)
    elif cat:
        head = '<{0}>'.format(cat)
    else:
        head = ''
    parts = [head]
    tags = (opts.get('tag_names') or {}).get(
        (q.__class__.__name__.lower(), q.id), [])
    if tags:
        parts.append('[{0}]'.format(', '.join(tags)))
    if opts['ids']:
        parts.append('~{0}~'.format(q.id))
    if opts['editors'] and getattr(q, 'edited', False) and q.editor:
        ename = _real_name(q.editor)
        if ename:
            parts.append('<Editor: {0}>'.format(ename))
    return ' '.join(p for p in parts if p).strip()


def _credited_name(q):
    """Who the question is credited to: a freeform name when it carries one,
    otherwise the account that owns it."""
    getter = getattr(q, 'author_real_name', None)
    if callable(getter):
        import html as _html
        return _html.unescape(getter() or '').strip()
    return _real_name(q.author)


def _real_name(writer):
    if writer is None:
        return ''
    try:
        name = (writer.get_real_name() or '').strip()
    except Exception:
        name = ''
    # get_real_name may return HTML-escaped entities; decode common ones.
    import html as _html
    return _html.unescape(name)


def _write_meta(pdf, meta):
    """The attribution line, set exactly as the rest of the question is -- the
    Word export gives it no styling of its own, and the two are meant to be the
    same document."""
    if not meta:
        return
    pdf.ln(LINE_H)
    _write_text(pdf, meta, _SERIF, '', SIZE)


def _quoted_guides(question):
    """Whether this question's set only treats a quoted parenthetical as a
    pronunciation guide (so the rest print as ordinary text)."""
    getter = getattr(question, 'guides_require_quotes', None)
    try:
        return bool(getter()) if callable(getter) else False
    except Exception:
        return False


def _heading(pdf, text, size, space_before=0, space_after=0, center=True):
    if space_before:
        pdf.ln(space_before)
    pdf.set_text_color(*BLACK)
    pdf.set_font(_SERIF if _encodable(text) else _UNICODE, 'B', size)
    pdf.cell(0, size + 2, text, align='C' if center else 'L',
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    if space_after:
        pdf.ln(space_after)


def _tossup(pdf, tossup, num, opts):
    quoted = _quoted_guides(tossup)
    _write_text(pdf, '{0}. '.format(num), _SERIF, 'B', SIZE)
    _write_qems(pdf, tossup.tossup_text,
                all_power=tossup.is_all_power(),
                allow_superpower=tossup.superpower_enabled(),
                quoted_guides=quoted)
    pdf.ln(LINE_H)
    # Not bolded, matching the Word export: the label is scaffolding, and
    # bolding it drew the eye away from the underlined answer beside it.
    _write_text(pdf, 'ANSWER: ', _SERIF, '', SIZE)
    _write_qems(pdf, tossup.tossup_answer, is_answer=True, quoted_guides=quoted)
    _write_meta(pdf, _meta_line(tossup, opts))
    pdf.ln(LINE_H + GAP)


def _bonus(pdf, bonus, num, opts):
    quoted = _quoted_guides(bonus)
    _write_text(pdf, '{0}. '.format(num), _SERIF, 'B', SIZE)
    _write_qems(pdf, bonus.leadin, quoted_guides=quoted)
    for i in range(1, 4):
        text = getattr(bonus, 'part{0}_text'.format(i), '') or ''
        if not text.strip():
            continue
        answer = getattr(bonus, 'part{0}_answer'.format(i), '') or ''
        diff = getattr(bonus, 'part{0}_difficulty'.format(i), '') or ''
        pdf.ln(LINE_H)
        _write_text(pdf, '[10{0}] '.format(diff), _SERIF, 'B', SIZE)
        _write_qems(pdf, text, quoted_guides=quoted)
        pdf.ln(LINE_H)
        _write_text(pdf, 'ANSWER: ', _SERIF, '', SIZE)
        _write_qems(pdf, answer, is_answer=True, quoted_guides=quoted)
    _write_meta(pdf, _meta_line(bonus, opts))
    pdf.ln(LINE_H + GAP)


def _atomic(pdf, render):
    """Draw something, moving it to the next page rather than letting it split.

    Word keeps each question whole with keep_together; fpdf has no such thing,
    so the block is drawn once into a throwaway copy to see whether it would
    break, and the page is turned first if it would. Something too long to fit
    a page of its own still has to break somewhere, so this only turns the page
    when there is anything above it to move away from.
    """
    with pdf.offset_rendering() as dummy:
        render(dummy)
    if dummy.page_break_triggered and pdf.get_y() > pdf.t_margin:
        pdf.add_page()
    render(pdf)


def _numbered(questions, start=1):
    """(question, printed number) pairs -- a question keeps its own number, and
    only one that has none falls back to its place in the list."""
    return [(q, q.question_number or n)
            for n, q in enumerate(questions, start)]


def _section(pdf, label, items, draw, opts):
    """A run of (question, number) pairs, under a heading if there is one.

    The heading is measured together with the first question, so it can't be
    stranded at the foot of a page with nothing under it.
    """
    for i, (q, num) in enumerate(items):
        head = label if i == 0 else None

        def render(target, q=q, num=num, head=head):
            if head:
                _heading(target, head, HEAD_SIZE, space_before=12, space_after=6)
            draw(target, q, num, opts)

        _atomic(pdf, render)


def _credits(pdf, writer_names, editor_names):
    if not writer_names and not editor_names:
        return
    _heading(pdf, 'Credits', HEAD_SIZE, space_before=6, space_after=4)
    for label, names in (('Writers', writer_names), ('Editors', editor_names)):
        if not names:
            continue
        _write_text(pdf, '{0}: '.format(label), _SERIF, 'B', SIZE)
        _write_text(pdf, ', '.join(names), _SERIF, '', SIZE)
        pdf.ln(LINE_H)
    pdf.ln(GAP)


def build_packetized_pdf(set_name, groups, opts, credits=None):
    """Build a packetized PDF.

    `groups` is an ordered list of (packet_name, tossups, bonuses). `opts` is a
    dict with boolean writers/editors/ids/credits, plus `interlace` to print
    tossup 1, bonus 1, tossup 2, … (reading order) instead of all tossups then
    all bonuses. `credits` is an optional (writer_names, editor_names) tuple
    shown before the first packet's tossups. Returns PDF bytes.

    The set export calls this once per packet with a single group, so each
    packet is its own file (and carries the credits, since it travels alone);
    passing several groups still produces one combined document.
    """
    pdf = FPDF(unit='pt', format='letter')
    # Curly quotes and dashes are cp1252, not latin-1, and a packet is full of
    # them; without this every one of them would take the fallback face.
    pdf.core_fonts_encoding = 'cp1252'
    pdf.set_margins(MARGIN, MARGIN, MARGIN)
    pdf.set_auto_page_break(True, margin=MARGIN)
    for style, fname in _UNICODE_FILES.items():
        pdf.add_font(_UNICODE, style, os.path.join(_FONT_DIR, fname))

    for gi, (packet_name, tossups, bonuses) in enumerate(groups):
        pdf.add_page()
        _heading(pdf, '{0} {1}'.format(set_name or '', packet_name).strip(),
                 TITLE_SIZE, space_after=10)
        if gi == 0 and opts.get('credits') and credits:
            _credits(pdf, credits[0], credits[1])
        if opts.get('interlace'):
            tus, bss = _numbered(tossups), _numbered(bonuses)
            label = 'Questions' if (tus or bss) else None
            for i in range(max(len(tus), len(bss))):
                if i < len(tus):
                    _section(pdf, label, [tus[i]], _tossup, opts)
                    label = None
                if i < len(bss):
                    _section(pdf, label, [bss[i]], _bonus, opts)
                    label = None
        else:
            # A "Tossups" heading over a packet that has only tossups says
            # there are bonuses somewhere, which there aren't; the headings are
            # only worth printing when they separate one kind from the other.
            both = bool(tossups) and bool(bonuses)
            _section(pdf, 'Tossups' if both else None, _numbered(tossups), _tossup, opts)
            _section(pdf, 'Bonuses' if both else None, _numbered(bonuses), _bonus, opts)

    out = pdf.output()
    return bytes(out)
