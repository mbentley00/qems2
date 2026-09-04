from __future__ import unicode_literals

import re

from bs4 import BeautifulSoup
from django.utils.encoding import smart_str
from django.utils.safestring import mark_safe
import unicodedata

DEFAULT_ALLOWED_TAGS = ['b', 'i', 'u', 'strong', 'em']

# translation mapping table that converts
# single smart quote characters to standard
# single quotes
SINGLE_QUOTE_MAP = {
        0x2018: 39,
        0x2019: 39,
        0x201A: 39,
        0x201B: 39,
        0x2039: 39,
        0x203A: 39,
}

# translation mapping table that converts
# double smart quote characters to standard
# double quotes
DOUBLE_QUOTE_MAP = {
        0x00AB: 34,
        0x00BB: 34,
        0x201C: 34,
        0x201D: 34,
        0x201E: 34,
        0x201F: 34,
}

# Constants for question types
ACF_STYLE_TOSSUP = 'ACF-style tossup'
ACF_STYLE_BONUS = 'ACF-style bonus'
VHSL_BONUS = 'VHSL bonus'

# Overflow packet: holds questions that don't fit the regular packets during
# packetization, and is the default packet for new questions in a packetized set
EXTRAS_PACKET_NAME = 'Extras'

# Constants for edit types
QUESTION_CREATE = 'Question Create'
QUESTION_CHANGE = 'Question Change'
QUESTION_EDIT = 'Question Edit'
QUESTION_RESTORE = 'Question Restore'
QUESTION_PROOFREAD = 'Question Proofread'
QUESTION_READ_CAREFULLY = 'Question Marked Read Carefully'

# Constants for types of categories
CATEGORY = "Category"
SUB_CATEGORY = "Subcategory"
SUB_SUB_CATEGORY = "Subsubcategory"

# Constants for Packet types
ACF_PACKET = "ACF Packet"
VHSL_PACKET = "VHSL Packet"

# Constants for PeriodWideEntry types
ACF_REGULAR_PERIOD = "ACF Regular Period"
ACF_TIEBREAKER_PERIOD = "ACF Tiebreaker Period"
VHSL_TOSSUP_PERIOD = "VHSL Tossup Period"
VHSL_BONUS_PERIOD = "VHSL Bonus Period"
VHSL_TIEBREAKER_PERIOD = "VHSL Tiebreaker Period"

def remove_new_lines(line):
    return line.replace("\n", "").replace("\r", "")

def sanitize_html(html, allowed_tags=DEFAULT_ALLOWED_TAGS):
    soup = BeautifulSoup(html)
    for tag in soup.find_all(True):
        if tag.name == 'span':
            new_tag = None
            try:
                if tag['style'].find('text-decoration: underline') > -1:
                    new_tag = soup.new_tag('u')
                elif tag['style'].find('text-decoration: italic') > -1:
                    new_tag = soup.new_tag('em')

                if new_tag is not None:
                    new_tag.contents = tag.contents
                    tag.replace_with(new_tag)
            except KeyError as ex:
                pass
        elif tag.name not in allowed_tags:
            tag.hidden = True

    return soup.renderContents()

def strip_markup(html):
    """The text of `html` with any HTML tags removed.

    Question prose almost never contains HTML -- QEMS's own markup is _x_,
    \\Bx\\B and ~x~, none of which is a tag -- so building a parse tree for it
    is nearly always wasted work, and this is called tens of thousands of times
    by the duplicate and repeat reports. With no "<" in the string there is
    nothing for the parser to remove, and escaping "&" then letting get_text
    unescape it is a round trip, so the text comes back exactly as it went in.

    Two exceptions fall through to the parser rather than being reasoned about:
    a string containing "<" at all, and one starting with whitespace, which the
    HTML parser drops.
    """
    html = convert_smart_quotes(html)
    if html and '<' not in html and not html[:1].isspace():
        return html
    html = html.replace("&", "&amp;")
    soup = BeautifulSoup(html)
    return soup.get_text()

def html_to_latex(html, replacement_dict):
    # replace the html tags with the appropriate latex markup
    # dict takes the form {'tag': 'latex_command'}, e.g. applying
    # {'b': 'bf'} to <b>answer</b> will produce \bf{answer}

    for h, l in replacement_dict.items():
        open_tag = '<{0}>'.format(h)
        close_tag = '</{0}>'.format(h)
        start_cmd = r'''\{0}{{'''.format(l)
        end_cmd = '}'
        html = html.replace(open_tag, start_cmd)
        html = html.replace(close_tag, end_cmd)

    return html

