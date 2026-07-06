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
    html = convert_smart_quotes(html)
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
    output = output.replace('\\P', '')
    output = re.sub(r'(?<!\\)_', '', output)
    output = re.sub(r'(?<!\\)~', '', output)
    output = output.replace('\\_', '_').replace('\\~', '~')
    return output

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

def get_formatted_question_html(line, allowUnderlines, allowParens, allowNewLines, allowPowers):
    italicsFlag = False
    parensFlag = False
    underlineFlag = False
    needToRestoreItalicsFlag = False
    subScriptFlag = False
    superScriptFlag = False
    boldFlag = False
    pgTargetFlag = False
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
        plusIndex = line.find(u"(+)")
        powerIndex = max(starIndex, plusIndex)
        if (powerIndex > -1):
            powerFlag = True
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
            
            if (not powerFlag):
                output += u"<strong class=\"pronunciation-guide\">("
                parensFlag = True
            else:
                output += u"("
        elif (c == u"(" and allowParens and previousChar == u"\\" and secondPreviousChar != u"\\"):
            output = output[:-1] # Get rid of the escape character
            output += c
        elif (c == u")" and allowParens and previousChar != u"\\" and secondPreviousChar != u"\\"):
            if (not powerFlag):
                output += u")</strong>"
                parensFlag = False
            else:
                output += u")"

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

    if (pgTargetFlag):
        output += u"</span>"

    if (underlineFlag):
        output += u"</b></u>"

    if (parensFlag):
        output += u"</strong>"
        
    if (powerFlag):
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

def strip_moderator_instructions(line):
    """Remove moderator/player instruction sentences and inline directive
    markers so they don't count toward question length."""
    if not line:
        return line
    line = MODERATOR_INSTRUCTION_RE.sub('', line)
    line = INLINE_DIRECTIVE_RE.sub('', line)
    return line


def get_char_count_exclusions(line, ignore_pronunciation):
    """The snippets dropped before counting characters, so the UI can explain
    what wasn't counted: moderator-instruction sentences (e.g. "Description
    acceptable"), inline directives ([emphasize]), and — when the set ignores
    them — pronunciation guides. Returns a de-duplicated list of strings."""
    if not line:
        return []
    found = []
    for m in MODERATOR_INSTRUCTION_RE.finditer(line):
        s = m.group(0).strip(' ~.!?\t\n')
        if s:
            found.append(s)
    for m in INLINE_DIRECTIVE_RE.finditer(line):
        s = m.group(0).strip()
        if s:
            found.append(s)
    if ignore_pronunciation and re.search(r'(?<!\\)\([^()]*\)', line):
        found.append('pronunciation guides')
    # De-dupe, preserving order.
    seen = set()
    out = []
    for s in found:
        key = s.lower()
        if key not in seen:
            seen.add(key)
            out.append(s)
    return out

def get_character_count(line, ignore_pronunciation):
    line = strip_moderator_instructions(line)
    # \P markers only annotate which words a pronunciation guide covers; they
    # are never read, so they never count.
    line = line.replace('\\P', '')
    if not ignore_pronunciation:
        return len(line)

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

    return count

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
