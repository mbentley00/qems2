"""Packetized PDF export (moderator-ready).

The PDF and the Word export are meant to be the same document in two formats,
so this file deliberately mirrors the docx writer in views.py: Times New Roman
12pt on half-inch margins, bold power regions, underlined answers, grey
sans-serif pronunciation guides, and a question that never breaks across a
page.

Type is set in the bundled Liberation Serif, which is drawn to Times New
Roman's metrics -- the same shape on the page as the Word export -- and,
unlike the PDF core Times, carries real glyphs for ř, ō, Greek and Cyrillic.
Guides are in Liberation Sans (Helvetica's metrics). A character the face
lacks falls back, for that character alone, to DejaVu Serif and then to Noto
Serif JP (kanji and kana; converted to TrueType outlines, since fpdf draws a
CFF font's glyphs blank).

Line breaking is done here rather than by fpdf: a paragraph's runs (bold, a
guide's teal target word, a fallback character) are gathered first and broken
only at spaces. fpdf's own write() wraps one run at a time, and when the first
word of a run didn't fit the rest of a line it split that word letter by
letter ("Ferrucci / o Busoni").

Kept separate from the python-docx exporter in views.py; the export view calls
build_packetized_pdf() and streams the bytes.
"""

import copy
import os
import re
import unicodedata
from html.parser import HTMLParser

from fpdf import FPDF
from fpdf.enums import XPos, YPos

from .utils import get_formatted_question_html

_FONT_DIR = os.path.join(os.path.dirname(__file__), 'static', 'fonts')

#: The body face: Times New Roman's metrics, so the PDF has the Word export's
#: line breaks and page count.
_SERIF = 'LiberationSerif'
#: Pronunciation guides, matching the docx's sans-serif aside.
_SANS = 'LiberationSans'
#: What a character falls back to when the face it is in has no glyph for it,
#: in order.
_UNICODE = 'DejaVuSerif'
_CJK = 'NotoSerifJP'
_FALLBACKS = (_UNICODE, _CJK)
_FONT_FILES = {
    _SERIF: {
        '': 'LiberationSerif-Regular.ttf',
        'B': 'LiberationSerif-Bold.ttf',
        'I': 'LiberationSerif-Italic.ttf',
        'BI': 'LiberationSerif-BoldItalic.ttf',
    },
    _SANS: {
        '': 'LiberationSans-Regular.ttf',
        'B': 'LiberationSans-Bold.ttf',
        'I': 'LiberationSans-Italic.ttf',
        'BI': 'LiberationSans-BoldItalic.ttf',
    },
    _UNICODE: {
        '': 'DejaVuSerif.ttf',
        'B': 'DejaVuSerif-Bold.ttf',
        'I': 'DejaVuSerif-Italic.ttf',
        'BI': 'DejaVuSerif-BoldItalic.ttf',
    },
    # One weight only: the font is large, and a packet's Japanese is a name
    # or a title, not a bolded clue.
    _CJK: dict.fromkeys(('', 'B', 'I', 'BI'), 'NotoSerifJP-Regular.ttf'),
}

_FAMILIES = {name.lower(): name for name in _FONT_FILES}

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

#: Characters that print nothing and would only confuse the layout: a soft
#: hyphen, zero-width spaces and joiners, a byte-order mark.
_INVISIBLE = dict.fromkeys(map(ord, '­​‌‍⁠﻿'))
#: Where a line may break. Deliberately not \s, which would also break at a
#: no-break space.
_BREAKS = re.compile(r'([ \t\r\n]+)')
#: Slack for float rounding when deciding whether a word fits.
_EPS = 0.01


def _safe(v):
    return '' if v is None else str(v)


_cmaps = {}


def _cmap(family):
    """The code points every style of `family` has a glyph for."""
    cmap = _cmaps.get(family)
    if cmap is None:
        from fontTools.ttLib import TTFont
        sets = []
        for fname in set(_FONT_FILES[family].values()):
            font = TTFont(os.path.join(_FONT_DIR, fname), lazy=True)
            try:
                sets.append(set(font.getBestCmap()))
            finally:
                font.close()
        cmap = _cmaps[family] = frozenset.intersection(*map(frozenset, sets))
    return cmap


def _covers(family, text):
    cmap = _cmap(family)
    return all(ord(ch) in cmap or ch in ' \t\r\n' for ch in text)


def _face_for(ch, family):
    """`family` if it has a glyph for ch, else the first fallback that does
    (and `family` again if none does: a blank box beats a crash)."""
    if ord(ch) in _cmap(family) or ch in ' \t\r\n':
        return family
    for fallback in _FALLBACKS:
        if ord(ch) in _cmap(fallback):
            return fallback
    return family