def get_answer_no_formatting(line):
    output = line
    output = strip_markup(output)
    # Strip unescaped markup, then turn "\_"/"\~" back into literal characters.
    # \B bold, \S superscript, \s subscript, \P pronunciation guide, \N note.
    for marker in ('\\B', '\\S', '\\s', '\\P', '\\N'):
        output = output.replace(marker, '')
    output = re.sub(r'(?<!\\)_', '', output)
    output = re.sub(r'(?<!\\)~', '', output)
    output = output.replace('\\_', '_').replace('\\~', '~')
    return output

def strip_parentheticals(text):
    """Drop parenthesized runs from an answer line — pronunciation guides and
    short asides — for compact previews like the packet grid, where they crowd
    out the answer itself. Escaped ``\\(`` / ``\\)`` are literal parentheses in
    QEMS markup, so those are kept (and unescaped). Nested groups go
    innermost-first, and the spacing left behind is tidied up."""
    if not text:
        return text
    # Park escaped parens somewhere the paren regex can't see them.
    out = text.replace('\\(', '\x00').replace('\\)', '\x01')
    prev = None
    while prev != out:
        prev = out
        out = re.sub(r'\([^()]*\)', '', out)
    out = out.replace('\x00', '(').replace('\x01', ')')
    out = re.sub(r'\s+([,;:.!?])', r'\1', out)
    return re.sub(r'\s{2,}', ' ', out).strip()


# Figure out if there's an "["
def get_primary_answer(line):
    if line is None:
        return line
    
    index = line.lower().find("[")
    if (index >= 0):
        return line[:index]
    else:
        return line

def preview(text):
    if (text is None):
        return text
    
    if (len(text) > 81):
        return mark_safe(text[0:81] + '...')
    else:
        return mark_safe(text)    

def get_formatted_question_html_for_bonus_answers(bonus):
    return get_formatted_question_html(bonus.part1_answer[0:80], True, True, False, False) + '<br />' + get_formatted_question_html(bonus.part2_answer[0:80], True, True, False, False) + '<br />' + get_formatted_question_html(bonus.part3_answer[0:80], True, True, False, False) + '<br />'

# A tossup is "all power" when its whole stem is inside the 15-point power
# region: it renders fully bold and every correct buzz scores a power. `flag`
# is the stored Tossup.all_power (True/False = an explicit editor choice, None
# = auto): by default a stem is all-power when it says "for 15 points" and
# carries no explicit (*)/(+) power mark of its own.
_FOR_15_RE = re.compile(r'for 15 points', re.IGNORECASE)

def compute_all_power(flag, text):
    if flag is not None:
        return flag
    text = text or u''
    if u'(*)' in text or u'(+)' in text:
        return False
    return _FOR_15_RE.search(text) is not None

# Markup tokens that render to nothing visible (they open/close a span). Used to
# map an offset in the *displayed* comment text back to the raw stored text.
_CONSUMED_PAIRS = (u'\\B', u'\\D', u'\\S', u'\\s', u'\\P')
_ESCAPE_PAIRS = (u'\\~', u'\\_', u'\\\\')

def _plain_to_raw_map(raw):
    """Walk the raw comment markup, returning (plain_text, offsets) where
    offsets[k] is the index in `raw` of the k-th visible character (and a final
    sentinel = len(raw) so an end offset always maps). Mirrors how
    get_formatted_question_html consumes markup for comments (underlines/parens/
    powers off): ``~`` toggles italic (consumed), ``\\B \\D \\S \\s \\P`` are
    consumed, and ``\\~ \\_ \\\\`` emit their second char."""
    raw = raw or u''
    plain = []
    offsets = []
    i = 0
    n = len(raw)
    while i < n:
        pair = raw[i:i + 2]
        if pair in _CONSUMED_PAIRS:
            i += 2
            continue
        if pair in _ESCAPE_PAIRS:
            offsets.append(i + 1)
            plain.append(raw[i + 1])
            i += 2
            continue
        if raw[i] == u'~':
            i += 1
            continue
        offsets.append(i)
        plain.append(raw[i])
        i += 1
    offsets.append(n)
    return u''.join(plain), offsets

