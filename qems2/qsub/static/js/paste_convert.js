/**
 * paste_convert.js — Word/rich-text paste conversion to QEMS markup,
 * unified bonus editor, and paste-full-tossup dialog.
 */
$(function () {

    // ========================================================================
    // 1. Word Paste Converter
    // ========================================================================

    /**
     * Convert an HTML string (from clipboard) into QEMS markup plain text.
     * Rules:
     *   bold + underline → _text_
     *   underline only   → __text__
     *   italic            → ~text~
     *   superscript       → \Stext\S
     *   subscript         → \stext\s
     *   everything else   → plain text
     */
    function htmlToQemsMarkup(html) {
        var doc = new DOMParser().parseFromString(html, 'text/html');
        mergeNestedFormatting(doc.body);
        // Inter-paragraph whitespace in the source HTML otherwise leaves
        // stray spaces at line edges
        return walkNode(doc.body)
            .replace(/[ \t]+\n/g, '\n')
            .replace(/\n[ \t]+/g, '\n');
    }

    /**
     * Clean QEMS markup that came from a PASTE (never from the editor's own
     * round-trip — \B a writer typed stays).
     *
     * Two kinds of pasted bold are styling rather than emphasis, and kept
     * bolding "a lot of text by default":
     *
     * 1. A bold run that ends at a power mark — qbreader and QEMS's own
     *    displays bold the pre-power text, but here power formatting is
     *    derived from the (*) / (+) mark itself, so the pasted bold would
     *    only duplicate it (and trip the hand-bolded-power warning).
     * 2. Bold covering essentially the whole paste — a source whose wrapper
     *    styles everything bold (sites using <strong> for styling, themes,
     *    export wrappers). Nobody bolds an entire question on purpose.
     *    Short pastes are left alone: a single bold word was likely copied
     *    for its boldness.
     */
    function cleanPastedQems(qems) {
        if (!qems) { return qems; }
        // 1. Bold ending in a power mark: unwrap, the mark carries the power.
        qems = qems.replace(/\\B([\s\S]*?\((?:\*|\+)\)\s*)\\B/g, '$1');
        // 2. Wrapper bold: measure how much of the text sits inside \B pairs.
        var boldLen = 0;
        qems.replace(/\\B([\s\S]*?)\\B/g, function (all, inner) {
            boldLen += inner.replace(/\s+/g, '').length;
            return all;
        });
        var totalLen = qems.replace(/\\B/g, '').replace(/\s+/g, '').length;
        if (totalLen >= 40 && boldLen >= 0.9 * totalLen) {
            qems = qems.replace(/\\B([\s\S]*?)\\B/g, '$1');
        }
        return qems;
    }

    /**
     * Collapse <b><u>text</u></b> / <u><b>text</b></u> pairs into a single
     * element carrying both styles, so walkNode sees bold+underline together
     * and emits _text_ rather than underline-only __text__. (Google Docs puts
     * both styles on one span, but Word and execCommand nest the tags.)
     */
    function mergeNestedFormatting(root) {
        var pairs = root.querySelectorAll('b > u, strong > u, u > b, u > strong');
        Array.prototype.forEach.call(pairs, function (inner) {
            var outer = inner.parentNode;
            // Only merge when the inner tag is the outer tag's sole content
            for (var n = outer.firstChild; n; n = n.nextSibling) {
                if (n === inner) continue;
                if (n.nodeType === Node.TEXT_NODE && !n.textContent.trim()) continue;
                return;
            }
            var span = outer.ownerDocument.createElement('span');
            span.setAttribute('style', 'font-weight:bold;text-decoration:underline');
            while (inner.firstChild) {
                span.appendChild(inner.firstChild);
            }
            outer.parentNode.replaceChild(span, outer);
        });
    }

    /**
     * Wrap text in a QEMS markup marker. Markup is line-scoped, so when the
     * content spans line breaks (Word's clipboard HTML can nest paragraph
     * breaks inside formatting tags) each line is wrapped separately —
     * otherwise the pair splits across lines and parses as stray markers.
     * Leading/trailing whitespace stays outside the markers.
     */
    function wrapInlineMarkup(text, marker) {
        return text.split('\n').map(function (segment) {
            var m = segment.match(/^(\s*)([\s\S]*?)(\s*)$/);
            if (!m[2]) { return segment; }
            // Already wrapped in this marker (a bold run nested in a bold
            // wrapper): wrapping again would nest the pair, which renders as
            // literal markers.
            if (m[2].length > 2 * marker.length &&
                m[2].slice(0, marker.length) === marker &&
                m[2].slice(-marker.length) === marker &&
                m[2].slice(marker.length, -marker.length).indexOf(marker) === -1) {
                return segment;
            }
            return m[1] + marker + m[2] + marker + m[3];
        }).join('\n');
    }

    /**
     * The value of CSS property `prop` in an inline style string, or '' if it
     * isn't set. Matches only a real declaration of that exact property — the
     * property name must start the string or follow a ';' or whitespace — so
     * `cssValue(style, 'font-weight')` does NOT match Word's
     * `mso-bidi-font-weight` (preceded by '-'). Returns the last declaration
     * when a property is repeated (later wins, as in CSS). `style` is assumed
     * lowercased already.
     */
    function cssValue(style, prop) {
        var re = new RegExp('(?:^|;|\\s)' + prop + '\\s*:\\s*([^;]*)', 'g');
        var m, val = '';
        while ((m = re.exec(style)) !== null) { val = m[1].trim(); }
        return val;
    }

    function walkNode(node) {
        if (node.nodeType === Node.TEXT_NODE) {
            // Newlines/tabs inside text nodes are source formatting (Word
            // pretty-prints its clipboard HTML with hard wraps mid-paragraph);
            // real line breaks only come from block elements and <br>
            return node.textContent.replace(/[\r\n\t]+/g, ' ');
        }
        if (node.nodeType !== Node.ELEMENT_NODE) {
            return '';
        }

        // Recurse into children first
        var inner = '';
        for (var i = 0; i < node.childNodes.length; i++) {
            inner += walkNode(node.childNodes[i]);
        }

        // Determine formatting from tag name and inline styles
        var tag = node.tagName.toLowerCase();

        // Empty formatting stubs (left behind by contenteditable edits)
        // would otherwise emit stray markup like '____'
        if (!inner && !/^(p|div|br|li|tr|h[1-6])$/.test(tag)) {
            return '';
        }

        var style = (node.getAttribute('style') || '').toLowerCase();
        // Read the real CSS property value, NOT a substring of the style string:
        // Word emits complex-script attributes like `mso-bidi-font-weight:bold`
        // on runs that are visually normal weight, and a substring test for
        // "font-weight" + "bold" would match those and bold the text falsely.
        var fontWeight = cssValue(style, 'font-weight');
        // An explicit inline font-weight wins over the tag name. Google Docs
        // wraps its whole clipboard in <b style="font-weight:normal"
        // id="docs-internal-guid-..."> -- a <b> that is not bold -- and
        // trusting the tag bolded entire pasted bonuses. Real bold from Docs
        // is a <span style="font-weight:700">, which still reads as bold.
        var isBold = fontWeight
            ? (fontWeight === 'bold' || fontWeight === 'bolder' ||
               (/^\d{3}$/.test(fontWeight) && parseInt(fontWeight, 10) >= 700))
            : (tag === 'b' || tag === 'strong');
        var textDecoration = cssValue(style, 'text-decoration') + ' ' +
                             cssValue(style, 'text-decoration-line');
        var isUnderline = (tag === 'u' || textDecoration.indexOf('underline') !== -1);
        var fontStyle = cssValue(style, 'font-style');
        var isItalic = (tag === 'i' || tag === 'em' ||
                        fontStyle.indexOf('italic') !== -1 ||
                        fontStyle.indexOf('oblique') !== -1);
        var isSup = (tag === 'sup');
        var isSub = (tag === 'sub');
        // Pronunciation-guide target span written by the rich editor
        // (<span class="pg-target">) round-trips back to \Pword\P markup.
        var isPgTarget = (tag === 'span' &&
                          /\bpg-target\b/.test(node.getAttribute('class') || ''));
        // Likewise <span class="q-note"> -> \Ntext\N. The note looks italic
        // through CSS rather than an <em> tag, precisely so that round-tripping
        // it doesn't add a pair of tildes inside the note on every save.
        var isNote = (tag === 'span' &&
                      /\bq-note\b/.test(node.getAttribute('class') || ''));

        // Apply QEMS markup wrappers
        if (isBold && isUnderline) {
            inner = wrapInlineMarkup(inner, '_');
        } else if (isUnderline) {
            inner = wrapInlineMarkup(inner, '__');
        } else if (isBold) {
            // Bold only -> \Btext\B (mirrors \S/\s for sup/sub)
            inner = wrapInlineMarkup(inner, '\\B');
        }
        if (isItalic) {
            inner = wrapInlineMarkup(inner, '~');
        }
        if (isSup) {
            inner = wrapInlineMarkup(inner, '\\S');
        }
        if (isSub) {
            inner = wrapInlineMarkup(inner, '\\s');
        }
        if (isPgTarget) {
            inner = wrapInlineMarkup(inner, '\\P');
        }
        if (isNote) {
            inner = wrapInlineMarkup(inner, '\\N');
        }

        // Block-level elements get a newline after them
        if (/^(p|div|br|li|tr|h[1-6])$/.test(tag)) {
            if (tag === 'br') {
                inner = '\n';
            } else {
                // Skip empty paragraphs (Word uses <p>&nbsp;</p> for blank lines)
                var trimmed = inner.replace(/[\u00a0\s]/g, '');
                if (!trimmed) {
                    inner = '\n';
                } else {
                    inner = inner + '\n';
                }
                // A block directly after inline content also breaks the line
                // before it (contenteditable leaves the first line bare:
                // "first<div>second</div>")
                var prev = node.previousSibling;
                var prevIsBreak = prev && prev.nodeType === Node.ELEMENT_NODE &&
                    /^(p|div|br|li|tr|h[1-6])$/i.test(prev.tagName);
                var prevHasContent = prev && (prev.nodeType !== Node.TEXT_NODE || prev.textContent.trim());
                if (prev && !prevIsBreak && prevHasContent) {
                    inner = '\n' + inner;
                }
            }
        }

        return inner;
    }

    /**
     * How many words a pronunciation guide covers, guessed from its whitespace:
     * one respelled chunk per spoken word, so ("zhahn-pohl SAR-truh") covers the
     * two words of "Jean-Paul Sartre".
     */
    function guideWordCount(inner) {
        var s = (inner || '').trim().replace(/^["“”'’]+|["“”'’]+$/g, '').trim();
        return s ? s.split(/\s+/).length : 0;
    }

    /**
     * True if the text in front of a guide ends with a closing \P. Closing
     * markup may sit between the marked word and its guide — a target inside
     * italics reads ~Death of the \PDauphin\P~ ("DOFF-in") — so trailing
     * italic/underline characters and \S \s \B tokens are stepped over first.
     * Mirrors style_checker.ends_with_pg_target on the server.
     */
    function endsWithPgTarget(head) {
        var s = (head || '').replace(/\s+$/, '');
        while (s) {
            if (/\\P$/.test(s)) { return true; }
            if (/[_~]$/.test(s)) { s = s.slice(0, -1).replace(/\s+$/, ''); }
            else if (/\\[SsB]$/.test(s)) { s = s.slice(0, -2).replace(/\s+$/, ''); }
            else { return false; }
        }
        return false;
    }

    /**
     * True when this set only reads a parenthetical as a pronunciation guide if
     * it carries quotation marks (QuestionSet.guides_require_quotes, published
     * by base.html). Defaults to false, QEMS's long-standing behaviour.
     */
    function guidesRequireQuotes() {
        return !!window.QEMS_GUIDES_REQUIRE_QUOTES;
    }

    /**
     * True if `inner` — the text between a pair of parens — reads as a
     * pronunciation guide. Power marks are never guides; under the set option
     * neither is an aside with no quotation marks in it. Only double quotes
     * count: an apostrophe belongs to ordinary prose.
     * Mirrors utils.parenthetical_has_quotes on the server.
     */
    function parenIsGuide(inner) {
        if (inner === '*' || inner === '+') { return false; }
        if (!guidesRequireQuotes()) { return true; }
        return /["“”]/.test(inner || '');
    }

    /**
     * Wrap the word(s) before each unmarked pronunciation guide in \P...\P,
     * guessing how many words each guide covers from its word count. Guides are
     * walked right-to-left so earlier match offsets stay valid. Power marks
     * (*) and (+) and escaped parens are not guides.
     * Mirrors style_checker.mark_pg_target on the server.
     * Returns {text, changed}.
     */
    function autoMarkPgTargets(text) {
        var re = /\(([^()]*)\)/g, m, guides = [];
        while ((m = re.exec(text)) !== null) {
            if (m.index > 0 && text.charAt(m.index - 1) === '\\') { continue; }
            if (!parenIsGuide(m[1])) { continue; }
            guides.push(m);
        }
        var out = text, changed = 0;
        for (var i = guides.length - 1; i >= 0; i--) {
            var g = guides[i];
            var n = guideWordCount(g[1]);
            if (!n) { continue; }
            var head = out.slice(0, g.index), tail = out.slice(g.index);
            var trimmed = head.replace(/\s+$/, '');
            var trailing = head.slice(trimmed.length);
            if (endsWithPgTarget(trimmed)) { continue; }  // already marked
            var words = trimmed.split(' ');
            if (words.length < n) { continue; }
            var target = words.slice(words.length - n).join(' ');
            if (!target.trim() || target.indexOf('\\P') !== -1) { continue; }
            var rest = words.slice(0, words.length - n).join(' ');
            out = (rest ? rest + ' ' : '') + '\\P' + target + '\\P' + trailing + tail;
            changed++;
        }
        return {text: out, changed: changed};
    }

    // Expose the converter for other scripts (e.g. rich_editor.js)
    window.QemsMarkup = {
        htmlToQems: htmlToQemsMarkup,
        isRichHtml: function (html) { return isRichHtml(html); },
        cleanPastedQems: cleanPastedQems,
        autoMarkPgTargets: autoMarkPgTargets,
        parenIsGuide: parenIsGuide,
        guidesRequireQuotes: guidesRequireQuotes
    };

    /**
     * Detect whether clipboard HTML looks like it came from a rich-text
     * source (Word, Google Docs, etc.) rather than plain text wrapped in HTML.
     */
    function isRichHtml(html) {
        if (!html) return false;
        // If it contains formatting tags or Word/Docs markers, it's rich
        return (/<(b|strong|i|em|u|sup|sub|span)\b/i.test(html) ||
                /class="?Mso/i.test(html) ||
                /docs-internal/i.test(html));
    }

    /**
     * Attach paste handler to a textarea element.
     * Uses a guard flag to prevent duplicate handlers.
     */
    function attachPasteHandler(textarea) {
        if (textarea._pasteHandlerAttached) return;
        textarea._pasteHandlerAttached = true;

        textarea.addEventListener('paste', function (e) {
            var clipboardData = e.clipboardData || window.clipboardData;
            if (!clipboardData) return;

            var html = clipboardData.getData('text/html');
            if (!isRichHtml(html)) return;

            // Prevent default paste and insert converted text
            e.preventDefault();
            var converted = cleanPastedQems(htmlToQemsMarkup(html))
                .replace(/\u00a0/g, ' ')        // non-breaking spaces → normal spaces
                .replace(/\n{3,}/g, '\n\n')     // 3+ newlines → max 2
                .trim();

            // Question field textareas (class "expanding") should be single
            // paragraphs — collapse all line breaks to spaces.
            if ($(textarea).hasClass('expanding')) {
                converted = converted.replace(/\n+/g, ' ').replace(/ {2,}/g, ' ');
            }

            // Insert at cursor position
            var start = textarea.selectionStart;
            var end = textarea.selectionEnd;
            var value = textarea.value;
            textarea.value = value.substring(0, start) + converted + value.substring(end);
            textarea.selectionStart = textarea.selectionEnd = start + converted.length;

            // Trigger input event so expanding-textareas updates
            $(textarea).trigger('input').trigger('change');

            // Show conversion status message
            var $ta = $(textarea);
            var $status = $ta.next('.paste-status');
            if (!$status.length) {
                $status = $('<div class="paste-status" style="color:#2a7a2a; font-size:0.85em; margin-top:2px;"></div>');
                $ta.after($status);
            }
            $status.text('Converted from rich text').show();
            setTimeout(function () { $status.fadeOut(); }, 3000);
        });
    }

    // Attach to all expanding textareas on the page
    $('textarea.expanding').each(function () {
        attachPasteHandler(this);
    });

    // Also attach to any textarea inside .expanding wrapper (the plugin wraps them)
    $('div.expanding textarea').each(function () {
        attachPasteHandler(this);
    });

    // ========================================================================
    // 2. Unified Bonus Editor
    // ========================================================================

    var $toggleBtn = $('#toggle-unified-editor');
    var $unifiedContainer = $('#unified-editor-container');
    var $unifiedTextarea = $('#unified-bonus-text');
    var $individualContainer = $('#individual-fields-container');
    var unifiedMode = false;

    if ($toggleBtn.length) {
        $toggleBtn.on('click', function (e) {
            e.preventDefault();
            if (!unifiedMode) {
                switchToUnified();
            } else {
                switchToIndividual();
            }
        });

        // Also attach paste handler to unified textarea
        if ($unifiedTextarea.length) {
            attachPasteHandler($unifiedTextarea[0]);
        }

        // On form submit, sync unified → individual fields if in unified mode
        $unifiedContainer.closest('form').on('submit', function () {
            if (unifiedMode) {
                parseUnifiedToFields();
            }
        });

        // Default to the unified editor view (whole-bonus rich text). Populate
        // the textarea from the saved fields now, before rich_editor.js enhances
        // it (this script runs first), so the rich editor renders the content.
        if ($unifiedContainer.length && $unifiedTextarea.length) {
            unifiedMode = true;
            $unifiedTextarea.val(fieldsToUnified());
            $individualContainer.hide();
            $unifiedContainer.show();
            $toggleBtn.text('Switch to Individual Fields');
        }
    }

    // Exposed so the bonus page's difficulty-warning check can pull the
    // [10e]/[10m]/[10h] difficulties out of the unified text into the fields
    // BEFORE it validates them (it runs before this script's submit handler).
    window.qemsSyncBonusUnified = function () {
        if (unifiedMode && $unifiedTextarea && $unifiedTextarea.length) {
            parseUnifiedToFields();
        }
    };

    // The other direction: something changed a per-part field behind the
    // unified view (a style fix applied to the saved question), so rebuild the
    // unified text from the fields and push it into the rich editor. Without
    // this the fix sat in a hidden field and the next save parsed the stale
    // unified text straight back over it. Returns the editor to flash, if any.
    window.qemsRefreshBonusUnified = function () {
        if (!(unifiedMode && $unifiedTextarea && $unifiedTextarea.length)) { return null; }
        $unifiedTextarea.val(fieldsToUnified()).trigger('change').trigger('input');
        return $unifiedTextarea;
    };

    function fieldsToUnified() {
        var leadin = ($('#id_leadin').val() || '').trim();
        var parts = [];
        for (var i = 1; i <= 3; i++) {
            var text = ($('#id_part' + i + '_text').val() || '').trim();
            var answer = ($('#id_part' + i + '_answer').val() || '').trim();
            var diff = ($('#id_part' + i + '_difficulty').val() || '');
            if (text || answer) {
                parts.push('[10' + diff + '] ' + text + '\nANSWER: ' + answer);
            }
        }
        var lines = [];
        if (leadin) {
            lines.push(leadin);
        }
        lines = lines.concat(parts);
        return lines.join('\n');
    }

    function parseUnifiedToFields() {
        var text = ($unifiedTextarea.val() || '').trim();
        if (!text) return;

        var lines = text.split('\n');
        var leadin = '';
        var parts = []; // each: {text: '', answer: '', difficulty: ''}
        var currentPart = null;

        for (var i = 0; i < lines.length; i++) {
            var line = lines[i];
            var partMatch = line.match(/^\[\d+([emh]?)\]\s*(.*)/);
            var answerMatch = line.match(/^ANSWER:\s*(.*)/i);

            if (partMatch) {
                // Start a new part, capture difficulty suffix
                currentPart = { text: partMatch[2], answer: '', difficulty: partMatch[1] };
                parts.push(currentPart);
            } else if (answerMatch) {
                if (currentPart) {
                    currentPart.answer = answerMatch[1];
                    currentPart = null; // done with this part
                }
            } else if (parts.length === 0 && !partMatch) {
                // Before any [10] marker → leadin
                leadin += (leadin ? '\n' : '') + line;
            } else if (currentPart) {
                // Continuation of part text
                currentPart.text += '\n' + line;
            }
        }

        $('#id_leadin').val(leadin);
        for (var j = 0; j < 3; j++) {
            var p = parts[j] || { text: '', answer: '', difficulty: '' };
            $('#id_part' + (j + 1) + '_text').val(p.text.trim());
            $('#id_part' + (j + 1) + '_answer').val(p.answer.trim());
            $('#id_part' + (j + 1) + '_difficulty').val(p.difficulty);
        }
    }

    function switchToUnified() {
        unifiedMode = true;
        $unifiedTextarea.val(fieldsToUnified());
        $individualContainer.hide();
        $unifiedContainer.show();
        $toggleBtn.text('Switch to Individual Fields');
        $unifiedTextarea.focus();
    }

    function switchToIndividual() {
        parseUnifiedToFields();
        unifiedMode = false;
        $unifiedContainer.hide();
        $individualContainer.show();
        $toggleBtn.text('Switch to Unified Editor');
    }

    // ========================================================================
    // 3. Paste Full Tossup Dialog
    // ========================================================================

    var $pasteBtn = $('#paste-full-tossup');
    if ($pasteBtn.length) {
        // Create dialog markup
        var $dialog = $('<div id="paste-tossup-dialog" title="Paste Full Tossup">' +
            '<p>Paste from Word or Google Docs &mdash; formatting will be auto-converted to QEMS markup. ' +
            'Separate the question and answer with <code>ANSWER:</code> on its own line or inline.</p>' +
            '<textarea id="paste-tossup-input" rows="12" style="width:100%; font-family:monospace; font-size:0.9em;"></textarea>' +
            '</div>');
        $('body').append($dialog);

        // Attach paste handler to the dialog textarea
        attachPasteHandler($dialog.find('textarea')[0]);

        $dialog.dialog({
            autoOpen: false,
            modal: true,
            width: 700,
            height: 450,
            buttons: {
                'Apply': function () {
                    applyPastedTossup();
                    $(this).dialog('close');
                },
                'Cancel': function () {
                    $(this).dialog('close');
                }
            },
            open: function () {
                // Clear and focus on open; re-attach paste handler for safety
                var ta = $('#paste-tossup-input')[0];
                attachPasteHandler(ta);
                $(ta).val('').focus();
            }
        });

        $pasteBtn.on('click', function (e) {
            e.preventDefault();
            $dialog.dialog('open');
        });
    }

    function applyPastedTossup() {
        var raw = ($('#paste-tossup-input').val() || '').trim();
        if (!raw) return;

        // Split at ANSWER: (case-insensitive)
        var idx = raw.search(/\nANSWER:\s*/i);
        var questionText, answerText;
        if (idx !== -1) {
            questionText = raw.substring(0, idx).trim();
            answerText = raw.substring(idx).replace(/^\nANSWER:\s*/i, '').trim();
        } else {
            // Try inline ANSWER:
            idx = raw.search(/ANSWER:\s*/i);
            if (idx !== -1) {
                questionText = raw.substring(0, idx).trim();
                answerText = raw.substring(idx).replace(/^ANSWER:\s*/i, '').trim();
            } else {
                // No ANSWER: found — put everything in question text
                questionText = raw;
                answerText = '';
            }
        }

        // Tossup fields are single-paragraph — collapse line breaks to spaces
        questionText = questionText.replace(/\n+/g, ' ').replace(/ {2,}/g, ' ');
        answerText = answerText.replace(/\n+/g, ' ').replace(/ {2,}/g, ' ');

        $('#id_tossup_text').val(questionText).trigger('input').trigger('change');
        $('#id_tossup_answer').val(answerText).trigger('input').trigger('change');
    }

    // ========================================================================
    // 4. Copy for Discord Playtest Bot
    // ========================================================================

    /**
     * Convert QEMS markup to Discord markdown.
     *   _text_   (bold+underline) → __**text**__
     *   __text__ (underline only) → __text__  (unchanged)
     *   ~text~   (italic)         → _text_
     */
    function qemsToDiscordMarkup(text) {
        if (!text) return '';

        // 1. Temporarily replace double-underscore pairs so they don't get
        //    caught by the single-underscore replacement below.
        var doubleUnderscore = [];
        text = text.replace(/__([^_]+)__/g, function (m, p1) {
            doubleUnderscore.push(p1);
            return '\x00DU' + (doubleUnderscore.length - 1) + '\x00';
        });

        // 2. Single underscores (bold+underline) → __**text**__
        text = text.replace(/_([^_\x00]+)_/g, '__**$1**__');

        // 3. Restore double-underscore (underline only) — same in Discord
        text = text.replace(/\x00DU(\d+)\x00/g, function (m, idx) {
            return '__' + doubleUnderscore[parseInt(idx)] + '__';
        });

        // 4. Tildes (italic) → Discord italic
        text = text.replace(/~([^~]+)~/g, '_$1_');

        // 4b. Bold-only \Btext\B → **text**
        text = text.replace(/\\B([^\\]+)\\B/g, '**$1**');

        // 5. Superscript — no Discord equivalent, just keep text
        text = text.replace(/\\S([^\\]+)\\S/g, '$1');

        // 6. Subscript — no Discord equivalent, just keep text
        text = text.replace(/\\s([^\\]+)\\s/g, '$1');

        // 7. Pronunciation-guide target markers — annotation only, dropped
        text = text.replace(/\\P/g, '');

        // 8. Notes to the moderator/players → Discord italic. The words are read
        //    aloud, so they stay; only the marker becomes formatting.
        text = text.replace(/\\N([\s\S]+?)\\N/g, '_$1_');
        text = text.replace(/\\N/g, '');

        return text;
    }

    /**
     * Split text into sentence-level chunks for individual spoiler blocks.
     * Splits after sentence-ending punctuation followed by whitespace.
     */
    function splitIntoSentences(text) {
        if (!text) return [];
        // A sentence ends at . ! or ?, possibly followed by a closing quote,
        // bracket or Discord markup ("trefoil." / end.** / done.”), and then
        // whitespace or the end of the text. Positions are tracked, not
        // re-derived from joined match lengths: the old version could skip a
        // stretch it failed to match and then cut the "remainder" mid-word,
        // repeating the giveaway in a second spoiler.
        var re = /[.!?]+["”’'\)\]*_]*(?=\s+|$)/g;
        var result = [], last = 0, m;
        while ((m = re.exec(text)) !== null) {
            var end = m.index + m[0].length;
            var chunk = text.substring(last, end);
            // An initial ("John H. Conway") is not the end of a sentence.
            if (/(^|\s)[A-Z]\.$/.test(chunk.trim())) { continue; }
            var s = chunk.trim();
            if (s) { result.push(s); }
            last = end;
        }
        var remainder = text.substring(last).trim();
        if (remainder) { result.push(remainder); }
        return result;
    }

    // A spoiler a reader has to open is worth roughly a clue. Past this a
    // single reveal is a paragraph, which is what the sentence-only split gave
    // for the long ones.
    var SPOILER_CHUNK_MAX = 160;
    // Below this a piece reads as a fragment rather than a clue, so it is kept
    // with its neighbour instead of standing alone.
    var SPOILER_CHUNK_MIN = 45;

    /**
     * Whether Discord's inline markup is closed in this piece of text.
     *
     * A chunk cut inside **bold** would leave the opening ** in one spoiler
     * and its partner in the next, and Discord prints both literally.
     */
    function markupBalanced(text) {
        function count(hay, needle) { return hay.split(needle).length - 1; }
        // Blank the doubled forms first so the leftover single characters can
        // be counted without them.
        var singles = text.replace(/\*\*/g, '\u0000').replace(/__/g, '\u0001');
        return count(text, '**') % 2 === 0 &&
               count(text, '__') % 2 === 0 &&
               count(singles, '*') % 2 === 0 &&
               count(singles, '_') % 2 === 0;
    }

    /**
     * Cut one over-long sentence at its clause breaks.
     *
     * Greedy: keep adding clauses until the next one would take the piece past
     * SPOILER_CHUNK_MAX. A sentence with nowhere sensible to break is left
     * whole -- hard-wrapping prose mid-clause reads worse than a long reveal.
     */
    function splitLongSentence(sentence) {
        if (sentence.length <= SPOILER_CHUNK_MAX) { return [sentence]; }
        // After a comma, semicolon or colon; before a dash, which introduces
        // what follows it rather than ending what precedes it.
        var re = /[;:,]\s+|\s+(?=[\u2014-]{1,2}\s)/g;
        var points = [], m;
        while ((m = re.exec(sentence)) !== null) {
            points.push(m.index + m[0].length);
        }
        if (!points.length) { return [sentence]; }

        var chunks = [], start = 0;
        for (var i = 0; i < points.length; i++) {
            var here = points[i];
            var next = (i + 1 < points.length) ? points[i + 1] : sentence.length;
            var piece = sentence.substring(start, here);
            if (next - start > SPOILER_CHUNK_MAX &&
                    piece.trim().length >= SPOILER_CHUNK_MIN &&
                    markupBalanced(piece)) {
                chunks.push(piece.trim());
                start = here;
            }
        }
        var tail = sentence.substring(start).trim();
        if (tail) {
            if (chunks.length && tail.length < SPOILER_CHUNK_MIN) {
                chunks[chunks.length - 1] += ' ' + tail;
            } else {
                chunks.push(tail);
            }
        }
        return chunks.length ? chunks : [sentence];
    }

    /**
     * The pieces a question's text is spoilered in, as
     * [{text, sentence}] -- `sentence` being which sentence the piece came
     * from, so anything decided per sentence (the readable opening) still
     * covers all of that sentence's pieces.
     */
    function splitIntoSpoilerChunks(text) {
        var out = [];
        splitIntoSentences(text).forEach(function (sentence, si) {
            splitLongSentence(sentence).forEach(function (piece) {
                out.push({ text: piece, sentence: si });
            });
        });
        return out;
    }

    /**
     * Get author and category from the form select fields.
     */
    function getAuthorAndCategory() {
        var author = '';
        var category = '';
        var $author = $('#id_author');
        var $category = $('#id_category');
        if ($author.length) {
            author = $author.find('option:selected').text().trim();
            if (author === '---------') author = '';
        }
        if ($category.length) {
            category = $category.find('option:selected').text().trim();
            if (category === '---------') category = '';
        }
        return { author: author, category: category };
    }

    /**
     * Format a tossup for the Discord playtest bot.
     * - Each sentence is individually spoiler-tagged with ||...||
     * - Text before (*) is bold-wrapped with **...**
     * - Answer is spoiler-tagged
     */
    // A question id tag appended to the last answer line so Discord bots can
    // link a pasted question back to the server even as its text changes.
    function qidSuffix(qid) {
        return qid ? ' <qid:' + qid + '>' : '';
    }

    // Whether the set wants a question's opening left readable in a spoilered
    // copy, so a playtest channel can see what a question is about without
    // opening it. For a tossup that is its first sentence; for a bonus, the
    // leadin and the first part (never an answer).
    //
    // Tossups used to be exempt: the playtest bot built a tossup's clue list
    // out of the ||spoilered|| runs alone, so an unspoilered opening sentence
    // was not merely unhidden, it was dropped -- which shifted every buzz index
    // and skewed the buzz percentages computed from what was left. The bot
    // handles an unspoilered opening now, so the setting means what it says for
    // both question types.
    //
    // The page sets the flag from the set's settings; absent, everything is
    // spoilered.
    // The author select's label is "Real Name (username)"; the Discord footer
    // wants the name alone. A bare username (no real name set) has no
    // parenthetical and passes through.
    function authorDisplayName(author) {
        return (author || '').replace(/\s*\([^()]*\)\s*$/, '').trim();
    }

    function showFirstClue() {
        return !!window.qemsDiscordShowFirst;
    }
    function spoil(text, isFirst) {
        return (isFirst && showFirstClue()) ? text : '||' + text + '||';
    }

    // Bold a stretch of the power region.
    //
    // Discord cannot nest ** inside **, and the region can already contain
    // some: QEMS's _x_ becomes __**x**__ and \Bx\B becomes **x**. Inside a
    // bolded region both are redundant -- the text is bold either way -- so the
    // inner pair is dropped and the underline, which is not redundant, stays.
    function boldPowerText(text) {
        return '**' + text.replace(/\*\*/g, '') + '**';
    }

    // How many sentences at the front of the question are only a note to the
    // moderator or players. A note is not a clue -- it is what the reader says
    // before the question starts -- so it must not use up the one sentence the
    // set chose to leave readable, or the opening clue ends up spoilered and
    // the readable part says nothing about the answer.
    function leadingNoteSentences(raw) {
        var m = /^\s*(?:\\N[\s\S]*?\\N\s*)+/.exec(raw || '');
        if (!m) { return 0; }
        return splitIntoSentences(qemsToDiscordMarkup(m[0])).length;
    }

    function formatTossupForDiscord(text, answer, author, category, qid) {
        text = (text || '').trim();
        answer = (answer || '').trim();
        var info = { author: authorDisplayName(author), category: category || '' };

        var discordAnswer = qemsToDiscordMarkup(answer);
        var noteLead = leadingNoteSentences(text);
        // Bold runs to the last power mark: a (+) superpower precedes the (*) power.
        var powerIdx = Math.max(text.indexOf('(*)'), text.indexOf('(+)'));
        var hasPower = powerIdx !== -1;
        var result = '';

        if (hasPower) {
            var beforePower = text.substring(0, powerIdx + 3); // include (*)
            var afterPower = text.substring(powerIdx + 3).trim();

            var beforeChunks = splitIntoSpoilerChunks(qemsToDiscordMarkup(beforePower));
            var afterChunks = splitIntoSpoilerChunks(qemsToDiscordMarkup(afterPower));

            // Pre-power: bold + spoiler. The readable opening is the first
            // clue -- any note in front of it comes along, since it is not a
            // clue and hiding it would tell a reader nothing. The bold run is
            // unaffected either way: power is about scoring, not hiding.
            // Bold each chunk inside its own spoiler rather than wrapping the
            // whole run: Discord parses ||...|| as a node of its own, and a **
            // spanning several of them mis-pairs -- the bold escaped past the
            // power mark and swallowed a ** from the answer line.
            result = beforeChunks.map(function (c) {
                return spoil(boldPowerText(c.text), c.sentence <= noteLead);
            }).join(' ');

            // Post-power: spoiler only, and never the opening.
            if (afterChunks.length > 0) {
                result += ' ' + afterChunks.map(function (c) {
                    return spoil(c.text, false);
                }).join(' ');
            }
        } else {
            var chunks = splitIntoSpoilerChunks(qemsToDiscordMarkup(text));
            result = chunks.map(function (c) {
                return spoil(c.text, c.sentence <= noteLead);
            }).join(' ');
        }

        result += '\nANSWER: ||' + discordAnswer + '||' + qidSuffix(qid);
        if (info.author || info.category) {
            result += '\n<' + info.author + ', ' + info.category + '>';
        }
        result += '\n!t';
        return result;
    }

    /**
     * Format a bonus for the Discord playtest bot.
     * - Leadin is plain text
     * - Part 1 text is plain, parts 2-3 text are spoilered
     * - All answers are spoilered
     * - Difficulty placeholder appended
     */
    function formatBonusForDiscord(leadin, parts, author, category, qid) {
        var info = { author: authorDisplayName(author), category: category || '' };
        // The leadin and first part are the "first clue" of a bonus; the
        // first answer never is. Unlike a tossup, a bonus is read whole, so
        // the bot has no clue list to lose text from.
        var leadinText = qemsToDiscordMarkup((leadin || '').trim());
        var result = (leadinText ? spoil(leadinText, true) : '') + '\n';

        var difficulties = [];
        for (var i = 1; i <= 3; i++) {
            var part = parts[i - 1] || {};
            var partText = qemsToDiscordMarkup((part.text || '').trim());
            var partAnswer = qemsToDiscordMarkup((part.answer || '').trim());
            var diff = (part.diff || '');

            // Plain '[10]': the difficulty letter would tell a player how hard
            // the part is before they hear it. The set of difficulties still
            // goes out, spoilered, on the line under the author.
            var label = '[10]';
            result += label + ' ' + spoil(partText, i === 1) + '\n';
            result += 'ANSWER: ||' + partAnswer + '||' + (i === 3 ? qidSuffix(qid) : '') + '\n';
            difficulties.push(diff || '?');
        }

        if (info.author || info.category) {
            result += '<' + info.author + ', ' + info.category + '>';
        }
        result += '\n||' + difficulties.join('/') + '||';
        result += '\n!t';
        return result;
    }

    /**
     * Copy text to clipboard and show brief confirmation.
     */
    function copyToClipboard(text, $button) {
        var originalText = $button.text();
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(function () {
                $button.text('Copied!');
                setTimeout(function () { $button.text(originalText); }, 2000);
            }, function () {
                fallbackCopy(text, $button, originalText);
            });
        } else {
            fallbackCopy(text, $button, originalText);
        }
    }

    function fallbackCopy(text, $button, originalText) {
        var $temp = $('<textarea>').val(text).appendTo('body').select();
        try {
            document.execCommand('copy');
            $button.text('Copied!');
        } catch (e) {
            $button.text('Copy failed');
        }
        $temp.remove();
        setTimeout(function () { $button.text(originalText); }, 2000);
    }

    // ========================================================================
    // 4a. Copy full question (rich text)
    // ========================================================================

    /**
     * QEMS markup -> display HTML for the clipboard, mirroring the server-side
     * renderer: escaped literals (\_ \~ \( \)) resolve to their plain
     * character (unlike the rich editor, which keeps them visible for
     * round-tripping).
     */
    function qemsToCopyHtml(text) {
        var html = (text || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        html = html.replace(/\\_/g, '\x00US\x00').replace(/\\~/g, '\x00TI\x00')
                   .replace(/\\\(/g, '\x00OP\x00').replace(/\\\)/g, '\x00CP\x00');
        html = html.replace(/__([^_]+)__/g, '<u>$1</u>');
        html = html.replace(/_([^_]+)_/g, '<u><b>$1</b></u>');
        html = html.replace(/~([^~]+)~/g, '<i>$1</i>');
        html = html.replace(/\\B([\s\S]+?)\\B/g, '<b>$1</b>');
        html = html.replace(/\\S([\s\S]+?)\\S/g, '<sup>$1</sup>');
        html = html.replace(/\\s([\s\S]+?)\\s/g, '<sub>$1</sub>');
        // Pronunciation-guide target word(s): the same teal the app renders.
        html = html.replace(/\\P([\s\S]+?)\\P/g, '<span style="color:#0b7285">$1</span>');
        html = html.replace(/\x00US\x00/g, '_').replace(/\x00TI\x00/g, '~')
                   .replace(/\x00OP\x00/g, '(').replace(/\x00CP\x00/g, ')');
        return html.replace(/\n/g, '<br>');
    }

    /** Question text with the power (everything up to the last mark) bolded. */
    function qemsQuestionTextToHtml(text) {
        text = text || '';
        var powerIdx = Math.max(text.indexOf('(*)'), text.indexOf('(+)'));
        if (powerIdx === -1) { return qemsToCopyHtml(text); }
        return '<b>' + qemsToCopyHtml(text.substring(0, powerIdx + 3)) + '</b>'
             + qemsToCopyHtml(text.substring(powerIdx + 3));
    }

    /**
     * Copy with both a rich (text/html) and a plain flavor: rich-text targets
     * (Word, Docs, email) get real bold/underline/italics; plain targets get
     * the raw QEMS markup. Falls back to plain-only where ClipboardItem is
     * unavailable.
     */
    function copyRichToClipboard(html, plain, $button) {
        if (navigator.clipboard && navigator.clipboard.write && window.ClipboardItem) {
            var originalText = $button.text();
            var item = new ClipboardItem({
                'text/html': new Blob([html], { type: 'text/html' }),
                'text/plain': new Blob([plain], { type: 'text/plain' })
            });
            navigator.clipboard.write([item]).then(function () {
                $button.text('Copied!');
                setTimeout(function () { $button.text(originalText); }, 2000);
            }, function () {
                copyToClipboard(plain, $button);
            });
        } else {
            copyToClipboard(plain, $button);
        }
    }

    var $copyFullTossup = $('#copy-full-tossup');
    if ($copyFullTossup.length) {
        $copyFullTossup.on('click', function (e) {
            e.preventDefault();
            var text = ($('#id_tossup_text').val() || '').trim();
            var answer = ($('#id_tossup_answer').val() || '').trim();
            var plain = text + '\nANSWER: ' + answer;
            var html = '<p>' + qemsQuestionTextToHtml(text) + '</p>'
                     + '<p>ANSWER: ' + qemsToCopyHtml(answer) + '</p>';
            copyRichToClipboard(html, plain, $copyFullTossup);
        });
    }

    var $copyFullBonus = $('#copy-full-bonus');
    if ($copyFullBonus.length) {
        $copyFullBonus.on('click', function (e) {
            e.preventDefault();
            if (window.qemsSyncBonusUnified) { window.qemsSyncBonusUnified(); }
            var leadin = ($('#id_leadin').val() || '').trim();
            var plainLines = [leadin];
            var htmlParts = ['<p>' + qemsToCopyHtml(leadin) + '</p>'];
            for (var i = 1; i <= 3; i++) {
                var partText = ($('#id_part' + i + '_text').val() || '').trim();
                var partAnswer = ($('#id_part' + i + '_answer').val() || '').trim();
                var diff = ($('#id_part' + i + '_difficulty').val() || '');
                if (!partText && !partAnswer) { continue; }
                plainLines.push('[10' + diff + '] ' + partText);
                plainLines.push('ANSWER: ' + partAnswer);
                htmlParts.push('<p>[10' + diff + '] ' + qemsToCopyHtml(partText) + '<br>'
                             + 'ANSWER: ' + qemsToCopyHtml(partAnswer) + '</p>');
            }
            copyRichToClipboard(htmlParts.join(''), plainLines.join('\n'), $copyFullBonus);
        });
    }

    // The saved question's id, read from the preview panel's anchor region
    // (present on the edit pages once a question exists; blank for a new one).
    function domQuestionId() {
        var $a = $('.anchor-region[data-question-id]').first();
        return ($a.length ? $a.attr('data-question-id') : '') || '';
    }

    function domTossupArgs() {
        var info = getAuthorAndCategory();
        return [($('#id_tossup_text').val() || ''), ($('#id_tossup_answer').val() || ''), info.author, info.category, domQuestionId()];
    }

    function domBonusArgs() {
        // The unified editor holds the bonus until submit; on Add Bonus the
        // per-part fields are empty until then, which copied "[10]" and
        // nothing else. Pull the unified text down first.
        if (window.qemsSyncBonusUnified) { window.qemsSyncBonusUnified(); }
        var info = getAuthorAndCategory();
        var parts = [];
        for (var i = 1; i <= 3; i++) {
            parts.push({
                text: ($('#id_part' + i + '_text').val() || ''),
                answer: ($('#id_part' + i + '_answer').val() || ''),
                diff: ($('#id_part' + i + '_difficulty').val() || '')
            });
        }
        return [($('#id_leadin').val() || ''), parts, info.author, info.category, domQuestionId()];
    }

    // Expose the formatters for other pages (e.g. the packet document view)
    window.QemsDiscord = {
        tossup: formatTossupForDiscord,
        tossupPlain: formatTossupForDiscordPlain,
        bonus: formatBonusForDiscord,
        bonusPlain: formatBonusForDiscordPlain,
        copy: copyToClipboard
    };

    // Wire up "Copy for Discord" buttons
    var $copyTossupBtn = $('#copy-for-discord-tossup');
    if ($copyTossupBtn.length) {
        $copyTossupBtn.on('click', function (e) {
            e.preventDefault();
            var formatted = formatTossupForDiscord.apply(null, domTossupArgs());
            copyToClipboard(formatted, $copyTossupBtn);
        });
    }

    var $copyBonusBtn = $('#copy-for-discord-bonus');
    if ($copyBonusBtn.length) {
        $copyBonusBtn.on('click', function (e) {
            e.preventDefault();
            var formatted = formatBonusForDiscord.apply(null, domBonusArgs());
            copyToClipboard(formatted, $copyBonusBtn);
        });
    }

    // ========================================================================
    // 5. Copy for Discord (No Spoilers)
    // ========================================================================

    /**
     * Format a tossup for Discord without spoiler tags.
     * Clean Discord markdown with category + author for sharing finished questions.
     */
    function formatTossupForDiscordPlain(text, answer, author, category, qid) {
        var info = { author: authorDisplayName(author), category: category || '' };

        var rendered = qemsToDiscordMarkup((text || '').trim());
        // Bold the power: everything up to and including the last power marker
        // (a (+) superpower precedes the (*) power).
        var powerIdx = Math.max(rendered.indexOf('(*)'), rendered.indexOf('(+)'));
        var result;
        if (powerIdx !== -1) {
            result = '**' + rendered.substring(0, powerIdx + 3) + '**' + rendered.substring(powerIdx + 3);
        } else {
            result = rendered;
        }
        answer = (answer || '').trim();
        result += '\nANSWER: ' + qemsToDiscordMarkup(answer) + qidSuffix(qid);
        if (info.author || info.category) {
            result += '\n<' + info.author + ', ' + info.category + '>';
        }
        return result;
    }

    /**
     * Format a bonus for Discord without spoiler tags.
     */
    function formatBonusForDiscordPlain(leadin, parts, author, category, qid) {
        var info = { author: authorDisplayName(author), category: category || '' };
        var result = qemsToDiscordMarkup((leadin || '').trim()) + ' For 10 points each:\n';

        for (var i = 1; i <= 3; i++) {
            var part = parts[i - 1] || {};
            var partText = qemsToDiscordMarkup((part.text || '').trim());
            var partAnswer = qemsToDiscordMarkup((part.answer || '').trim());
            var diff = (part.diff || '');
            result += '[10' + diff + '] ' + partText + '\n';
            result += 'ANSWER: ' + partAnswer + (i === 3 ? qidSuffix(qid) : '') + '\n';
        }

        if (info.author || info.category) {
            result += '<' + info.author + ', ' + info.category + '>';
        }
        return result;
    }

    // Wire up "Copy for Discord (no spoilers)" buttons
    var $copyPlainTossupBtn = $('#copy-for-discord-plain-tossup');
    if ($copyPlainTossupBtn.length) {
        $copyPlainTossupBtn.on('click', function (e) {
            e.preventDefault();
            var formatted = formatTossupForDiscordPlain.apply(null, domTossupArgs());
            copyToClipboard(formatted, $copyPlainTossupBtn);
        });
    }

    var $copyPlainBonusBtn = $('#copy-for-discord-plain-bonus');
    if ($copyPlainBonusBtn.length) {
        $copyPlainBonusBtn.on('click', function (e) {
            e.preventDefault();
            var formatted = formatBonusForDiscordPlain.apply(null, domBonusArgs());
            copyToClipboard(formatted, $copyPlainBonusBtn);
        });
    }

    // ========================================================================
    // 6. Unsaved Changes Warning for Comments
    // ========================================================================

    // Snapshot initial values of the question edit form (the first form on page)
    var $editForm = $('form.clearfix').first();
    var initialValues = {};
    if ($editForm.length) {
        $editForm.find('input, textarea, select').each(function () {
            var $el = $(this);
            var name = $el.attr('name');
            if (!name) return;
            if ($el.is(':checkbox')) {
                initialValues[name] = $el.prop('checked');
            } else {
                initialValues[name] = $el.val();
            }
        });
    }

    function hasUnsavedChanges() {
        if (!$editForm.length) return false;
        var dirty = false;
        $editForm.find('input, textarea, select').each(function () {
            var $el = $(this);
            var name = $el.attr('name');
            if (!name || !(name in initialValues)) return;
            if ($el.is(':checkbox')) {
                if ($el.prop('checked') !== initialValues[name]) dirty = true;
            } else {
                if ($el.val() !== initialValues[name]) dirty = true;
            }
        });
        return dirty;
    }

    // Warn before posting a top-level comment if there are unsaved question changes
    $(document).on('submit', 'form:has(input[name="next"])', function (e) {
        if (hasUnsavedChanges()) {
            if (!confirm('You have unsaved changes to this question. Post comment anyway?')) {
                e.preventDefault();
                return false;
            }
        }
    });

    // ========================================================================
    // 7. Comment Reply UI
    // ========================================================================

    // Re-render the current page via a GET navigation rather than location.reload():
    // on a page that was rendered by a POST (e.g. right after saving a question),
    // reload() repeats the POST and triggers the browser's "resend information?" prompt.
    function qemsGetReload() {
        window.location.assign(window.location.pathname + window.location.search);
    }

    // Refresh just the comments panel (edit tossup/bonus pages) in place so
    // adding/replying/resolving doesn't reload the whole page. Re-fetches the
    // current page and swaps in the server-rendered .comments markup (keeping
    // fidelity and the delegated action handlers). Returns true if it handled
    // the refresh; false (no .comments panel) lets callers fall back to a full
    // GET reload — doc view, packet grid, etc. don't have this sidebar.
    function qemsRefreshComments(done) {
        // The edit pages keep comments in a sidebar; the doc view has the same
        // thread markup in its packet-comments panel. Either can refresh in place.
        var SEL = '.edit-comments .comments, #packet-comments-body .comments';
        var $panel = $(SEL).first();
        if (!$panel.length) { return false; }
        $.get(window.location.pathname + window.location.search, function (html) {
            var $fresh = $(html).find(SEL).first();
            if ($fresh.length) { $panel.html($fresh.html()); }
            else { qemsGetReload(); }
            if (typeof done === 'function') { done(); }
        }).fail(function () { qemsGetReload(); });
        return true;
    }
    window.qemsRefreshComments = qemsRefreshComments;

    // Post a new top-level comment (tossup/bonus/packet) via AJAX. Avoids the
    // django_comments security form, whose timestamp expires on a long-open tab
    // (which produced a "bad request" when adding a comment).
    $(document).on('click', '.comment-submit', function (e) {
        e.preventDefault();
        var $form = $(this).closest('.add-comment-form');
        var text = $.trim($form.find('.new-comment-text').val());
        if (!text) { return; }
        var $btn = $(this);
        $btn.prop('disabled', true);
        $.post('/post_comment/', {
            target_type: $form.data('target-type'),
            target_id: $form.data('target-id'),
            qset_id: $form.data('qset-id'),
            comment_text: text
        }, function (response) {
            var json = $.parseJSON(response);
            if (json.success) {
                $form.find('.new-comment-text').val('');
                if (!qemsRefreshComments()) { qemsGetReload(); }
                $btn.prop('disabled', false);
            }
            else { alert(json.message || 'Could not post comment.'); $btn.prop('disabled', false); }
        }).fail(function () { $btn.prop('disabled', false); });
    });

    // Ctrl/Cmd+Enter in any comment box submits it (finds the nearby Post button).
    $(document).on('keydown',
        'textarea[name="comment"], textarea.reply-text, textarea.new-comment-text, textarea.edit-comment-text, .doc-comment-box textarea',
        function (e) {
            if (!(e.ctrlKey || e.metaKey)) { return; }
            if (e.key !== 'Enter' && e.keyCode !== 13) { return; }
            e.preventDefault();
            var $ta = $(this), $btn = $();
            if ($ta.closest('.add-comment-form').length) { $btn = $ta.closest('.add-comment-form').find('.comment-submit'); }
            else if ($ta.closest('.comment-edit-form').length) { $btn = $ta.closest('.comment-edit-form').find('.save-comment-edit'); }
            else if ($ta.closest('.reply-form').length) { $btn = $ta.closest('.reply-form').find('.post-reply'); }
            else if ($ta.closest('.doc-comment-box').length) {
                // Doc view: new comment, reply, and edit boxes all share this class.
                $btn = $ta.closest('.doc-comment-box')
                          .find('.doc-comment-post, .doc-reply-post, .doc-edit-save');
            }
            else { $btn = $ta.closest('form').find('input[type=submit], button[type=submit]'); }
            $btn.first().trigger('click');
        });

    // Toggle a comment's resolved status
    $(document).on('click', '.resolve-toggle', function (e) {
        e.preventDefault();
        var id = $(this).data('comment-id');
        $.post('/resolve_comment/', { comment_id: id }, function () {
            if (!qemsRefreshComments()) { qemsGetReload(); }
        });
    });

    // Toggle reply form visibility
    $(document).on('click', '.reply-toggle', function (e) {
        e.preventDefault();
        var commentId = $(this).data('comment-id');
        var $form = $('#reply-form-' + commentId);
        $form.toggle();
        if ($form.is(':visible')) {
            $form.find('.reply-text').focus();
        }
    });

    // Cancel reply
    $(document).on('click', '.cancel-reply', function (e) {
        e.preventDefault();
        var $form = $(this).closest('.reply-form');
        $form.find('.reply-text').val('');
        $form.hide();
    });

    // ---- Edit your own comment, in place ----
    // The rendered comment carries the raw markup in data-raw, so the editor
    // starts from what was typed rather than the formatted HTML.
    function commentTextEl($item) {
        return $item.find('.comment-body, .comment-strikeable').first();
    }

    $(document).on('click', '.edit-comment-toggle', function (e) {
        e.preventDefault();
        var $item = $(this).closest('.comment-item');
        if ($item.find('> .comment-edit-form').length) { return; }
        var $text = commentTextEl($item);
        var $form = $('<div class="comment-edit-form">' +
            '<textarea rows="3" class="edit-comment-text"></textarea>' +
            '<button class="button small primary save-comment-edit">Save</button> ' +
            '<button class="button small secondary cancel-comment-edit">Cancel</button></div>');
        $form.find('textarea').val($text.attr('data-raw') || $.trim($text.text()));
        $text.hide();
        $(this).closest('.comment-actions').after($form);
        $form.find('textarea').focus();
    });

    $(document).on('click', '.cancel-comment-edit', function (e) {
        e.preventDefault();
        var $form = $(this).closest('.comment-edit-form');
        commentTextEl($form.closest('.comment-item')).show();
        $form.remove();
    });

    $(document).on('click', '.save-comment-edit', function (e) {
        e.preventDefault();
        var $form = $(this).closest('.comment-edit-form');
        var $item = $form.closest('.comment-item');
        var text = $.trim($form.find('textarea').val());
        if (!text) { return; }
        var $btn = $(this).prop('disabled', true);
        $.post('/edit_comment/', { comment_id: $item.data('comment-id'), comment_text: text },
            function (response) {
                var json;
                try { json = (typeof response === 'string') ? JSON.parse(response) : response; }
                catch (err) { json = null; }
                if (!json || !json.success) {
                    alert((json && json.message) || 'Could not save the comment.');
                    $btn.prop('disabled', false);
                    return;
                }
                commentTextEl($item).attr('data-raw', json.text).html(json.html).show();
                $form.remove();
            }).fail(function () {
                alert('Could not save the comment.');
                $btn.prop('disabled', false);
            });
    });

    // ========================================================================
    // 8. @mention autocomplete for comment boxes
    // ========================================================================

    (function () {
        var qsetId = window.QEMS_QSET_ID;
        if (!qsetId) { return; }
        // Every box a comment can be written in, the anchored-comment popover
        // (edit_tossup / edit_bonus) included -- an @mention has to work
        // wherever the comment is typed, not only in the thread at the bottom.
        var SEL = 'textarea[name="comment"], textarea.reply-text, textarea.new-comment-text, textarea.edit-comment-text, textarea.anchor-comment-text, .doc-comment-box textarea';
        var members = null, loading = false;
        var $dd = $('<div class="mention-dropdown" style="display:none;"></div>').appendTo('body');
        var activeTa = null, matchStart = -1;

        function loadMembers(cb) {
            if (members) { cb(); return; }
            if (loading) { return; }
            loading = true;
            $.getJSON('/set_members/' + qsetId + '/', function (data) {
                members = (data && data.members) || [];
                loading = false;
                cb();
            });
        }
        function hide() { $dd.hide(); activeTa = null; matchStart = -1; }

        function showFor(ta) {
            var before = ta.value.slice(0, ta.selectionStart);
            var m = before.match(/@([\w.\-]*)$/);
            if (!m) { hide(); return; }
            var token = m[1].toLowerCase();
            matchStart = ta.selectionStart - m[0].length;
            var matches = members.filter(function (mem) {
                return mem.username.toLowerCase().indexOf(token) !== -1 ||
                       mem.name.toLowerCase().indexOf(token) !== -1;
            }).slice(0, 8);
            if (!matches.length) { hide(); return; }
            $dd.empty();
            matches.forEach(function (mem, i) {
                $('<div class="mention-item">')
                    .toggleClass('active', i === 0)
                    .attr('data-username', mem.username)
                    .html('<strong></strong> <span class="mention-username"></span>')
                    .find('strong').text(mem.name).end()
                    .find('.mention-username').text('@' + mem.username).end()
                    .appendTo($dd);
            });
            var off = $(ta).offset();
            $dd.css({ left: off.left, top: off.top + $(ta).outerHeight(),
                      minWidth: Math.max(220, $(ta).outerWidth()) }).show();
            activeTa = ta;
        }

        function pick($item) {
            if (!activeTa || !$item || !$item.length) { return; }
            var username = $item.attr('data-username');
            var ta = activeTa, val = ta.value, pos = ta.selectionStart;
            var insert = '@' + username + ' ';
            ta.value = val.slice(0, matchStart) + insert + val.slice(pos);
            var newPos = matchStart + insert.length;
            ta.selectionStart = ta.selectionEnd = newPos;
            $(ta).trigger('input');
            hide();
            ta.focus();
        }

        $(document).on('input', SEL, function () {
            var ta = this;
            loadMembers(function () { showFor(ta); });
        });
        $(document).on('keydown', SEL, function (e) {
            if (!$dd.is(':visible')) { return; }
            var $items = $dd.find('.mention-item');
            var idx = $items.index($items.filter('.active'));
            if (e.key === 'ArrowDown') { e.preventDefault(); idx = Math.min(idx + 1, $items.length - 1); }
            else if (e.key === 'ArrowUp') { e.preventDefault(); idx = Math.max(idx - 1, 0); }
            else if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); pick($items.eq(idx < 0 ? 0 : idx)); return; }
            else if (e.key === 'Escape') { hide(); return; }
            else { return; }
            $items.removeClass('active');
            $items.eq(idx).addClass('active');
        });
        $(document).on('mousedown', '.mention-item', function (e) {
            e.preventDefault();
            pick($(this));
        });
        $(document).on('blur', SEL, function () { setTimeout(hide, 150); });
    })();

    // Post reply via AJAX
    $(document).on('click', '.post-reply', function (e) {
        e.preventDefault();
        var $btn = $(this);
        var parentId = $btn.data('parent-id');
        var qsetId = $btn.data('qset-id');
        var $form = $btn.closest('.reply-form');
        var commentText = $form.find('.reply-text').val().trim();

        if (!commentText) return;

        if (hasUnsavedChanges()) {
            if (!confirm('You have unsaved changes to this question. Post reply anyway?')) {
                return;
            }
        }

        $.post('/reply_to_comment/', {
            parent_id: parentId,
            comment_text: commentText,
            qset_id: qsetId
        }, function (response) {
            var json_response = $.parseJSON(response);
            var ok = (json_response['message_class'] || '').indexOf('success') >= 0;
            // On success, refresh the comments panel in place (no page reload).
            if (ok && qemsRefreshComments()) { return; }
            var dialog = $('#info-dialog').dialog({
                modal: true,
                buttons: {
                    Ok: function () {
                        $(this).dialog('close');
                        if (!(ok && qemsRefreshComments())) { qemsGetReload(); }
                    }
                }
            });
            dialog.append('<div class="' + json_response['message_class'] + '">' + json_response['message'] + '</div>');
            dialog.dialog('open');
        });
    });
});
