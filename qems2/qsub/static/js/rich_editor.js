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
        // \Pword\P marks the target of a following pronunciation guide.
        html = html.replace(/\\P([\s\S]+?)\\P/g, '<span class="pg-target">$1</span>');
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
            // Pronunciation-guide target: select the word(s) together with the
            // following ("...") guide, and this wraps just the word(s) in \P...\P.
            // With the caret inside an existing mark it removes that mark, so PG
            // is a toggle; selecting fewer words inside a mark shrinks it.
            '  <a href="#" class="rich-editor-btn rich-editor-pg" data-cmd="pgtarget" title="Mark pronunciation-guide target: select the word(s) and their (&quot;...&quot;) guide. Click with the caret inside a mark to remove it.">PG</a>' +
            // Guess every unmarked guide\'s target from how many words its
            // respelling has, so marks don't have to be placed by hand.
            '  <a href="#" class="rich-editor-btn rich-editor-pg" data-cmd="pgauto" title="Mark the target of every pronunciation guide in this field, guessing the word(s) each one covers from its respelling">PG auto</a>' +
            // Switch to editing the raw QEMS markup (e.g. ~foo~ for italics) in
            // the underlying textarea, to hand-fix anything the rich view got
            // wrong; the label flips to "Rich" to switch back.
            '  <a href="#" class="rich-editor-btn rich-editor-plain" data-cmd="plaintext" title="Edit the raw QEMS markup directly (e.g. ~foo~ for italics, _foo_ for answer underlines)">Raw</a>' +
            '</div>');
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
        $editor.html(qemsToHtml($ta.val(), multiline));

        // The expanding-textareas plugin wraps textareas in div.expanding —
        // hide the wrapper if present, otherwise the textarea itself.
        var $anchorEl = $ta.closest('div.expanding');
        if (!$anchorEl.length) { $anchorEl = $ta; }
        var $wrapper = $('<div class="rich-editor-wrapper"></div>').append($toolbar, $editor);
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

        // The pg-target span containing `node`, if any (bounded by the editor).
        function pgTargetAt(node) {
            while (node && node !== $editor[0]) {
                if (node.nodeType === 1 && $(node).hasClass('pg-target')) { return node; }
                node = node.parentNode;
            }
            return null;
        }

        // The pg-target span the caret/selection currently sits inside, if any.
        function currentPgTarget() {
            var sel = window.getSelection();
            if (!sel || !sel.rangeCount) { return null; }
            var range = sel.getRangeAt(0);
            if (!$editor[0].contains(range.commonAncestorContainer)) { return null; }
            return pgTargetAt(range.commonAncestorContainer);
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
        // covering the span so the native undo stack stays intact.
        function unmarkPgTarget(span) {
            var inner = span.innerHTML;
            selectNode(span);
            document.execCommand('insertHTML', false, inner);
            syncDown();
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
            // Peel any pg-target markers already inside the selection so we don't
            // nest spans when re-marking.
            html = html.replace(/<span class="pg-target">([\s\S]*?)<\/span>/g, '$1');
            // Split off a trailing ("...") / (...) guide, keeping it outside the span.
            var m = html.match(/^([\s\S]*?)(\s*\([^()]*\)\s*)$/);
            var termHtml, tail;
            if (m && m[1].replace(/<[^>]*>/g, '').trim()) {
                termHtml = m[1].replace(/\s+$/, '');
                tail = ' ' + m[2].trim();
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
            var inPg = !!currentPgTarget();
            $toolbar.find('.rich-editor-btn').each(function () {
                var cmd = $(this).attr('data-cmd');
                if (cmd === 'pgauto') { return; }
                var active;
                if (cmd === 'pgtarget') {
                    // Pressed while the caret is inside a mark, so it reads as a
                    // toggle: clicking again removes the mark.
                    active = inPg;
                    $(this).attr('title', inPg
                        ? 'Remove this pronunciation-guide mark (or select fewer words to shrink it)'
                        : 'Mark pronunciation-guide target: select the word(s) and their ("...") guide');
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
            // The formatting buttons act on the rich editor; ignore them while
            // the raw textarea is showing.
            if (plainMode) { return; }
            $editor.focus();
            if (cmd === 'pgtarget') {
                wrapPgTarget();
            } else if (cmd === 'pgauto') {
                autoMarkPgTargets();
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
