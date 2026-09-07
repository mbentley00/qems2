/**
 * rich_editor.js — WYSIWYG entry for new tossups and bonuses.
 *
 * Replaces the question textareas on the add-tossup/add-bonus pages and the
 * Type Questions page with contenteditable editors so formatting pasted from
 * Google Docs/Word shows as real bold/italic/underline. The underlying
 * textareas stay in the form and are kept in sync with QEMS markup (converted
 * via paste_convert.js's htmlToQemsMarkup), so the server sees exactly what
 * it always has:
 *   bold + underline -> _text_      underline only -> __text__
 *   italic           -> ~text~      superscript/subscript -> \S \S / \s \s
 *
 * Single-question fields are single-paragraph (Enter disabled, whitespace
 * collapsed); the Type Questions box is multiline because the packet parser
 * is line-oriented.
 */
$(function () {

    if (!window.QemsMarkup) { return; }

    var FIELD_SELECTOR = '#id_tossup_text, #id_tossup_answer, #id_leadin, ' +
        '#id_part1_text, #id_part1_answer, #id_part2_text, #id_part2_answer, ' +
        '#id_part3_text, #id_part3_answer';

    function escapeHtml(s) {
        return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    // One line of QEMS markup -> minimal display HTML
    // (mirrors get_formatted_question_html)
    function qemsLineToHtml(line) {
        var html = escapeHtml(line || '');
        // Protect escaped literals (\_ and \~) so they don't become markup.
        html = html.replace(/\\_/g, '@@QXUS@@').replace(/\\~/g, '@@QXTI@@');
        html = html.replace(/__([^_]+)__/g, '<u>$1</u>');
        html = html.replace(/_([^_]+)_/g, '<u><b>$1</b></u>');
        html = html.replace(/~([^~]+)~/g, '<i>$1</i>');
        html = html.replace(/\\B([\s\S]+?)\\B/g, '<b>$1</b>');
        html = html.replace(/\\S([\s\S]+?)\\S/g, '<sup>$1</sup>');
        html = html.replace(/\\s([\s\S]+?)\\s/g, '<sub>$1</sub>');
        // \Ntext\N — a note to the moderator/players. Shown the way it renders
        // on the saved question, so it's obvious which words are outside the
        // character count. Italic comes from CSS, not an <em>, so converting
        // back to markup doesn't add tildes inside the note.
        html = html.replace(/\\N([\s\S]+?)\\N/g, '<span class="q-note">$1</span>');
        // \Pword\P marks the target of a following pronunciation guide. The
        // guide itself is greyed whenever the pair is complete, so "anchored"
        // is something you can see rather than infer: amber word, grey guide.
        // Closing markup may sit between the two (~...\PDauphin\P~ ("DOFF-in")),
        // so the gap allows closing tags as well as spaces.
        html = html.replace(
            /\\P([\s\S]+?)\\P((?:<\/[a-z]+>|\s)*)(\(([^()]*)\))?/g,
            function (all, term, gap, guide, inner) {
                var marked = '<span class="pg-target">' + term + '</span>';
                gap = gap || '';
                if (guide && window.QemsMarkup.parenIsGuide(stripTags(inner))) {
                    return marked + gap + '<span class="pg-guide">' + guide + '</span>';
                }
                return marked + gap + (guide || '');
            });
        html = html.replace(/@@QXUS@@/g, '\\_').replace(/@@QXTI@@/g, '\\~');
        return html;
    }

    // Text content of a display-HTML fragment — used when a decision has to be
    // made about what the reader sees, not about the tags around it.
    function stripTags(html) {
        return (html || '').replace(/<[^>]*>/g, '');
    }

    function qemsToHtml(text, multiline) {
        if (!multiline) {
            return qemsLineToHtml(text);
        }
        // One <div> per line, matching how contenteditable structures lines
        return (text || '').split('\n').map(function (line) {
            return '<div>' + (qemsLineToHtml(line) || '<br>') + '</div>';
        }).join('');
    }

    // Strip stray spaces at line edges (e.g. from inter-paragraph whitespace
    // in pasted HTML), collapse runs, cap blank lines
    function tidyMultiline(text) {
        return text.replace(/[ \t]{2,}/g, ' ')
            .replace(/[ \t]+\n/g, '\n')
            .replace(/\n[ \t]+/g, '\n')
            .replace(/\n{3,}/g, '\n\n');
    }

    // Editor HTML -> QEMS markup
    function htmlToQems(html, multiline) {
        var text = window.QemsMarkup.htmlToQems(html).replace(/\u00a0/g, ' ');
        if (multiline) {
            return tidyMultiline(text).replace(/^\s+/, '').replace(/\s+$/, '');
        }
        return text.replace(/\s+/g, ' ').trim();
    }

    var registry = {};
    var resyncFns = [];
    // Every enhanced field's write-through, including the ones the registry
    // cannot name: the structured answer rows are <input>s with no id, so they
    // are reachable only from here.
    var syncDownFns = [];

    /* ---------- Special characters (the toolbar's Omega button) ---------- */

    // Accented letters are pairs so the panel's Caps toggle can show the
    // upper-case form of the same grid rather than doubling its height.
    // Ordered by base letter, which is how a writer hunts for one.
    var SYMBOL_LETTERS = [
        ['á', 'Á'], ['à', 'À'], ['â', 'Â'], ['ä', 'Ä'], ['ã', 'Ã'], ['å', 'Å'],
        ['ā', 'Ā'], ['ą', 'Ą'], ['æ', 'Æ'],
        ['é', 'É'], ['è', 'È'], ['ê', 'Ê'], ['ë', 'Ë'], ['ē', 'Ē'], ['ě', 'Ě'],
        ['ę', 'Ę'],
        ['í', 'Í'], ['ì', 'Ì'], ['î', 'Î'], ['ï', 'Ï'], ['ī', 'Ī'], ['ı', 'İ'],
        ['ó', 'Ó'], ['ò', 'Ò'], ['ô', 'Ô'], ['ö', 'Ö'], ['õ', 'Õ'], ['ő', 'Ő'],
        ['ø', 'Ø'], ['œ', 'Œ'],
        ['ú', 'Ú'], ['ù', 'Ù'], ['û', 'Û'], ['ü', 'Ü'], ['ū', 'Ū'], ['ů', 'Ů'],
        ['ű', 'Ű'],
        ['ý', 'Ý'], ['ÿ', 'Ÿ'],
        ['ç', 'Ç'], ['ć', 'Ć'], ['č', 'Č'], ['ď', 'Ď'], ['đ', 'Đ'], ['ğ', 'Ğ'],
        ['ł', 'Ł'], ['ñ', 'Ñ'], ['ń', 'Ń'], ['ř', 'Ř'], ['ś', 'Ś'], ['š', 'Š'],
        ['ş', 'Ş'], ['ť', 'Ť'], ['ź', 'Ź'], ['ż', 'Ż'], ['ž', 'Ž'],
        ['ð', 'Ð'], ['þ', 'Þ'], ['ß', 'ß']
    ];

    var SYMBOL_CURRENCY = [
        ['€', 'Euro'], ['£', 'Pound'], ['¥', 'Yen / yuan'], ['¢', 'Cent'],
        ['₹', 'Indian rupee'], ['₽', 'Russian ruble'], ['₩', 'Korean won'],
        ['₪', 'Israeli shekel'], ['₺', 'Turkish lira'], ['₴', 'Ukrainian hryvnia'],
        ['₦', 'Nigerian naira'], ['₫', 'Vietnamese dong'], ['ƒ', 'Florin / guilder'],
        ['¤', 'Generic currency sign']
    ];

    // Dashes, quotes and the marks that come up while proofreading a question.
    var SYMBOL_MARKS = [
        ['—', 'Em dash'], ['–', 'En dash'], ['…', 'Ellipsis'],
        ['“', 'Left double quote'], ['”', 'Right double quote'],
        ['‘', 'Left single quote'], ['’', 'Right single quote / apostrophe'],
        ['«', 'Left guillemet'], ['»', 'Right guillemet'],
        ['′', 'Prime (feet, minutes)'], ['″', 'Double prime (inches, seconds)'],
        ['°', 'Degree'], ['·', 'Middle dot'], ['•', 'Bullet'],
        ['§', 'Section'], ['¶', 'Pilcrow'], ['†', 'Dagger'], ['‡', 'Double dagger'],
        ['©', 'Copyright'], ['®', 'Registered'], ['™', 'Trademark'],
        ['±', 'Plus-minus'], ['×', 'Multiplication'], ['÷', 'Division'],
        ['−', 'Minus sign'], ['≈', 'Approximately equal'], ['≠', 'Not equal'],
        ['≤', 'Less than or equal'], ['≥', 'Greater than or equal'],
        ['½', 'One half'], ['⅓', 'One third'], ['¼', 'One quarter'],
        ['¾', 'Three quarters'],
        ['¡', 'Inverted exclamation'], ['¿', 'Inverted question mark']
    ];

    var enhanceCount = 0;

    // `opts.compact` drops the toolbar and sizes the editor like an ordinary
    // one-line input: the structured answer rows are four fields wide, and a
    // toolbar apiece would bury the fields under their own chrome. Ctrl+B/U/I
    // and paste conversion still work there.
    function enhance(textarea, multiline, opts) {
        opts = opts || {};
        var $ta = $(textarea);
        // Distinguishes this editor's document-level handlers from the other
        // fields' (a page has one editor per question field).
        var editorSeq = ++enhanceCount;
        // Server-side validation still applies; a hidden required field
        // would silently block submission.
        $ta.removeAttr('required');

        var $toolbar = $(
            '<div class="rich-editor-toolbar">' +
            // Bold is bold-only (Ctrl+B); combine with Underline for the
            // required-answer (bold+underline) convention.
            '  <a href="#" class="rich-editor-btn" data-cmd="bold" title="Bold (Ctrl+B)"><b>B</b></a>' +
            '  <a href="#" class="rich-editor-btn" data-cmd="underline" title="Underline (Ctrl+U)"><u>U</u></a>' +
            '  <a href="#" class="rich-editor-btn" data-cmd="italic" title="Italic (Ctrl+I)"><i>I</i></a>' +
            '  <a href="#" class="rich-editor-btn" data-cmd="subscript" title="Subscript">x<sub>2</sub></a>' +
            '  <a href="#" class="rich-editor-btn" data-cmd="superscript" title="Superscript">x<sup>2</sup></a>' +
            // Characters no keyboard here has: the accents European names need,
            // currency signs, and the dashes and marks that come up in
            // proofreading. Opens a panel under the toolbar; the caret stays
            // where it was, so a click drops the character into the sentence.
            '  <a href="#" class="rich-editor-btn rich-editor-sym" data-cmd="symbols" ' +
            'title="Special characters: accents, currency, dashes and proofreading marks">Ω</a>' +
            // Pronunciation-guide target: select the word(s) together with the
            // following ("...") guide, and this wraps just the word(s) in \P...\P.
            // With the caret inside an existing mark it removes that mark, so PG
            // is a toggle; selecting fewer words inside a mark shrinks it.
            '  <a href="#" class="rich-editor-btn rich-editor-pg" data-cmd="pgtarget" title="Mark pronunciation-guide target: select the word(s) and their (&quot;...&quot;) guide. Click with the caret inside a mark to remove it.">PG</a>' +
            // Guess every unmarked guide\'s target from how many words its
            // respelling has, so marks don't have to be placed by hand.
            '  <a href="#" class="rich-editor-btn rich-editor-pg" data-cmd="pgauto" title="Mark the target of every pronunciation guide in this field, guessing the word(s) each one covers from its respelling">PG auto</a>' +
            // A note to the moderator/players (\N...\N): read aloud, but never
            // counted toward the question length. A toggle, like PG.
            '  <a href="#" class="rich-editor-btn rich-editor-note" data-cmd="note" title="Mark a note to the moderator or players (e.g. &quot;Description acceptable.&quot;). It is read aloud but never counts toward the question length. Click with the caret inside a note to remove the mark.">Note</a>' +
            // Switch to editing the raw QEMS markup (e.g. ~foo~ for italics) in
            // the underlying textarea, to hand-fix anything the rich view got
            // wrong; the label flips to "Rich" to switch back.
            // How worn a clue is, without leaving the question. The same
            // lookup the highlight popup does, asked for deliberately and
            // answered under the field, where it stays put while you read it.
            '  <a href="#" class="rich-editor-btn rich-editor-qb" data-cmd="qbfreq" title="How often the selected phrase appears in the qbreader database. Select a phrase first.">DB</a>' +
            '  <a href="#" class="rich-editor-btn rich-editor-plain" data-cmd="plaintext" title="Edit the raw QEMS markup directly (e.g. ~foo~ for italics, _foo_ for answer underlines)">Raw</a>' +
            '</div>');
        // Type Questions is the one box where a bonus is typed from nothing --
        // everywhere else the [10] parts are separate fields the page supplies.
        // The shape (leadin line, three parts, an ANSWER line apiece) is what
        // the parser is strict about, so it's worth handing over ready-made.
        if (textarea.id === 'id_questions') {
            $toolbar.find('.rich-editor-qb').before(
                '<a href="#" class="rich-editor-btn rich-editor-skel" data-cmd="bonusskel" ' +
                'title="Start a bonus: the &quot;For 10 points each:&quot; line, three [10] parts ' +
                'and an ANSWER line for each. Added at the end of the box, with the caret where ' +
                'the leadin goes.">Bonus skeleton</a>');
        }
        var $editor = $('<div class="rich-editor" contenteditable="true" spellcheck="true"></div>');
        // Only the big bulk "type questions" box gets the extra-tall sizing.
        // (Edit-page fields are also multiline so Enter works, but size by role.)
        if (textarea.id === 'id_questions' || textarea.id === 'unified-bonus-text') { $editor.addClass('rich-editor-multiline'); }
        // Long stem/leadin/part-text fields start taller; answer lines stay short.
        var TALL_FIELDS = ['id_tossup_text', 'id_leadin',
                           'id_part1_text', 'id_part2_text', 'id_part3_text'];
        var SHORT_FIELDS = ['id_tossup_answer', 'id_part1_answer',
                            'id_part2_answer', 'id_part3_answer'];
        if (textarea.id && TALL_FIELDS.indexOf(textarea.id) !== -1) {
            $editor.addClass('rich-editor-tall');
        } else if (textarea.id && SHORT_FIELDS.indexOf(textarea.id) !== -1) {
            $editor.addClass('rich-editor-short');
        }
        if (opts.editorClass) { $editor.addClass(opts.editorClass); }
        $editor.html(qemsToHtml($ta.val(), multiline));

        // A contenteditable has no placeholder of its own, so carry the
        // field's across and show it while the editor is empty.
        var placeholder = opts.placeholder || $ta.attr('placeholder') || '';
        if (placeholder) { $editor.attr('data-placeholder', placeholder); }
        function updatePlaceholder() {
            $editor.toggleClass('rich-editor-empty', !$editor.text().trim());
        }
        updatePlaceholder();

        // The expanding-textareas plugin wraps textareas in div.expanding —
        // hide the wrapper if present, otherwise the textarea itself.
        var $anchorEl = $ta.closest('div.expanding');
        if (!$anchorEl.length) { $anchorEl = $ta; }
        var $wrapper = $('<div class="rich-editor-wrapper"></div>');
        if (opts.compact) {
            $wrapper.addClass('rich-editor-compact').append($editor);
        } else {
            $wrapper.append($toolbar, $editor);
        }
        // Carry the field's layout classes onto the wrapper, which is what now
        // occupies its place in the row.
        if (opts.wrapperClass) { $wrapper.addClass(opts.wrapperClass); }
        $anchorEl.after($wrapper).hide();

        // When true, the raw textarea is showing and is the source of truth, so
        // the rich editor must not push its (frozen) content back over it.
        var plainMode = false;

        function syncDown() {
            if (plainMode) { return; }
            $ta.val(htmlToQems($editor[0].innerHTML, multiline));
        }

        // Swap between the rich editor and the underlying textarea (the raw
        // QEMS markup) so a writer can hand-fix something the rich view parsed
        // wrong. Each side is converted into the other on switch.
        function setPlainMode(on) {
            if (on === plainMode) { return; }
            // The panel inserts into the rich editor, which is about to be
            // hidden; leaving it open would float it over the raw textarea.
            showSymbolPanel(false);
            if (on) {
                $ta.val(htmlToQems($editor[0].innerHTML, multiline));
                plainMode = true;
                $editor.hide();
                $anchorEl.show();
                $ta.trigger('focus');
            } else {
                plainMode = false;
                $editor.html(qemsToHtml($ta.val(), multiline));
                $anchorEl.hide();
                $editor.show();
            }
            $wrapper.toggleClass('rich-editor-plainmode', plainMode);
            $toolbar.find('.rich-editor-plain')
                .text(plainMode ? 'Rich' : 'Raw')
                .attr('title', plainMode
                    ? 'Back to the rich text editor'
                    : 'Edit the raw QEMS markup directly (e.g. ~foo~ for italics, _foo_ for answer underlines)');
        }

        // The HTML of the current selection inside this editor (empty if the
        // selection is collapsed or outside the editor).
        function selectionHtml() {
            var sel = window.getSelection();
            if (!sel || !sel.rangeCount || sel.isCollapsed) { return ''; }
            var range = sel.getRangeAt(0);
            if (!$editor[0].contains(range.commonAncestorContainer)) { return ''; }
            var box = document.createElement('div');
            box.appendChild(range.cloneContents());
            return box.innerHTML;
        }

        // The span of class `cls` containing `node`, if any (bounded by the editor).
        function spanAt(node, cls) {
            while (node && node !== $editor[0]) {
                if (node.nodeType === 1 && $(node).hasClass(cls)) { return node; }
                node = node.parentNode;
            }
            return null;
        }

        function pgTargetAt(node) { return spanAt(node, 'pg-target'); }

        // The span of class `cls` the caret/selection currently sits inside.
        function currentSpan(cls) {
            var sel = window.getSelection();
            if (!sel || !sel.rangeCount) { return null; }
            var range = sel.getRangeAt(0);
            if (!$editor[0].contains(range.commonAncestorContainer)) { return null; }
            return spanAt(range.commonAncestorContainer, cls);
        }

        function currentPgTarget() { return currentSpan('pg-target'); }
        function currentNote() { return currentSpan('q-note'); }

        // Wrap the selection in a note (\N...\N), or, with the caret inside one
        // and nothing selected, unwrap it — the same toggle shape as PG. A note
        // is read aloud but never counts toward the question length, so being
        // able to take one off matters as much as putting it on.
        function toggleNote() {
            var span = currentNote();
            if (span) {
                var inner = span.innerHTML;
                selectNode(span);
                document.execCommand('insertHTML', false, inner);
                syncDown();
                return;
            }
            var html = selectionHtml();
            if (!html) { return; }
            // Never nest: a note inside a note would round-trip to \N\N pairs
            // that don't mean anything.
            html = html.replace(/<span class="q-note">([\s\S]*?)<\/span>/g, '$1');
            document.execCommand('insertHTML', false,
                '<span class="q-note">' + html + '</span>');
            syncDown();
        }

        function selectNode(node) {
            var range = document.createRange();
            range.selectNode(node);
            var sel = window.getSelection();
            sel.removeAllRanges();
            sel.addRange(range);
        }

        function rangeHtml(range) {
            var box = document.createElement('div');
            box.appendChild(range.cloneContents());
            return box.innerHTML;
        }

        // Drop the mark around the caret. Goes through execCommand on a range
        // covering the span so the native undo stack stays intact. The guide
        // beside it loses its grey at the same time — the grey means "this pair
        // is anchored", so it must not outlive the anchor.
        function unmarkPgTarget(span) {
            var inner = span.innerHTML;
            ungreyGuideAfter(span);
            selectNode(span);
            document.execCommand('insertHTML', false, inner);
            syncDown();
        }

        // Unwrap the .pg-guide span that follows `node` (skipping whitespace and
        // the closing markup a target inside italics leaves behind).
        function ungreyGuideAfter(node) {
            var next = node.nextSibling;
            while (next) {
                if (next.nodeType === 3 && !next.nodeValue.trim()) { next = next.nextSibling; continue; }
                if (next.nodeType === 1 && $(next).hasClass('pg-guide')) {
                    $(next).replaceWith(next.innerHTML);
                }
                return;
            }
        }

        // Narrow an existing mark to just the selected word(s): rebuild the
        // span's contents with the selection marked and the rest plain.
        function remarkInside(span, range) {
            var pre = document.createRange();
            pre.setStart(span, 0);
            pre.setEnd(range.startContainer, range.startOffset);
            var post = document.createRange();
            post.setStart(range.endContainer, range.endOffset);
            post.setEnd(span, span.childNodes.length);
            var html = rangeHtml(pre) +
                '<span class="pg-target">' + rangeHtml(range) + '</span>' +
                rangeHtml(post);
            selectNode(span);
            document.execCommand('insertHTML', false, html);
            syncDown();
        }

        // Mark the target of every not-yet-marked guide in this field, guessing
        // each target from the guide's word count. Mirrors the server's
        // style_checker.mark_pg_target.
        function autoMarkPgTargets() {
            var result = window.QemsMarkup.autoMarkPgTargets($ta.val() || '');
            if (!result.changed) { return; }
            $ta.val(result.text);
            resyncUp();
        }

        // Wrap the selected word(s) in a pronunciation-guide target span
        // (\Pword\P). The user is expected to select the term together with its
        // following ("...") guide; the trailing parenthetical is left outside
        // the span so only the spoken word(s) get marked. If the selection has
        // no trailing guide, the whole selection is wrapped.
        //
        // A mark is never a dead end: with the caret inside one and nothing
        // selected this removes it, and selecting part of a mark moves the mark
        // onto just that part.
        function wrapPgTarget() {
            var span = currentPgTarget();
            if (span) {
                var sel = window.getSelection();
                if (!sel || !sel.rangeCount || sel.isCollapsed) { unmarkPgTarget(span); }
                else { remarkInside(span, sel.getRangeAt(0)); }
                return;
            }
            var html = selectionHtml();
            if (!html) { return; }
            // Peel any pg-target/pg-guide markers already inside the selection so
            // we don't nest spans when re-marking.
            html = html.replace(/<span class="pg-(?:target|guide)">([\s\S]*?)<\/span>/g, '$1');
            // Split off a trailing ("...") / (...) guide, keeping it outside the
            // span — and greying it, since marking the target is exactly what
            // makes the pair anchored.
            var m = html.match(/^([\s\S]*?)(\s*\(([^()]*)\)\s*)$/);
            var termHtml, tail;
            if (m && m[1].replace(/<[^>]*>/g, '').trim() &&
                    window.QemsMarkup.parenIsGuide(stripTags(m[3]))) {
                termHtml = m[1].replace(/\s+$/, '');
                tail = ' <span class="pg-guide">' + m[2].trim() + '</span>';
            } else {
                termHtml = html;
                tail = '';
            }
            document.execCommand('insertHTML', false,
                '<span class="pg-target">' + termHtml + '</span>' + tail);
            syncDown();
        }

        function resyncUp() {
            $editor.html(qemsToHtml($ta.val(), multiline));
        }

        $editor.on('input blur', syncDown);
        $editor.on('input blur focus', updatePlaceholder);
        syncDownFns.push(syncDown);

        // Remember the caret/selection inside the editor so actions that move
        // focus away (toolbar buttons, the category-tag tree) can restore it.
        var savedRange = null;
        function saveSelection() {
            var sel = window.getSelection();
            if (sel && sel.rangeCount && $editor[0].contains(sel.anchorNode)) {
                savedRange = sel.getRangeAt(0).cloneRange();
            }
        }
        $editor.on('keyup mouseup blur', saveSelection);

        /* ---------- The special-characters panel ---------- */

        // Built on first use and then reused: several dozen buttons per field
        // is a lot of DOM to create for editors nobody opens it on.
        var $symPanel = null;

        function symGrid(pairs, isLetters) {
            return '<div class="sym-grid">' + pairs.map(function (p) {
                if (isLetters) {
                    return '<a href="#" class="sym-btn" data-lower="' + p[0] +
                        '" data-upper="' + p[1] + '">' + p[0] + '</a>';
                }
                return '<a href="#" class="sym-btn" title="' + p[1] + '">' + p[0] + '</a>';
            }).join('') + '</div>';
        }

        function buildSymbolPanel() {
            $symPanel = $(
                '<div class="rich-editor-symbols">' +
                '  <div class="sym-head">' +
                '    <span class="sym-heading">Special characters</span>' +
                '    <a href="#" class="sym-caps" title="Show the capital letters">Caps</a>' +
                '    <a href="#" class="sym-close" title="Close (Esc)">&times;</a>' +
                '  </div>' +
                '  <div class="sym-label">Letters</div>' + symGrid(SYMBOL_LETTERS, true) +
                '  <div class="sym-label">Currency</div>' + symGrid(SYMBOL_CURRENCY) +
                '  <div class="sym-label">Punctuation and marks</div>' + symGrid(SYMBOL_MARKS) +
                '</div>');
            // Keep the caret: focus must never leave the editor on the way in.
            $symPanel.on('mousedown', function (e) { e.preventDefault(); });
            $symPanel.on('click', '.sym-btn', function (e) {
                e.preventDefault();
                insertSymbol($(this).text());
            });
            $symPanel.on('click', '.sym-caps', function (e) {
                e.preventDefault();
                var caps = !$symPanel.hasClass('sym-uppercase');
                $symPanel.toggleClass('sym-uppercase', caps);
                $(this).toggleClass('active', caps)
                    .attr('title', caps ? 'Show the small letters' : 'Show the capital letters');
                $symPanel.find('.sym-btn[data-lower]').each(function () {
                    $(this).text($(this).attr(caps ? 'data-upper' : 'data-lower'));
                });
            });
            $symPanel.on('click', '.sym-close', function (e) {
                e.preventDefault();
                showSymbolPanel(false);
            });
            $wrapper.append($symPanel);
        }

        function showSymbolPanel(on) {
            if (on && !$symPanel) { buildSymbolPanel(); }
            if (!$symPanel) { return; }
            $symPanel.toggle(!!on);
            $toolbar.find('.rich-editor-sym').toggleClass('active', !!on);
            if (on) {
                // Sits directly under the toolbar, whatever height it wrapped to.
                $symPanel.css('top', $toolbar.outerHeight() + 'px');
                $(document).on('mousedown.qsym' + editorSeq, function (e) {
                    if (!$symPanel[0].contains(e.target) &&
                        !$toolbar.find('.rich-editor-sym')[0].contains(e.target)) {
                        showSymbolPanel(false);
                    }
                });
                $(document).on('keydown.qsym' + editorSeq, function (e) {
                    if (e.key === 'Escape') { showSymbolPanel(false); }
                });
            } else {
                $(document).off('mousedown.qsym' + editorSeq)
                           .off('keydown.qsym' + editorSeq);
            }
        }

        /* ---------- Bonus skeleton (Type Questions) ---------- */

        var BONUS_LEADIN_LINE = 'For 10 points each:';
        var BONUS_SKELETON = [BONUS_LEADIN_LINE,
                              '[10] ', 'ANSWER: ',
                              '[10] ', 'ANSWER: ',
                              '[10] ', 'ANSWER: '];

        // Append a blank bonus at the end of the box. The end, rather than the
        // caret: the box holds a run of whole questions, and dropping seven
        // lines into the middle of one would split it in two.
        //
        // Written through the textarea and rebuilt from it (rather than
        // inserted into the editor) so the editor keeps one <div> per line,
        // which is how the category-tag button finds the line the caret is on.
        function insertBonusSkeleton() {
            syncDown();
            var text = String($ta.val() || '').replace(/\s+$/, '');
            // A blank line off the last question, the way questions are spaced
            // when they're typed by hand. (The parser ignores blank lines.)
            var lines = (text ? text.split('\n').concat(['']) : [])
                .concat(BONUS_SKELETON);
            $ta.val(lines.join('\n'));
            resyncUp();
            caretAtLineStart(lines.length - BONUS_SKELETON.length);
            updatePlaceholder();
            // The character-count panel listens for this.
            $editor.trigger('input');
        }

        // Put the caret at the front of the skeleton's "For 10 points each:"
        // line, which is where the leadin gets typed -- the first thing you
        // write, and the one line the skeleton can't start for you.
        function caretAtLineStart(index) {
            var lineEl = $editor[0].childNodes[index];
            if (!lineEl) { return; }
            $editor.focus();
            var range = document.createRange();
            range.selectNodeContents(lineEl);
            range.collapse(true);
            var sel = window.getSelection();
            sel.removeAllRanges();
            sel.addRange(range);
            // resyncUp rebuilt the editor, so the remembered caret points at
            // nodes that are gone; save this one the way a click would.
            $editor.trigger('keyup');
        }

        // Drop a character in at the caret. The caret was saved when focus left
        // the editor for the toolbar, so the character lands where the writer
        // was typing rather than at the end of the field. insertText (rather
        // than a DOM edit) keeps the native undo stack intact, and the
        // character picks up whatever formatting is active there.
        function insertSymbol(ch) {
            $editor.focus();
            var sel = window.getSelection();
            if (!sel) { return; }
            if (!sel.rangeCount || !$editor[0].contains(sel.anchorNode)) {
                var range;
                if (savedRange && $editor[0].contains(savedRange.startContainer)) {
                    range = savedRange;
                } else {
                    range = document.createRange();
                    range.selectNodeContents($editor[0]);
                    range.collapse(false);
                }
                sel.removeAllRanges();
                sel.addRange(range);
            }
            document.execCommand('insertText', false, ch);
            saveSelection();
            syncDown();
            updatePlaceholder();
        }

        // Reflect the formatting at the caret/selection on the toolbar buttons
        // (a button appears "pressed" when its style is active).
        function updateToolbarState() {
            var inPg = !!currentPgTarget();
            var inNote = !!currentNote();
            $toolbar.find('.rich-editor-btn').each(function () {
                var cmd = $(this).attr('data-cmd');
                // None of these is a formatting state: PG auto and the bonus
                // skeleton are one-shot actions, and the symbols button is lit
                // while its panel is open.
                if (cmd === 'pgauto' || cmd === 'symbols' || cmd === 'bonusskel') { return; }
                var active;
                if (cmd === 'pgtarget') {
                    // Pressed while the caret is inside a mark, so it reads as a
                    // toggle: clicking again removes the mark.
                    active = inPg;
                    $(this).attr('title', inPg
                        ? 'Remove this pronunciation-guide mark (or select fewer words to shrink it)'
                        : 'Mark pronunciation-guide target: select the word(s) and their ("...") guide');
                } else if (cmd === 'note') {
                    active = inNote;
                    $(this).attr('title', inNote
                        ? 'This is a note to the moderator/players — click to unmark it and count it toward the length again'
                        : 'Mark the selection as a note to the moderator or players. It is read aloud but never counts toward the question length.');
                } else {
                    try { active = document.queryCommandState(cmd); } catch (e) { active = false; }
                }
                $(this).toggleClass('active', active);
            });
        }
        $editor.on('keyup mouseup focus', updateToolbarState);

        // Common formatting shortcuts: Ctrl/Cmd + B / I / U. Bold is bold-only.
        $editor.on('keydown', function (e) {
            if (e.ctrlKey || e.metaKey) {
                var k = (e.key || '').toLowerCase();
                var cmd = k === 'b' ? 'bold' : k === 'u' ? 'underline' : k === 'i' ? 'italic' : null;
                if (cmd) {
                    e.preventDefault();
                    document.execCommand(cmd, false, null);
                    syncDown();
                    updateToolbarState();
                }
            }
        });

        // Single-paragraph fields: no line breaks
        if (!multiline) {
            $editor.on('keydown', function (e) {
                if (e.which === 13) { e.preventDefault(); }
            });
        }

        // Normalize pastes to QEMS-supported formatting only, so what you
        // see is exactly what will be saved
        $editor.on('paste', function (e) {
            var cd = e.originalEvent.clipboardData || window.clipboardData;
            if (!cd) { return; }
            e.preventDefault();
            var html = cd.getData('text/html');
            var qems;
            if (html && window.QemsMarkup.isRichHtml(html)) {
                qems = window.QemsMarkup.htmlToQems(html);
            } else {
                // Plain text may already contain QEMS markup — render it
                qems = cd.getData('text/plain') || '';
            }
            qems = qems.replace(/\u00a0/g, ' ');
            // Styling-not-emphasis bold from the source (a bolded power
            // section, a wrapper that bolds everything) is dropped — see
            // cleanPastedQems. Only for pastes; the editor's own content
            // never passes through here.
            qems = window.QemsMarkup.cleanPastedQems(qems);
            var insert;
            if (multiline) {
                qems = tidyMultiline(qems).replace(/\s+$/, '');
                // <br> separators rather than nested <div>s when inserting
                // mid-line
                insert = qems.split('\n').map(qemsLineToHtml).join('<br>');
            } else {
                insert = qemsLineToHtml(qems.replace(/\s+/g, ' '));
            }
            document.execCommand('insertHTML', false, insert);
            syncDown();
        });

        // Other features (Paste Full Tossup dialog, unified bonus editor)
        // write to the textarea and trigger change — reflect that here
        $ta.on('change', resyncUp);
        resyncFns.push(resyncUp);

        // Keep the user's selection when clicking toolbar buttons
        $toolbar.on('mousedown', 'a', function (e) { e.preventDefault(); });
        $toolbar.on('click', 'a', function (e) {
            e.preventDefault();
            var cmd = $(this).attr('data-cmd');
            if (cmd === 'plaintext') { setPlainMode(!plainMode); return; }
            if (cmd === 'qbfreq') {
                if (window.QemsQbreader && window.QemsQbreader.lookupInto) {
                    window.QemsQbreader.lookupInto($wrapper);
                }
                return;
            }
            // The formatting buttons act on the rich editor; ignore them while
            // the raw textarea is showing.
            if (plainMode) { return; }
            if (cmd === 'symbols') {
                showSymbolPanel(!$symPanel || !$symPanel.is(':visible'));
                return;
            }
            if (cmd === 'bonusskel') {
                insertBonusSkeleton();
                return;
            }
            $editor.focus();
            if (cmd === 'pgtarget') {
                wrapPgTarget();
            } else if (cmd === 'pgauto') {
                autoMarkPgTargets();
            } else if (cmd === 'note') {
                toggleNote();
            } else {
                cmd.split(',').forEach(function (c) {
                    document.execCommand(c, false, null);
                });
            }
            syncDown();
            updateToolbarState();
        });

        if (textarea.id) {
            registry[textarea.id] = {
                root: $editor[0], syncDown: syncDown, resyncUp: resyncUp,
                multiline: multiline,
                getSavedRange: function () { return savedRange; }
            };
        }
    }

    /* ---------- Category tag insertion (Type Questions page) ---------- */

    // The parser's own line shapes (packet_parser.py: ansregex, bpart_regex,
    // vhsl_bpart_regex), which are what decide where one question ends.
    var ANSWER_LINE = /^a..?wers?:/i;
    var BONUS_PART_LINE = /^\[V?\d+[emh]?\]/i;

    // Group a run of typed questions into blocks of line indexes. An ANSWER
    // line closes a question -- unless the next line is another [10] part, in
    // which case the same bonus is still going. That is the whole difference
    // between a tossup (one answer line) and a bonus (three), and it is why a
    // bonus's category tag belongs on the last of them.
    function questionBlocks(lines) {
        var blocks = [], current = null, closed = true, i, line;
        for (i = 0; i < lines.length; i++) {
            line = (lines[i] || '').trim();
            if (!line) { continue; }
            if (closed && !BONUS_PART_LINE.test(line)) {
                current = {start: i, lastAnswer: -1};
                blocks.push(current);
            }
            closed = false;
            if (ANSWER_LINE.test(line)) {
                current.lastAnswer = i;
                closed = true;   // reopened above if the next line is a [10] part
            }
        }
        return blocks;
    }

    // Where a category tag belongs in a run of typed questions: at the end of
    // the last answer line of the question the caret is in -- the only answer
    // line of a tossup, the third part's for a bonus. A caret between questions
    // (or past the end) belongs to the question above, which is the one just
    // typed. Returns -1 when no question around the caret has an answer line
    // yet, and the caller drops the tag at the caret instead.
    function answerLineFor(lines, cursorLine) {
        var blocks = questionBlocks(lines);
        if (!blocks.length) { return -1; }
        // The last question that starts at or above the caret is the one the
        // caret is in (or, in the gap between two, the one just finished).
        var chosen = blocks[0], i;
        for (i = 0; i < blocks.length && cursorLine >= blocks[i].start; i++) {
            chosen = blocks[i];
        }
        // A question still being typed has no answer line to hang the tag on;
        // fall back to the nearest complete question above it.
        if (chosen.lastAnswer < 0) {
            for (i = blocks.indexOf(chosen) - 1; i >= 0; i--) {
                if (blocks[i].lastAnswer >= 0) { return blocks[i].lastAnswer; }
            }
            return -1;
        }
        return chosen.lastAnswer;
    }

    // Put the tag on that line, replacing whatever category was there before:
    // clicking a second category means you changed your mind, not that the
    // question has two.
    function placeTagOnLine(line, tag) {
        var existing = /\{[^{}]*\}\s*$/;
        if (existing.test(line)) { return line.replace(existing, tag); }
        return line.replace(/\s+$/, '') + ' ' + tag;
    }

    // Which line of the editor the caret sits on. Multiline editors keep one
    // <div> per line, so the caret's line is the index of the line element it
    // is inside.
    function caretLineIndex(editor, range) {
        if (!range || !editor.contains(range.startContainer)) { return -1; }
        var node = range.startContainer;
        while (node && node.parentNode !== editor) { node = node.parentNode; }
        if (!node) { return -1; }
        var idx = Array.prototype.indexOf.call(editor.childNodes, node);
        return idx < 0 ? -1 : idx;
    }

    // Insert a category tag in the rich editor for the given textarea id.
    // Returns false if no such editor exists (the caller falls back to
    // plain-textarea handling). Uses the caret saved before focus moved to the
    // tag button, and inserts via execCommand so the native undo stack stays
    // intact (direct DOM edits broke undo and ignored the caret).
    function insertCategoryTag(textareaId, tag) {
        var reg = registry[textareaId];
        if (!reg) { return false; }
        var editor = reg.root;
        editor.focus();

        var sel = window.getSelection();
        var range = reg.getSavedRange && reg.getSavedRange();

        // A run of typed questions: the tag goes at the end of the relevant
        // answer line rather than at the caret, which is usually still in the
        // stem the writer was reading.
        var $ta = $('#' + textareaId);
        if (reg.multiline && $ta.length) {
            reg.syncDown();
            var lines = String($ta.val() || '').split('\n');
            var cursorLine = caretLineIndex(editor, range);
            if (cursorLine < 0) { cursorLine = lines.length - 1; }
            var target = answerLineFor(lines, cursorLine);
            if (target >= 0) {
                lines[target] = placeTagOnLine(lines[target], tag);
                $ta.val(lines.join('\n'));
                reg.resyncUp();
                // Leave the caret at the end of the line the tag went on, so
                // the writer can see where it landed.
                var lineEl = editor.childNodes[target];
                if (lineEl) {
                    var r = document.createRange();
                    r.selectNodeContents(lineEl);
                    r.collapse(false);
                    sel.removeAllRanges();
                    sel.addRange(r);
                    // resyncUp rebuilt the editor, so the remembered caret
                    // points at nodes that no longer exist. Save the new one
                    // the same way a click does, or the next tag would find no
                    // caret and fall back to the last question in the box.
                    $(editor).trigger('keyup');
                }
                return true;
            }
        }

        if (range && editor.contains(range.startContainer)) {
            sel.removeAllRanges();
            sel.addRange(range);
        } else {
            // No remembered caret — drop the tag at the very end.
            range = document.createRange();
            range.selectNodeContents(editor);
            range.collapse(false);
            sel.removeAllRanges();
            sel.addRange(range);
        }

        // The tag must be plain text, so turn off any inline formatting active
        // at the caret before inserting (otherwise the tag inherits e.g. the
        // underline of the answer it follows).
        ['bold', 'italic', 'underline', 'subscript', 'superscript'].forEach(function (cmd) {
            try {
                if (document.queryCommandState(cmd)) { document.execCommand(cmd, false, null); }
            } catch (e) { /* command unsupported */ }
        });
        document.execCommand('insertText', false, ' ' + tag);
        reg.syncDown();
        return true;
    }

    window.QemsRichEditor = {
        get: function (textareaId) { return registry[textareaId] || null; },
        // Write every editor through to the field behind it. What "unchanged"
        // means on an edit page is what the page would save right now, and
        // that is only knowable once every editor has had its say.
        syncAllDown: function () {
            syncDownFns.forEach(function (fn) {
                try { fn(); } catch (e) { /* one bad field must not stop the rest */ }
            });
        },
        insertCategoryTag: insertCategoryTag,
        // Shared with the plain-textarea fallback on the Type Questions page,
        // so both agree on where a category tag belongs.
        answerLineFor: answerLineFor,
        placeTagOnLine: placeTagOnLine,
        // Enhance a field the page built after load (a structured answer row
        // added by its + button).
        enhanceField: function (field, opts) {
            opts = opts || {};
            enhance(field, !!opts.multiline, opts);
        },
        // QEMS markup -> display HTML, for anything that shows a line rather
        // than edits it (the structured answer's live "Reads as").
        markupToHtml: qemsLineToHtml
    };

    /* ---------- Wire up the pages ---------- */

    // Add-tossup / add-bonus and edit-tossup / edit-bonus pages: per-field
    // editors, all of which take line breaks. Writing a question is the same
    // job as reflowing one -- you paste a draft, you break a clue apart to look
    // at it -- so Enter works the same way on both.
    var $qForm = $('#add-tossups, #add-bonuses, #edit-tossup, #edit-bonus').first();
    $qForm.find(FIELD_SELECTOR).each(function () {
        enhance(this, true);
    });

    // Structured answer lines: every text field is rich, so a writer sees the
    // answer as it prints rather than the underscores that produce it. The
    // primary answer earns the full toolbar (it is the same field the plain
    // editor gives one to); the rows are compact.
    $('.structured-answer').each(function () {
        var $block = $(this);
        $block.find('.sa-primary-input').each(function () {
            enhance(this, false, {editorClass: 'rich-editor-short'});
        });
        $block.find('.sa-text, .sa-instruction').each(function () {
            enhance(this, false, {compact: true});
        });
    });

    // Type Questions page: one big line-oriented box
    $('#id_questions').each(function () {
        enhance(this, true);
    });

    // The unified bonus editor is a whole-bonus rich-text box (multiline).
    // paste_convert.js populates its value and shows it before this runs.
    $('#unified-bonus-text').each(function () {
        enhance(this, true);
    });

    // Switching back from the unified bonus editor rewrites the individual
    // textareas without firing change — resync after its handler runs
    $('#toggle-unified-editor').on('click', function () {
        setTimeout(function () {
            resyncFns.forEach(function (fn) { fn(); });
        }, 0);
    });
});
