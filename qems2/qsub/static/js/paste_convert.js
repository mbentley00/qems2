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
        return walkNode(doc.body);
    }

    function walkNode(node) {
        if (node.nodeType === Node.TEXT_NODE) {
            return node.textContent;
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
            inner = '_' + inner + '_';
        } else if (isUnderline) {
            inner = '__' + inner + '__';
        } else if (isBold) {
            // Bold alone has no QEMS markup — pass through
        }
        if (isItalic) {
            inner = '~' + inner + '~';
        }
        if (isSup) {
            inner = '\\S' + inner + '\\S';
        }
        if (isSub) {
            inner = '\\s' + inner + '\\s';
        }

        // Block-level elements get a newline after them
        if (/^(p|div|br|li|tr|h[1-6])$/.test(tag)) {
            if (tag === 'br') {
                inner = '\n';
            } else {
                inner = inner + '\n';
            }
        }

        return inner;
    }

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
            var converted = htmlToQemsMarkup(html).trim();

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
    }

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
    function formatTossupForDiscord() {
        var text = ($('#id_tossup_text').val() || '').trim();
        var answer = ($('#id_tossup_answer').val() || '').trim();
        var info = getAuthorAndCategory();

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

        result += '\nANSWER: ||' + discordAnswer + '||';
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
    function formatBonusForDiscord() {
        var leadin = ($('#id_leadin').val() || '').trim();
        var info = getAuthorAndCategory();
        var result = qemsToDiscordMarkup(leadin) + '\n';

        var difficulties = [];
        for (var i = 1; i <= 3; i++) {
            var partText = qemsToDiscordMarkup(($('#id_part' + i + '_text').val() || '').trim());
            var partAnswer = qemsToDiscordMarkup(($('#id_part' + i + '_answer').val() || '').trim());
            var diff = ($('#id_part' + i + '_difficulty').val() || '');

            var label = '[10' + diff + ']';
            if (i === 1) {
                result += label + ' ' + partText + '\n';
            } else {
                result += label + ' ||' + partText + '||\n';
            }
            result += 'ANSWER: ||' + partAnswer + '||\n';
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

    // Wire up "Copy for Discord" buttons
    var $copyTossupBtn = $('#copy-for-discord-tossup');
    if ($copyTossupBtn.length) {
        $copyTossupBtn.on('click', function (e) {
            e.preventDefault();
            var formatted = formatTossupForDiscord();
            copyToClipboard(formatted, $copyTossupBtn);
        });
    }

    var $copyBonusBtn = $('#copy-for-discord-bonus');
    if ($copyBonusBtn.length) {
        $copyBonusBtn.on('click', function (e) {
            e.preventDefault();
            var formatted = formatBonusForDiscord();
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
    function formatTossupForDiscordPlain() {
        var text = ($('#id_tossup_text').val() || '').trim();
        var answer = ($('#id_tossup_answer').val() || '').trim();
        var info = getAuthorAndCategory();

        var result = qemsToDiscordMarkup(text);
        result += '\nANSWER: ' + qemsToDiscordMarkup(answer);
        if (info.author || info.category) {
            result += '\n<' + info.author + ', ' + info.category + '>';
        }
        return result;
    }

    /**
     * Format a bonus for Discord without spoiler tags.
     */
    function formatBonusForDiscordPlain() {
        var leadin = ($('#id_leadin').val() || '').trim();
        var info = getAuthorAndCategory();
        var result = qemsToDiscordMarkup(leadin) + ' For 10 points each:\n';

        for (var i = 1; i <= 3; i++) {
            var partText = qemsToDiscordMarkup(($('#id_part' + i + '_text').val() || '').trim());
            var partAnswer = qemsToDiscordMarkup(($('#id_part' + i + '_answer').val() || '').trim());
            var diff = ($('#id_part' + i + '_difficulty').val() || '');
            result += '[10' + diff + '] ' + partText + '\n';
            result += 'ANSWER: ' + partAnswer + '\n';
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
            var formatted = formatTossupForDiscordPlain();
            copyToClipboard(formatted, $copyPlainTossupBtn);
        });
    }

    var $copyPlainBonusBtn = $('#copy-for-discord-plain-bonus');
    if ($copyPlainBonusBtn.length) {
        $copyPlainBonusBtn.on('click', function (e) {
            e.preventDefault();
            var formatted = formatBonusForDiscordPlain();
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
            var dialog = $('#info-dialog').dialog({
                modal: true,
                buttons: {
                    Ok: function () {
                        $(this).dialog('close');
                        window.location.reload();
                    }
                }
            });
            dialog.append('<div class="' + json_response['message_class'] + '">' + json_response['message'] + '</div>');
            dialog.dialog('open');
        });
    });
});