def toggle_comment_strike(raw, start, end, strike):
    """Return `raw` with the displayed-text range [start, end) struck through
    (wrapped in ``\\D``) when `strike` is true, or with the enclosing strike
    markers removed when false. Offsets index the visible text (see
    _plain_to_raw_map). Returns the raw unchanged if the offsets are invalid."""
    raw = raw or u''
    _plain, offsets = _plain_to_raw_map(raw)
    last = len(offsets) - 1  # index of the sentinel
    if not (0 <= start < end <= last):
        return raw
    r_start = offsets[start]
    r_end = offsets[end]
    if strike:
        return raw[:r_start] + u'\\D' + raw[r_start:r_end] + u'\\D' + raw[r_end:]
    # Un-strike: drop the \D markers bracketing this range (later one first so
    # the earlier index stays valid). The closing marker sits just past the last
    # struck character, so search from there (offsets[end] would already be past
    # it, onto the next visible character).
    r_last = offsets[end - 1]
    open_at = raw.rfind(u'\\D', 0, r_start)
    close_at = raw.find(u'\\D', r_last)
    if open_at == -1 or close_at == -1:
        return raw
    raw = raw[:close_at] + raw[close_at + 2:]
    raw = raw[:open_at] + raw[open_at + 2:]
    return raw

