"""Packetized PDF export (moderator-ready). Renders each packet on its own
page(s) with QEMS formatting (bold power regions, underlined answers, italics)
via fpdf2 and a bundled DejaVu Serif Unicode font, so diacritics, Greek,
Cyrillic, dashes and quotes all render.

Kept separate from the python-docx exporter in views.py; the export view calls
build_packetized_pdf() and streams the bytes.
"""

import os
import re
from html.parser import HTMLParser

from fpdf import FPDF
from fpdf.enums import XPos, YPos

from .utils import get_formatted_question_html

_FONT_DIR = os.path.join(os.path.dirname(__file__), 'static', 'fonts')
_FONT = 'DejaVuSerif'
_FONT_FILES = {
    '': 'DejaVuSerif.ttf',
    'B': 'DejaVuSerif-Bold.ttf',
    'I': 'DejaVuSerif-Italic.ttf',
    'BI': 'DejaVuSerif-BoldItalic.ttf',
}

SIZE = 11
LINE_H = 14
GAP = 8


def _safe(v):
    return '' if v is None else str(v)


class _RunWriter(HTMLParser):
    """Walk the HTML from get_formatted_question_html and write each text run to
    the PDF with the bold/italic/underline state that wraps it."""

    def __init__(self, pdf, size=SIZE):
        super().__init__(convert_charrefs=True)
        self.pdf = pdf
        self.size = size
        self.b = self.i = self.u = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('b', 'strong'):
            self.b += 1
        elif tag in ('i', 'em'):
            self.i += 1
        elif tag == 'u':
            self.u += 1

    def handle_endtag(self, tag):
        if tag in ('b', 'strong'):
            self.b = max(0, self.b - 1)
        elif tag in ('i', 'em'):
            self.i = max(0, self.i - 1)
        elif tag == 'u':
            self.u = max(0, self.u - 1)

    def handle_data(self, data):
        if not data:
            return
        style = ('B' if self.b else '') + ('I' if self.i else '') + ('U' if self.u else '')
        self.pdf.set_font(_FONT, style, self.size)
        self.pdf.write(LINE_H, data)


def _write_qems(pdf, text, is_answer=False, size=SIZE):
    """Render one QEMS-markup field as inline formatted runs on the PDF."""
    html_text = get_formatted_question_html(
        _safe(text), True, True, False, not is_answer)
    _RunWriter(pdf, size).feed(html_text)


def _meta_line(q, opts):
    """`<Author, Category> ~id~ <Editor: Name>` with each piece optional."""
    author = _real_name(q.author) if opts['writers'] else ''
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
    if opts['ids']:
        parts.append('~{0}~'.format(q.id))
    if opts['editors'] and getattr(q, 'edited', False) and q.editor:
        ename = _real_name(q.editor)
        if ename:
            parts.append('<Editor: {0}>'.format(ename))
    return ' '.join(p for p in parts if p).strip()


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
    if not meta:
        return
    pdf.ln(LINE_H)
    pdf.set_font(_FONT, 'I', 9)
    pdf.set_text_color(90, 90, 90)
    pdf.write(11, meta)
    pdf.set_text_color(0, 0, 0)


def _tossup(pdf, tossup, num, opts):
    pdf.set_font(_FONT, 'B', SIZE)
    pdf.write(LINE_H, '{0}. '.format(num))
    _write_qems(pdf, tossup.tossup_text)
    pdf.ln(LINE_H)
    pdf.set_font(_FONT, 'B', SIZE)
    pdf.write(LINE_H, 'ANSWER: ')
    _write_qems(pdf, tossup.tossup_answer, is_answer=True)
    _write_meta(pdf, _meta_line(tossup, opts))
    pdf.ln(LINE_H + GAP)


def _bonus(pdf, bonus, num, opts):
    pdf.set_font(_FONT, 'B', SIZE)
    pdf.write(LINE_H, '{0}. '.format(num))
    _write_qems(pdf, bonus.leadin)
    for i in range(1, 4):
        text = getattr(bonus, 'part{0}_text'.format(i), '') or ''
        if not text.strip():
            continue
        answer = getattr(bonus, 'part{0}_answer'.format(i), '') or ''
        diff = getattr(bonus, 'part{0}_difficulty'.format(i), '') or ''
        pdf.ln(LINE_H)
        pdf.set_font(_FONT, 'B', SIZE)
        pdf.write(LINE_H, '[10{0}] '.format(diff))
        _write_qems(pdf, text)
        pdf.ln(LINE_H)
        pdf.set_font(_FONT, 'B', SIZE)
        pdf.write(LINE_H, 'ANSWER: ')
        _write_qems(pdf, answer, is_answer=True)
    _write_meta(pdf, _meta_line(bonus, opts))
    pdf.ln(LINE_H + GAP)


def _heading(pdf, text, size, gap_before=0, center=False):
    if gap_before:
        pdf.ln(gap_before)
    pdf.set_font(_FONT, 'B', size)
    pdf.set_text_color(0, 0, 0)
    pdf.cell(0, size + 4, text, align='C' if center else 'L',
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def _credits(pdf, writer_names, editor_names):
    if not writer_names and not editor_names:
        return
    _heading(pdf, 'Credits', 13, gap_before=6)
    for label, names in (('Writers', writer_names), ('Editors', editor_names)):
        if not names:
            continue
        pdf.set_font(_FONT, 'B', SIZE)
        pdf.write(LINE_H, '{0}: '.format(label))
        pdf.set_font(_FONT, '', SIZE)
        pdf.write(LINE_H, ', '.join(names))
        pdf.ln(LINE_H)
    pdf.ln(GAP)


def build_packetized_pdf(set_name, groups, opts, credits=None):
    """Build a packetized PDF.

    `groups` is an ordered list of (packet_name, tossups, bonuses). `opts` is a
    dict with boolean writers/editors/ids/credits. `credits` is an optional
    (writer_names, editor_names) tuple shown before the first packet's tossups.
    Returns PDF bytes.
    """
    pdf = FPDF(unit='pt', format='letter')
    pdf.set_margins(40, 40, 40)
    pdf.set_auto_page_break(True, margin=40)
    for style, fname in _FONT_FILES.items():
        pdf.add_font(_FONT, style, os.path.join(_FONT_DIR, fname))

    for gi, (packet_name, tossups, bonuses) in enumerate(groups):
        pdf.add_page()
        _heading(pdf, '{0} {1}'.format(set_name or '', packet_name).strip(), 16, center=True)
        pdf.ln(4)
        if gi == 0 and opts.get('credits') and credits:
            _credits(pdf, credits[0], credits[1])
        if tossups:
            _heading(pdf, 'Tossups', 13, gap_before=4, center=True)
            pdf.ln(2)
            for i, t in enumerate(tossups, 1):
                _tossup(pdf, t, t.question_number or i, opts)
        if bonuses:
            _heading(pdf, 'Bonuses', 13, gap_before=6, center=True)
            pdf.ln(2)
            for i, b in enumerate(bonuses, 1):
                _bonus(pdf, b, b.question_number or i, opts)

    out = pdf.output()
    return bytes(out)
