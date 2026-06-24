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
        html = html.replace(/@@QXUS@@/g, '\\_').replace(/@@QXTI@@/g, '\\~');
        return html;
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

    function enhance(textarea, multiline) {
        var $ta = $(textarea);
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
            '  <span class="rich-editor-hint">Rich text &mdash; pasting from Google Docs/Word keeps formatting</span>' +
            '</div>');
        var $editor = $('<div class="rich-editor" contenteditable="true" spellcheck="true"></div>');
        // Only the big bulk "type questions" box gets the extra-tall sizing.
        // (Edit-page fields are also multiline so Enter works, but size by role.)
        if (textarea.id === 'id_questions') { $editor.addClass('rich-editor-multiline'); }
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
        $editor.html(qemsToHtml($ta.val(), multiline));

        // The expanding-textareas plugin wraps textareas in div.expanding —
        // hide the wrapper if present, otherwise the textarea itself.
        var $anchorEl = $ta.closest('div.expanding');
        if (!$anchorEl.length) { $anchorEl = $ta; }
        var $wrapper = $('<div class="rich-editor-wrapper"></div>').append($toolbar, $editor);
        $anchorEl.after($wrapper).hide();

        function syncDown() {
            $ta.val(htmlToQems($editor[0].innerHTML, multiline));
        }

        function resyncUp() {
            $editor.html(qemsToHtml($ta.val(), multiline));
        }

        $editor.on('input blur', syncDown);

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

        // Reflect the formatting at the caret/selection on the toolbar buttons
        // (a button appears "pressed" when its style is active).
        function updateToolbarState() {
            $toolbar.find('.rich-editor-btn').each(function () {
                var cmd = $(this).attr('data-cmd');
                var active = false;
                try { active = document.queryCommandState(cmd); } catch (e) { active = false; }
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
            $editor.focus();
            $(this).attr('data-cmd').split(',').forEach(function (cmd) {
                document.execCommand(cmd, false, null);
            });
            syncDown();
            updateToolbarState();
        });

        if (textarea.id) {
            registry[textarea.id] = {
                root: $editor[0], syncDown: syncDown, resyncUp: resyncUp,
                getSavedRange: function () { return savedRange; }
            };
        }
    }

    /* ---------- Category tag insertion (Type Questions page) ---------- */

    // Insert a category tag at the caret in the rich editor for the given
    // textarea id. Returns false if no such editor exists (caller falls back to
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
        insertCategoryTag: insertCategoryTag
    };

    /* ---------- Wire up the pages ---------- */

    // Add-tossup / add-bonus and edit-tossup / edit-bonus pages: per-field editors.
    // Edit pages allow line breaks (you sometimes need to reflow a stored
    // question); the add pages stay single-line.
    var $qForm = $('#add-tossups, #add-bonuses, #edit-tossup, #edit-bonus').first();
    var allowLineBreaks = $qForm.is('#edit-tossup, #edit-bonus');
    $qForm.find(FIELD_SELECTOR).each(function () {
        enhance(this, allowLineBreaks);
    });

    // Type Questions page: one big line-oriented box
    $('#id_questions').each(function () {
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