def get_formatted_question_html(line, allowUnderlines, allowParens, allowNewLines, allowPowers,
                                allPower=False, allowSuperpower=True,
                                guidesRequireQuotes=False):
    # With the set option on, a parenthetical with no quotation marks in it isn't
    # a guide; escaping it here makes the rest of the parse render it as the
    # literal parens it is.
    if guidesRequireQuotes and allowParens:
        line = escape_unquoted_parens(line)
    italicsFlag = False
    parensFlag = False
    underlineFlag = False
    needToRestoreItalicsFlag = False
    subScriptFlag = False
    superScriptFlag = False
    boldFlag = False
    strikeFlag = False
    pgTargetFlag = False
    noteFlag = False
    powerFlag = False
    powerIndex = -1
    promptFlag = False
    index = 0
    
    previousChar = u""
    secondPreviousChar = u""
    output = u""
    nextChar = u""
    
    # If powers are allowed, see if there's a power in this question. The bold
    # power region runs from the start to the LAST power mark: a 20-point
    # superpower "(+)" (when present) comes before the 15-point power "(*)", and
    # everything up to and including the last mark is bold. Either mark is
    # optional; a tossup may carry both.
    if (allowPowers):
        starIndex = line.find(u"(*)")
        # The 20-point superpower "(+)" is only recognized when the set enables
        # it; otherwise a stray (+) is left to render as ordinary text.
        plusIndex = line.find(u"(+)") if allowSuperpower else -1
        powerIndex = max(starIndex, plusIndex)
        if (powerIndex > -1):
            powerFlag = True
            output += u"<strong>"

    # An all-power tossup with no explicit mark is bold from end to end. The
    # rest of the parsing runs normally so pronunciation guides etc. still
    # render; we just wrap everything in one <strong>.
    allPowerWrap = allowPowers and allPower and powerIndex == -1
    if (allPowerWrap):
        output += u"<strong>"

    while (index < len(line)):
        c = line[index]        
        if (index < len(line) - 1):
            nextChar = line[index + 1]
        else:
            nextChar = ""
        
        if (index >= powerIndex and powerFlag):
            powerFlag = False
            output += line[index:index + 3] + u"</strong>"  # the actual mark: (*) or (+)
            index += 3 # Skip over the rest of what's in the power mark
            continue

        # A power/superpower mark that isn't the one powerIndex points at (a stem
        # can carry both "(+)" and "(*)"; powerIndex is the later one, and "(+)"
        # prints literally when the set has superpower off). It's a scoring mark,
        # not a guide, so it renders plain instead of getting the pronunciation-
        # guide styling below.
        if (c == u"(" and allowParens and allowPowers and previousChar != u"\\"
                and line[index:index + 3] in (u"(*)", u"(+)")):
            output += line[index:index + 3]
            secondPreviousChar = u"("
            previousChar = u")"
            index += 3
            continue

        if (c == u"~" and previousChar != u"\\"):
            if (not italicsFlag):
                output += u"<i>"
                italicsFlag = True
            else:
                output += u"</i>"
                italicsFlag = False
        elif (c == u"~" and previousChar == u"\\" and secondPreviousChar != u"\\"):
            output = output[:-1] # Get rid of the escape character
            output += c
        elif (c == u"(" and allowParens and previousChar != u"\\"):
            if (italicsFlag):
                needToRestoreItalicsFlag = True
                itatlicsFlag = False
                output += u"</i>"
            
            # A guide keeps its own look (gray, never bold) even inside the
            # bolded power region: it's an aside to the moderator, not part of
            # what's read for points. The Word export does the same.
            output += u"<strong class=\"pronunciation-guide\">("
            parensFlag = True
        elif (c == u"(" and allowParens and previousChar == u"\\" and secondPreviousChar != u"\\"):
            output = output[:-1] # Get rid of the escape character
            output += c
        elif (c == u")" and allowParens and previousChar != u"\\" and secondPreviousChar != u"\\"):
            output += u")</strong>"
            parensFlag = False

            if (needToRestoreItalicsFlag):
                output += u"<i>"
                italticsFlag = True
                needToRestoreItalicsFlag = False

        elif (c == u")" and allowParens and previousChar == u"\\"):
            output = output[:-1] # Get rid of the escape character
            output += c
        elif (c == u"s" and previousChar == u"\\" and secondPreviousChar != u"\\" and not superScriptFlag):
            output = output[:-1] # Get rid of the escape character
            if (subScriptFlag):
                subScriptFlag = False
                output += u"</sub>"
            else:
                subScriptFlag = True
                output += u"<sub>"
        elif (c == u"S" and previousChar == u"\\" and secondPreviousChar != u"\\" and not subScriptFlag):
            output = output[:-1] # Get rid of the escape character
            if (superScriptFlag):
                superScriptFlag = False
                output += u"</sup>"
            else:
                superScriptFlag = True
                output += u"<sup>"
        elif (c == u"B" and previousChar == u"\\" and secondPreviousChar != u"\\"):
            output = output[:-1] # Get rid of the escape character
            if (boldFlag):
                boldFlag = False
                output += u"</b>"
            else:
                boldFlag = True
                output += u"<b>"
        elif (c == u"D" and previousChar == u"\\" and secondPreviousChar != u"\\"):
            # \Dtext\D strikes text through — used when an editor crosses out
            # part of a comment to show it's been handled.
            output = output[:-1] # Get rid of the escape character
            if (strikeFlag):
                strikeFlag = False
                output += u"</del>"
            else:
                strikeFlag = True
                output += u"<del>"
        elif (c == u"P" and previousChar == u"\\" and secondPreviousChar != u"\\"):
            # \Pwords\P marks the word(s) a following pronunciation guide
            # covers, e.g. Denis \PDiderot\P ("DID-er-OW").
            output = output[:-1] # Get rid of the escape character
            if (pgTargetFlag):
                pgTargetFlag = False
                output += u"</span>"
            else:
                pgTargetFlag = True
                output += u"<span class=\"pg-target\">"
        elif (c == u"N" and previousChar == u"\\" and secondPreviousChar != u"\\"):
            # \Ntext\N marks a note to the moderator or the players — "Description
            # acceptable.", "Note to moderator: read the answer line carefully."
            # It is read aloud but is not part of the clue, so it never counts
            # toward the question's length. The italics say the same thing to
            # anyone reading the page.
            output = output[:-1] # Get rid of the escape character
            if (noteFlag):
                noteFlag = False
                output += u"</span>"
            else:
                noteFlag = True
                output += u"<span class=\"q-note\">"
        else:
            if (c == u"_" and previousChar == u"\\" and secondPreviousChar != u"\\"):
                # Escaped underscore: render a literal "_", not markup.
                output = output[:-1] # Get rid of the escape character
                output += c
            elif (c == u"_" and allowUnderlines):
                if (nextChar == u"_"):
                    # This is a prompt
                    if (not promptFlag):
                        output += u"<u>"
                        promptFlag = True
                    else:
                        output += u"</u>"
                        promptFlag = False

                    index += 1 # Skip ahead so we don't re-process this character
                else:
                    # This is a regular answer line
                    if (not underlineFlag):
                        output += u"<u><b>"
                        underlineFlag = True
                    else:
                        output += u"</b></u>"
                        underlineFlag = False
            else:
                output += c
        secondPreviousChar = previousChar
        previousChar = c
        index += 1

    if (italicsFlag):
        output += u"</i>"

    if (boldFlag):
        output += u"</b>"

    if (strikeFlag):
        output += u"</del>"

    if (pgTargetFlag):
        output += u"</span>"

    if (noteFlag):
        output += u"</span>"

    if (underlineFlag):
        output += u"</b></u>"

    if (parensFlag):
        output += u"</strong>"
        
    if (powerFlag):
        output += u"</strong>"

    if (allPowerWrap):
        output += u"</strong>"

    if (promptFlag):
        output += u"</u>"

    if (allowNewLines):
        output = output.replace(u"&lt;br&gt;", u"<br />")

    return output