def _runs_by_font(text, family):
    """Split text into (chunk, family) pieces: `family` wherever it has the
    glyph, a fallback face where only that does.

    A single Greek word in a clue shouldn't drag the packet out of the body
    face, so the split is per character-run rather than per field.
    """
    if _covers(family, text):
        return [(text, family)]
    out = []
    buf = []
    cur = None
    for ch in text:
        fam = _face_for(ch, family)
        if cur is not None and fam != cur:
            out.append((''.join(buf), cur))
            buf = []
        cur = fam
        buf.append(ch)
    if buf:
        out.append((''.join(buf), cur))
    return out


#: (family, style) -> a TTFFont parsed once per process, which each document
#: gets its own copy of (see _add_font).
_parsed = {}


def _add_font(pdf, family, style, path):
    """pdf.add_font, without parsing the file again for every document.

    A set export builds a PDF per packet plus a scratch document per fifty
    questions, and fpdf's add_font reads every glyph of the file each time --
    with six faces that was most of the export's run time. The parsed font is
    kept, and each document gets a copy with its own subset (which glyphs it
    used) and, if it will be written out, its own handle on the file: writing
    a PDF cuts the font down to that subset in place.
    """
    try:
        from fontTools.ttLib import TTFont
        from fpdf.fonts import SubsetMap
        template = _template(family, style)
        # TTFFont.__deepcopy__ shares the parsed tables and copies the rest.
        font = copy.deepcopy(template)
        font.i = len(pdf.fonts) + 1
        font.subset = SubsetMap(font)
        font.missing_glyphs = []
        font.biggest_size_pt = 0
        if not getattr(pdf, '_qems_scratch', False):
            font.ttfont = TTFont(path, recalcTimestamp=False, lazy=True)
        pdf.fonts[font.fontkey] = font
    except Exception:
        # fpdf's internals moved under us: parse it the slow way.
        pdf.fonts.pop('{0}{1}'.format(family.lower(), style), None)
        pdf.add_font(family, style, path)


def _set_font(pdf, family, style, size):
    """set_font, registering a bundled face the first time a run needs it.

    Most packets never need the fallbacks, and an unused face shouldn't be
    embedded."""
    # fpdf reports a family back lowercased (pdf.font_family).
    family = _FAMILIES.get(family.lower(), family)
    if family in _FONT_FILES and hasattr(pdf, 'add_font'):
        loaded = getattr(pdf, '_qems_fonts', None)
        if loaded is None:
            loaded = pdf._qems_fonts = set()
        if family not in loaded:
            for st, fname in _FONT_FILES[family].items():
                _add_font(pdf, family, st, os.path.join(_FONT_DIR, fname))
            loaded.add(family)
    pdf.set_font(family, style, size)


def _write_text(pdf, text, family, style, size, color=BLACK, line_h=LINE_H):
    """Add one formatted run to the paragraph being built on `pdf`.

    Nothing is drawn until the paragraph ends (_ln), because where a line can
    break depends on the runs after this one: a word can carry on into the
    next run with no space between them."""
    if not text:
        return
    text = unicodedata.normalize('NFC', text).translate(_INVISIBLE)
    if not text:
        return
    buf = getattr(pdf, '_qems_buf', None)
    if buf is None:
        buf = pdf._qems_buf = []
    buf.append((text, family, style, size, color, line_h))


def _ln(pdf, h):
    """End the paragraph being built, then move down `h`."""
    _flush(pdf)
    pdf.ln(h)


def _template(family, style):
    """The once-parsed TTFFont for a bundled face (see _add_font)."""
    style = style.replace('U', '')
    font = _parsed.get((family, style))
    if font is None:
        scratch = FPDF(unit='pt')
        scratch.add_font(family, style, os.path.join(_FONT_DIR, _FONT_FILES[family][style]))
        font = _parsed[(family, style)] = scratch.fonts[
            '{0}{1}'.format(family.lower(), style)]
    return font


def _width(piece):
    """A piece's width in points: what fpdf's get_string_width gives (the
    glyphs' advance widths, at no extra spacing), read straight from the font.
    get_string_width builds fpdf's whole styled-fragment machinery for each
    call, and the layout measures every word."""
    text, family, style, size, _color = piece
    cw = _template(family, style).cw
    return sum(cw[ord(ch)] for ch in text) * size * 0.001


