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
            return m[1] + marker + m[2] + marker + m[3];
        }).join('\n');
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
        var isBold = (tag === 'b' || tag === 'strong' ||
                      style.indexOf('font-weight') !== -1 &&
                      (style.indexOf('bold') !== -1 || style.indexOf('700') !== -1));
        var isUnderline = (tag === 'u' ||
                          style.indexOf('text-decoration') !== -1 &&
                          style.indexOf('underline') !== -1);
        var isItalic = (tag === 'i' || tag === 'em' ||
                       style.indexOf('font-style') !== -1 &&
                       style.indexOf('italic') !== -1);
        var isSup = (tag === 'sup');
        var isSub = (tag === 'sub');

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

    // Expose the converter for other scripts (e.g. rich_editor.js)
    window.QemsMarkup = {
        htmlToQems: htmlToQemsMarkup,
        isRichHtml: function (html) { return isRichHtml(html); }
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
            var converted = htmlToQemsMarkup(html)
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

        return text;
    }

    /**
     * Split text into sentence-level chunks for individual spoiler blocks.
     * Splits after sentence-ending punctuation followed by whitespace.
     */
    function splitIntoSentences(text) {
        if (!text) return [];
        // Match runs of text that end with sentence-ending punctuation
        var matches = text.match(/[^.!?]*[.!?]+(?:\s+|$)/g);
        if (!matches) return [text];
        var result = [];
        for (var i = 0; i < matches.length; i++) {
            var s = matches[i].trim();
            if (s) result.push(s);
        }
        // If there's trailing text without punctuation, include it
        var joined = matches.join('');
        var remainder = text.substring(joined.length).trim();
        if (remainder) result.push(remainder);
        return result;
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

    function formatTossupForDiscord(text, answer, author, category, qid) {
        text = (text || '').trim();
        answer = (answer || '').trim();
        var info = { author: author || '', category: category || '' };

        var discordAnswer = qemsToDiscordMarkup(answer);
        var powerIdx = text.indexOf('(*)');
        var hasPower = powerIdx !== -1;
        var result = '';

        if (hasPower) {
            var beforePower = text.substring(0, powerIdx + 3); // include (*)
            var afterPower = text.substring(powerIdx + 3).trim();

            var beforeSentences = splitIntoSentences(qemsToDiscordMarkup(beforePower));
            var afterSentences = splitIntoSentences(qemsToDiscordMarkup(afterPower));

            // Pre-power: bold + spoiler
            result = '**' + beforeSentences.map(function (s) {
                return '||' + s + '||';
            }).join(' ') + '**';

            // Post-power: spoiler only
            if (afterSentences.length > 0) {
                result += ' ' + afterSentences.map(function (s) {
                    return '||' + s + '||';
                }).join(' ');
            }
        } else {
            var sentences = splitIntoSentences(qemsToDiscordMarkup(text));
            result = sentences.map(function (s) {
                return '||' + s + '||';
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
        var info = { author: author || '', category: category || '' };
        var result = qemsToDiscordMarkup((leadin || '').trim()) + '\n';

        var difficulties = [];
        for (var i = 1; i <= 3; i++) {
            var part = parts[i - 1] || {};
            var partText = qemsToDiscordMarkup((part.text || '').trim());
            var partAnswer = qemsToDiscordMarkup((part.answer || '').trim());
            var diff = (part.diff || '');

            var label = '[10' + diff + ']';
            if (i === 1) {
                result += label + ' ' + partText + '\n';
            } else {
                result += label + ' ||' + partText + '||\n';
            }
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
        var info = { author: author || '', category: category || '' };

        var rendered = qemsToDiscordMarkup((text || '').trim());
        // Bold the power: everything up to and including the (*) marker.
        var powerIdx = rendered.indexOf('(*)');
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
        var info = { author: author || '', category: category || '' };
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
        var $panel = $('.edit-comments .comments');
        if (!$panel.length) { return false; }
        $.get(window.location.pathname + window.location.search, function (html) {
            var $fresh = $(html).find('.edit-comments .comments').first();
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
        'textarea[name="comment"], textarea.reply-text, textarea.new-comment-text, .doc-comment-box textarea',
        function (e) {
            if (!(e.ctrlKey || e.metaKey)) { return; }
            if (e.key !== 'Enter' && e.keyCode !== 13) { return; }
            e.preventDefault();
            var $ta = $(this), $btn = $();
            if ($ta.closest('.add-comment-form').length) { $btn = $ta.closest('.add-comment-form').find('.comment-submit'); }
            else if ($ta.closest('.reply-form').length) { $btn = $ta.closest('.reply-form').find('.post-reply'); }
            else if ($ta.closest('.doc-comment-box').length) { $btn = $ta.closest('.doc-comment-box').find('.doc-comment-post'); }
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

    // ========================================================================
    // 8. @mention autocomplete for comment boxes
    // ========================================================================

    (function () {
        var qsetId = window.QEMS_QSET_ID;
        if (!qsetId) { return; }
        var SEL = 'textarea[name="comment"], textarea.reply-text, textarea.new-comment-text, .doc-comment-box textarea';
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