# Moderator instructions that don't count toward question length:
# sentences like "Description acceptable." or "Note to moderator: read the
# answerline carefully." plus inline markers like [emphasize].  Sentences are
# only matched at the start of the text or after sentence-ending punctuation,
# so content like "critics found the description acceptable" still counts.
_DIRECTIVE_CORE = (
    r'note to (?:the )?(?:moderators?|players?|readers?)[:,]?[^.!?]*'
    r'|(?:a )?descriptions? (?:is |are )?acceptable[^.!?]*'
    r'|(?:two|both|all) answers? (?:are |is |will be )?required[^.!?]*'
    r'|you have (?:\d+|ten|fifteen|twenty|thirty) seconds[^.!?]*'
    r'|read (?:the )?answer ?line carefully[^.!?]*'
)
MODERATOR_INSTRUCTION_RE = re.compile(
    r'(?:^|(?<=[.!?\]~]))[\s~]*(?:' + _DIRECTIVE_CORE + r')[.!?]?[~\s]*',
    re.IGNORECASE)
INLINE_DIRECTIVE_RE = re.compile(r'\[(?:emphasi[sz]e|pause|read slowly)\]\s*', re.IGNORECASE)

#: An explicitly marked note to the moderator or the players: ``\Ntext\N``.
#: The rules above have to guess from the wording, which means an unusual
#: phrasing is counted and an ordinary sentence occasionally isn't. Marking one
#: is the way to say so outright, and it always wins.
NOTE_RE = re.compile(r'\\N(.*?)\\N', re.S)

def strip_notes(line):
    """Remove ``\\Ntext\\N`` notes, and any spacing they leave behind."""
    if not line:
        return line
    line = NOTE_RE.sub('', line)
    # An unclosed \N (mid-edit, or a typo) would otherwise leave the marker in
    # the counted text; drop the marker without eating the rest of the question.
    line = line.replace('\\N', '')
    return re.sub(r'\s{2,}', ' ', line).strip()

def strip_moderator_instructions(line):
    """Remove moderator/player instruction sentences and inline directive
    markers so they don't count toward question length. Explicitly marked notes
    (``\\Ntext\\N``) go first, since they say outright what the sentence rules
    below can only infer."""
    if not line:
        return line
    line = strip_notes(line)
    line = MODERATOR_INSTRUCTION_RE.sub('', line)
    line = INLINE_DIRECTIVE_RE.sub('', line)
    return line


def get_char_count_exclusions(line, ignore_pronunciation, guides_require_quotes=False):
    """The snippets dropped before counting characters, so the UI can explain
    what wasn't counted: moderator-instruction sentences (e.g. "Description
    acceptable"), inline directives ([emphasize]), and — when the set ignores
    them — pronunciation guides. Returns a de-duplicated list of strings."""
    if not line:
        return []
    if guides_require_quotes:
        line = escape_unquoted_parens(line)
    found = []
    for m in NOTE_RE.finditer(line):
        s = m.group(1).strip()
        if s:
            found.append(s)
    # Whatever a marked note covered is already accounted for; running the
    # guessing rules over it too would list the same sentence twice.
    line = strip_notes(line)
    for m in MODERATOR_INSTRUCTION_RE.finditer(line):
        s = m.group(0).strip(' ~.!?\t\n')
        if s:
            found.append(s)
    for m in INLINE_DIRECTIVE_RE.finditer(line):
        s = m.group(0).strip()
        if s:
            found.append(s)
    if ignore_pronunciation and PARENTHETICAL_RE.search(line):
        found.append('pronunciation guides')
    if count_parenthetical_spaces(strip_moderator_instructions(line)):
        found.append('the space each guide or power mark adds')
    # De-dupe, preserving order.
    seen = set()
    out = []
    for s in found:
        key = s.lower()
        if key not in seen:
            seen.add(key)
            out.append(s)
    return out