def _flush(target):
    """Lay out and draw the runs gathered since the paragraph began."""
    buf = getattr(target, '_qems_buf', None)
    if not buf:
        return
    target._qems_buf = []
    pieces = []
    for text, family, style, size, color, _h in buf:
        for chunk, fam in _runs_by_font(text, family):
            pieces.append((chunk, fam, style, size, color))
    line_h = max(entry[5] for entry in buf)
    widths = {}

    def width_of(piece):
        w = widths.get(piece)
        if w is None:
            w = widths[piece] = _width(piece)
        return w

    if isinstance(target, _Bound):
        indent, full = target.x, target.width
    else:
        indent, full = target.get_x() - target.l_margin, target.epw
    lines = _layout(pieces, width_of, full - indent, full)
    if isinstance(target, _Bound):
        target.add_lines(lines, line_h, width_of)
    else:
        _draw(target, lines, line_h, width_of)


def _tokens(pieces):
    """('word' | 'space' | 'newline', [piece, ...]) in order. A word's pieces
    are every run it touches, so a word half in one style and half in another
    (or with one fallback letter) stays one word."""
    out = []
    for text, *rest in pieces:
        for seg in _BREAKS.split(text):
            if not seg:
                continue
            if _BREAKS.fullmatch(seg):
                if '\n' in seg:
                    out.extend(('newline', []) for _ in range(seg.count('\n')))
                elif out and out[-1][0] == 'space':
                    out[-1][1].append((seg, *rest))
                else:
                    out.append(('space', [(seg, *rest)]))
            elif out and out[-1][0] == 'word':
                out[-1][1].append((seg, *rest))
            else:
                out.append(('word', [(seg, *rest)]))
    return out


def _layout(pieces, width_of, first, full):
    """Break a paragraph's pieces into lines, at spaces only.

    `first` is the room left on the line the paragraph starts on, `full` a
    whole line's width. A word too wide for a line of its own is the one thing
    split between letters. Returns a list of lines, each a list of pieces; the
    spaces a line breaks at are dropped.
    """
    lines = [[]]
    state = {'used': 0.0, 'avail': first}
    pending, pending_w = [], 0.0

    def new_line():
        lines.append([])
        state['used'], state['avail'] = 0.0, full

    for kind, parts in _tokens(pieces):
        if kind == 'newline':
            new_line()
            pending, pending_w = [], 0.0
            continue
        if kind == 'space':
            if lines[-1]:   # a space starting a line isn't printed
                pending.extend(parts)
                pending_w += sum(width_of(p) for p in parts)
            continue
        w = sum(width_of(p) for p in parts)
        if lines[-1] and state['used'] + pending_w + w > state['avail'] + _EPS:
            new_line()
        elif (not lines[-1] and w > state['avail'] + _EPS
              and state['avail'] < full - _EPS):
            # Partway along a line the word doesn't fit on: start a new one.
            new_line()
        if lines[-1]:
            lines[-1].extend(pending)
            state['used'] += pending_w
        pending, pending_w = [], 0.0
        if w <= state['avail'] + _EPS:
            lines[-1].extend(parts)
            state['used'] += w
            continue
        # Wider than a whole line: break it wherever the line runs out.
        for text, *rest in parts:
            chunk = ''
            for ch in text:
                cw = width_of((ch, *rest))
                if (chunk or lines[-1]) and state['used'] + cw > state['avail'] + _EPS:
                    if chunk:
                        lines[-1].append((chunk, *rest))
                    chunk = ''
                    new_line()
                chunk += ch
                state['used'] += cw
            if chunk:
                lines[-1].append((chunk, *rest))
    while len(lines) > 1 and not lines[-1]:
        lines.pop()
    return lines


def _merged(line):
    """A line's pieces with neighbours of the same face and colour joined, so
    an underline or a word isn't drawn in slivers."""
    out = []
    for piece in line:
        if out and out[-1][1:] == piece[1:]:
            out[-1] = (out[-1][0] + piece[0],) + piece[1:]
        else:
            out.append(piece)
    return out