# An unescaped parenthetical: a pronunciation guide or a power mark.
PARENTHETICAL_RE = re.compile(r'(?<!\\)\([^()]*\)')


def count_parenthetical_spaces(line):
    """How many spaces exist only to hold a parenthetical: "Goethe (GUR-tuh)
    wrote" and "clue (*) more" each carry one space the writer wouldn't have
    typed without the guide or power mark, so it isn't part of the question's
    length. One per parenthetical, whichever side the space is on."""
    n = 0
    for m in PARENTHETICAL_RE.finditer(line):
        if (m.start() > 0 and line[m.start() - 1] == ' ') or \
                (m.end() < len(line) and line[m.end()] == ' '):
            n += 1
    return n


def get_character_count(line, ignore_pronunciation, guides_require_quotes=False):
    line = strip_moderator_instructions(line)
    # An unquoted parenthetical is ordinary text under this set option, so it is
    # escaped into literal parens and counted like any other words.
    if guides_require_quotes:
        line = escape_unquoted_parens(line)
    # \P markers only annotate which words a pronunciation guide covers; they
    # are never read, so they never count.
    line = line.replace('\\P', '')
    # Whether or not guides count, the space one forces the writer to add never
    # does: "Goethe (GUR-tuh) wrote" is as long as "Goethe wrote" plus the guide.
    spaces = count_parenthetical_spaces(line)
    if not ignore_pronunciation:
        return max(len(line) - spaces, 0)

    count = 0
    parensFlag = False # Parentheses indicate pronunciation guide
    previousChar = ""
    for c in line:
        if (parensFlag):
            if (c == ")" and previousChar != "\\"):
                parensFlag = False
        else:
            if (c == "(" and previousChar != "\\"):
                parensFlag = True                    
            elif (c != "~" and not (previousChar == "\\" and (c == ")" or c == "("))):
                count = count + 1 # Only count non-special chars not in pronunciation guide
        previousChar = c

    return max(count - spaces, 0)

def special_character_imbalance_reason(line):
    """Return a human-readable reason the formatting characters are unbalanced,
    or None if everything is balanced.  Underscores (_) mark underlined text,
    tildes (~) mark italics, and parentheses must nest one level deep at most.
    A preceding backslash escapes any of these."""
    underlineFlag = False
    italicsFlag = False
    parensFlag = False
    previousChar = ""
    for c in line:
        if (c == '_' and previousChar != "\\"):
            underlineFlag = not underlineFlag
        elif (c == '~' and previousChar != "\\"):
            italicsFlag = not italicsFlag
        elif (c == '(' and previousChar != "\\"):
            if (parensFlag):
                return ('Nested parentheses: an opening "(" appears before an '
                        'earlier "(" was closed. Escape a literal paren as "\\(".')
            else:
                parensFlag = True
        elif (c == ')' and previousChar != "\\"):
            if (parensFlag):
                parensFlag = False
            else:
                return ('An extra closing ")" appears with no matching "(". '
                        'Escape a literal paren as "\\)".')
        previousChar = c

    if underlineFlag:
        return ('Unbalanced underline markers ("_"): there is an odd number of '
                'them, so some underlined text is never closed. Escape a literal '
                'underscore as "\\_".')
    if italicsFlag:
        return ('Unbalanced italics markers ("~"): there is an odd number of '
                'them, so some italicized text is never closed. Escape a literal '
                'tilde as "\\~".')
    if parensFlag:
        return ('An opening "(" is never closed. Escape a literal paren as "\\(".')
    if len(re.findall(r'(?<!\\)\\P', line)) % 2:
        return ('Unbalanced pronunciation-guide target markers ("\\P"): there is '
                'an odd number of them. Wrap the word(s) a guide covers in a '
                'pair, e.g. Denis \\PDiderot\\P ("DID-er-OW").')
    if len(re.findall(r'(?<!\\)\\N', line)) % 2:
        return ('Unbalanced note markers ("\\N"): there is an odd number of '
                'them. Wrap a note to the moderator or players in a pair, e.g. '
                '\\NDescription acceptable.\\N')
    return None

def are_special_characters_balanced(line):
    return special_character_imbalance_reason(line) is None

def does_answerline_have_underlines(line):
    if (line == ""):
        return True # Ignore completely blank lines

    # An escaped "\_" is a literal underscore, not an underlined required portion.
    if re.search(r'(?<!\\)_', line):
        return True
    else:
        return False

# A parenthetical that isn't already escaped as a literal "\(".
_UNESCAPED_PAREN_RE = re.compile(r'(?<!\\)\(([^()]*)\)')
# A pronunciation guide is a quoted respelling: ("KAM-uh-flahzh").
_QUOTED_GUIDE_RE = re.compile(u'^\\s*["“‘\'].*["”’\']\\s*$', re.S)

# The quote characters that mark a parenthetical as a pronunciation guide when
# the set requires them. Only double quotes count: an apostrophe is ordinary
# prose ("(Smith's own account)" is a note, not a respelling), and QEMS converts
# straight quotes to curly ones on save, so both forms have to be accepted.
_GUIDE_QUOTE_CHARS = u'"“”'


def parenthetical_has_quotes(inner):
    """True if the text inside a parenthetical carries a quotation mark, i.e. it
    reads as a respelling — ``("DID-er-OW")`` — rather than an aside."""
    return any(ch in (inner or '') for ch in _GUIDE_QUOTE_CHARS)


def escape_unquoted_parens(line):
    """Escape the parentheses around every parenthetical with no quotation marks
    in it, so it renders, counts and style-checks as ordinary text rather than as
    a pronunciation guide.

    This is how ``QuestionSet.guides_require_quotes`` is applied: QEMS treats any
    parenthetical as a guide, and a set that also writes ordinary asides —
    "(a portrait of the artist's wife)" — wants those read, counted and left
    alone. Escaped parens are already the app's way of saying "literal paren", so
    everything downstream (rendering, the character count, the ``\\P`` rules, the
    exports) obeys without knowing about the setting.

    Power marks ``(*)``/``(+)`` are scoring marks, not text, and are left alone;
    so are parens already escaped by hand. Only one level is rewritten — nested
    parentheses are a formatting error the balance check already reports."""
    if not line:
        return line

    def _escape(match):
        inner = match.group(1)
        if not inner.strip() or inner.strip() in ('*', '+'):
            return match.group(0)
        if parenthetical_has_quotes(inner):
            return match.group(0)
        return '\\(' + inner + '\\)'

    return _UNESCAPED_PAREN_RE.sub(_escape, line)


def escape_answer_note_parens(answer):
    """Escape the parentheses around an editorial note at the end of an answer
    line, so it renders as text rather than as a pronunciation guide.

    Anything parenthesized after the closing "]" of the acceptable-answers
    section is almost always a note — "(when witnessing a demonstration of
    dazzle camouflage, FDR declared, ...)" — while a real guide is a quoted
    respelling like ("KAM-uh-flahzh"). Left alone, QEMS renders the note in the
    grey pronunciation-guide style. Guides, power marks, and parens already
    escaped are untouched, as is an answer line with no bracket section (there
    a guide on the primary answer is indistinguishable from a note)."""
    if not answer:
        return answer
    cut = answer.rfind(']')
    if cut == -1:
        return answer
    head, tail = answer[:cut + 1], answer[cut + 1:]

    def _escape(match):
        inner = match.group(1)
        if not inner.strip() or inner.strip() in ('*', '+'):
            return match.group(0)
        if _QUOTED_GUIDE_RE.match(inner):
            return match.group(0)
        return '\\(' + inner + '\\)'

    return head + _UNESCAPED_PAREN_RE.sub(_escape, tail)


def convert_smart_quotes(line):
    return smart_str(line).translate(DOUBLE_QUOTE_MAP).translate(SINGLE_QUOTE_MAP)


def _prev_significant_char(text, i):
    """The nearest character before index i that isn't QEMS inline markup
    (underscores, tildes, or a \\P/\\B/\\S/\\s toggle), so quote direction is
    judged by the visible text: in `_"Ode"_` the quote still opens."""
    j = i - 1
    while j >= 0:
        ch = text[j]
        if ch in ('_', '~'):
            j -= 1
        elif ch in ('P', 'B', 'S', 's') and j > 0 and text[j - 1] == '\\':
            j -= 2
        else:
            return ch
    return ''