def _draw(pdf, lines, h, width_of):
    for i, line in enumerate(lines):
        if i:
            pdf.ln(h)
        for piece in _merged(line):
            text, family, style, size, color = piece
            _set_font(pdf, family, style, size)
            pdf.set_text_color(*color)
            # A cell never wraps, and the width is the one the layout used.
            # (A zero width would mean "to the right margin".)
            pdf.cell(max(width_of(piece), _EPS), h, text,
                     new_x=XPos.RIGHT, new_y=YPos.TOP)
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
    _ln(pdf, LINE_H)
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
    _flush(pdf)
    if space_before:
        pdf.ln(space_before)
    pdf.set_text_color(*BLACK)
    text = unicodedata.normalize('NFC', text).translate(_INVISIBLE)
    family = _SERIF
    if not _covers(_SERIF, text):
        # One face for the whole heading: the first that has every glyph, or
        # else whichever the most characters need.
        family = next((f for f in _FALLBACKS if _covers(f, text)), None) or max(
            _FALLBACKS, key=lambda f: sum(_face_for(ch, _SERIF) == f for ch in text))
    _set_font(pdf, family, 'B', size)
    # multi_cell, so a long set name wraps rather than running off the page.
    pdf.multi_cell(0, size + 2, text, align='C' if center else 'L',
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
    _ln(pdf, LINE_H)
    # Not bolded, matching the Word export: the label is scaffolding, and
    # bolding it drew the eye away from the underlined answer beside it.
    _write_text(pdf, 'ANSWER: ', _SERIF, '', SIZE)
    _write_qems(pdf, tossup.tossup_answer, is_answer=True, quoted_guides=quoted)
    _write_meta(pdf, _meta_line(tossup, opts))
    _ln(pdf, LINE_H + GAP)


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
        _ln(pdf, LINE_H)
        _write_text(pdf, '[10{0}] '.format(diff), _SERIF, 'B', SIZE)
        _write_qems(pdf, text, quoted_guides=quoted)
        _ln(pdf, LINE_H)
        _write_text(pdf, 'ANSWER: ', _SERIF, '', SIZE)
        _write_qems(pdf, answer, is_answer=True, quoted_guides=quoted)
    _write_meta(pdf, _meta_line(bonus, opts))
    _ln(pdf, LINE_H + GAP)


def _atomic(pdf, render):
    """Draw something, moving it to the next page rather than letting it split.

    Word keeps each question whole with keep_together; fpdf has no such thing,
    so the block is drawn once into a throwaway copy to see whether it would
    break, and the page is turned first if it would. Something too long to fit
    a page of its own still has to break somewhere, so this only turns the page
    when there is anything above it to move away from.

    The trial run goes into a separate scratch document (see _probe) rather
    than fpdf's offset_rendering(): that deep-copies the whole document, every
    page drawn so far included, for each question -- quadratic in a packet's
    length, which made a full-size set's export run for minutes and well
    past the server's request timeout.
    """
    probe = _probe(pdf)
    if pdf.font_family:
        _set_font(probe, pdf.font_family, pdf.font_style, pdf.font_size_pt)
    # Most questions are nowhere near the foot of the page; if even a generous
    # bound on the block's height fits, the trial run can only say "fits".
    bound = _Bound(probe, indent=pdf.get_x() - pdf.l_margin)
    render(bound)
    if pdf.get_y() + bound.total() <= pdf.page_break_trigger:
        render(pdf)
        return
    probe.add_page()
    start_page = probe.page
    if pdf.font_family:
        _set_font(probe, pdf.font_family, pdf.font_style, pdf.font_size_pt)
    probe.set_xy(pdf.get_x(), pdf.get_y())
    render(probe)
    breaks = probe.page > start_page or probe.get_y() > probe.page_break_trigger
    if breaks and pdf.get_y() > pdf.t_margin:
        pdf.add_page()
    render(pdf)


class _Bound:
    """Stands in for the document to measure a block's height without drawing
    anything.

    Paragraphs go through the same _layout the real document uses, measured
    in the probe's fonts, so the line count is the real one; `indent` is how
    far along its line the block starts. One spare line on top covers rounding.
    """

    def __init__(self, probe, indent=0):
        self.probe = probe
        self.width = probe.epw
        self.height = 0
        self.x = max(indent, 0)

    def set_font(self, family, style, size):
        _set_font(self.probe, family, style, size)

    def set_text_color(self, *rgb):
        pass

    def add_lines(self, lines, h, width_of):
        self.height += (len(lines) - 1) * h
        last = sum(width_of(p) for p in lines[-1])
        self.x = (self.x if len(lines) == 1 else 0) + last

    def ln(self, h=None):
        _flush(self)
        self.height += self.probe.font_size if h is None else h
        self.x = 0

    def multi_cell(self, w, h, text, *args, **kwargs):
        # A heading: generous, since it is only ever a line or two.
        n = int(self.probe.get_string_width(text) // (self.width * 0.75)) + 1
        self.height += n * h
        self.x = 0

    def total(self):
        _flush(self)
        return self.height + LINE_H


#: Trial runs a scratch document takes before it is replaced, so its throwaway
#: pages don't pile up over a long packet.
_PROBE_RUNS = 50


def _new_pdf():
    pdf = FPDF(unit='pt', format='letter')
    # Text is placed in cells exactly as wide as _layout measured it, so they
    # carry no padding of their own.
    pdf.c_margin = 0
    pdf.set_margins(MARGIN, MARGIN, MARGIN)
    pdf.set_auto_page_break(True, margin=MARGIN)
    return pdf


def _probe(pdf):
    """The scratch document _atomic measures a question in: the same page,
    margins and faces as `pdf`, and never output."""
    probe = getattr(pdf, '_qems_probe', None)
    if probe is None or probe._qems_runs >= _PROBE_RUNS:
        probe = _new_pdf()
        probe._qems_scratch = True   # never written out
        probe._qems_runs = 0
        pdf._qems_probe = probe
    probe._qems_runs += 1
    return probe


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
    for label, names in (('Writers', writer_names), ('Editors', editor_names)):
        if not names:
            continue
        _write_text(pdf, '{0}: '.format(label), _SERIF, 'B', SIZE)
        _write_text(pdf, ', '.join(names), _SERIF, '', SIZE)
        _ln(pdf, LINE_H)
    _ln(pdf, GAP)


def _text_block(pdf, text):
    """Plain text above the questions -- the set's credits, a packet's note --
    one line at a time, in the body face."""
    for line in (text or '').splitlines():
        if line.strip():
            _write_text(pdf, line, _SERIF, '', SIZE)
        _ln(pdf, LINE_H)
    _ln(pdf, GAP)


def _careful_notes(pdf, tossups, bonuses):
    """The answer lines flagged "read answer carefully", listed at the top of
    the packet so the moderator meets them before the room does.

    The same block the Word export prints. The answers are drawn through the
    QEMS renderer rather than as plain text, since what makes an answer worth
    flagging is usually in its formatting -- which part is underlined, what is
    only prompted on."""
    flagged = []
    for tossup, num in tossups:
        if getattr(tossup, 'read_carefully', False):
            flagged.append(('Tossup', num, tossup, [tossup.tossup_answer]))
    for bonus, num in bonuses:
        if getattr(bonus, 'read_carefully', False):
            parts = [getattr(bonus, 'part{0}_answer'.format(i), '') for i in range(1, 4)]
            flagged.append(('Bonus', num, bonus, [p for p in parts if p]))
    if not flagged:
        return
    _write_text(pdf, 'Moderator \u2014 read these answer lines carefully:', _SERIF, 'B', SIZE)
    _ln(pdf, LINE_H)
    for label, num, question, answers in flagged:
        quoted = _quoted_guides(question)
        _write_text(pdf, '{0} {1}: '.format(label, num if num else '?'), _SERIF, 'B', SIZE)
        for i, answer in enumerate(answers):
            if i:
                _write_text(pdf, ' / ', _SERIF, '', SIZE)
            _write_qems(pdf, answer, is_answer=True, quoted_guides=quoted)
        _ln(pdf, LINE_H)
    _ln(pdf, GAP)


def build_packetized_pdf(set_name, groups, opts, credits=None, front_matter=None):
    """Build a packetized PDF.

    `groups` is an ordered list of (packet_name, tossups, bonuses). `opts` is a
    dict with boolean writers/editors/ids/credits, plus `interlace` to print
    tossup 1, bonus 1, tossup 2, … (reading order) instead of all tossups then
    all bonuses. `credits` is an optional (writer_names, editor_names) tuple
    shown before the first packet's tossups. Returns PDF bytes.

    The set export calls this once per packet with a single group, so each
    packet is its own file (and carries the credits, since it travels alone);
    passing several groups still produces one combined document.

    `front_matter` is an optional list of plain-text blocks -- credits the set's
    owner wrote, a note belonging to this packet -- printed above the first
    tossup, in place of the generated `credits` when there are any.

    Questions flagged "read answer carefully" are listed under all of that, as
    they are in the Word export.
    """
    pdf = _new_pdf()

    for gi, (packet_name, tossups, bonuses) in enumerate(groups):
        pdf.add_page()
        _heading(pdf, '{0} {1}'.format(set_name or '', packet_name).strip(),
                 TITLE_SIZE, space_after=10)
        if gi == 0 and front_matter:
            for block in front_matter:
                _text_block(pdf, block)
        elif gi == 0 and opts.get('credits') and credits:
            _credits(pdf, credits[0], credits[1])
        _careful_notes(pdf, _numbered(tossups), _numbered(bonuses))
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

    _flush(pdf)
    out = pdf.output()
    return bytes(out)