def smarten_quotes(text):
    """Convert straight quotes and apostrophes to typographic ("smart") ones,
    picking opening or closing by context: a quote after a space/start/opening
    bracket opens; anything else closes. A single quote after a letter is an
    apostrophe (’), and a leading '90s-style apostrophe stays an apostrophe."""
    if not text:
        return text
    openers = set(' \t\n([{-–—/')
    out = []
    for i, c in enumerate(text):
        if c not in ('"', "'"):
            out.append(c)
            continue
        prev = _prev_significant_char(text, i)
        opening = (prev == '' or prev in openers or prev in ('‘', '“'))
        if c == '"':
            out.append('“' if opening else '”')
        else:
            nxt = text[i + 1] if i + 1 < len(text) else ''
            if opening and not nxt.isdigit():
                out.append('‘')
            else:
                out.append('’')
    return ''.join(out)

def strip_special_chars(line):
    return line.replace('_', '').replace('~', '')

def strip_unicode(line):
    if (isinstance(line, str)):
        # line is not a unicode string, and normalizing it will throw
        return line
    if (line is None or line == ""):
        return ""
    return ''.join(c for c in unicodedata.normalize('NFKD', line)
              if unicodedata.category(c) != 'Mn')

def get_bonus_type_from_question_type(question_type):
    if (question_type is None or str(question_type) == ''):
        # print "bonus type none"
        return ACF_STYLE_BONUS
    elif (str(question_type) == VHSL_BONUS):
        # print "vhsl"
        return VHSL_BONUS
    else:
        # print "acf"
        return ACF_STYLE_BONUS

def get_tossup_type_from_question_type(question_type):
    if (question_type is None or str(question_type) == ''):
        # print "tossup type none"
        return ACF_STYLE_TOSSUP
    else:
        return ACF_STYLE_TOSSUP

def collapse_nested_markup(text, markers=('\\B', '\\S', '\\s')):
    """Fold a marker pair nested directly inside the same marker into one.

    ``\B\Bfoo (*)\B\B`` means bold foo, once; but the display and the rich
    editor read markers as alternating open/close, so the inner pair renders
    and the outer one shows as literal "\B" text on either side. (Pasting
    from Google Docs used to produce exactly this: the real bold span sat
    inside Docs' own <b> wrapper and was wrapped twice.)

    A run of k adjacent markers with no text between is k opens when outside
    a span and, inside one, closes the span -- reopening if the run is longer
    than the depth, so ``\Bfoo\B\Bbar\B`` (two spans back to back) comes
    through unchanged. Text without a doubled marker is returned as is."""
    if not text:
        return text
    for marker in markers:
        if marker + marker not in text:
            continue
        parts = re.split('((?:' + re.escape(marker) + ')+)', text)
        out = []
        depth = 0
        for part in parts:
            if not part:
                continue
            if part.replace(marker, '') == '':
                k = len(part) // len(marker)
                if depth == 0:
                    out.append(marker)
                    depth = k
                elif k >= depth:
                    out.append(marker)
                    extra = k - depth
                    depth = 0
                    if extra:
                        out.append(marker)
                        depth = extra
                else:
                    # Fewer closes than opens: the span stays open (the text
                    # was already unbalanced; leave the remainder alone).
                    depth -= k
            else:
                out.append(part)
        text = ''.join(out)
    return text


def strip_answer_from_answer_line(line):
    if (line is not None):
        line = line.replace("ANSWER: ", "")
    
    return line

class InvalidTossup(Exception):

    def __init__(self, *args, reason=None):
        self.args = [a for a in args]
        self.reason = reason

    def __str__(self):
        s = '*' * 50 + '<br />'
        s += 'Invalid tossup {0}!<br />'.format(self.args[2])
        if self.reason:
            s += '{0}<br />'.format(self.reason)
        s += 'The problem is in field: {0}, which has value: {1}<br />'.format(self.args[0], self.args[1])
        s += '*' * 50 + '<br />'

        return s


class InvalidBonus(Exception):

    def __init__(self, *args, reason=None):
        self.args = [a for a in args]
        self.reason = reason

    def __str__(self):
        s = '*' * 50 + '<br />'
        s += 'Invalid bonus {0}!<br />'.format(self.args[2])
        if self.reason:
            s += '{0}<br />'.format(self.reason)
        s += 'The problem is in field: {0}, which has value: {1}<br />'.format(self.args[0], self.args[1])
        s += '*' * 50 + '<br />'

        return s

class InvalidPacket(Exception):

    def __init__(self, *args):
        self.args = [a for a in args]

    def __str__(self):
        s = '*' * 80 + '\n'
        s += 'There was a problem in packet {0}\n'.format(self.args[0])
        s += '*' * 80 + '\n'

        return s
