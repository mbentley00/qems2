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
        html = html.replace(/__([^_]+)__/g, '<u>$1</u>');
        html = html.replace(/_([^_]+)_/g, '<u><b>$1</b></u>');
        html = html.replace(/~([^~]+)~/g, '<i>$1</i>');
        html = html.replace(/\\S([\s\S]+?)\\S/g, '<sup>$1</sup>');
        html = html.replace(/\\s([\s\S]+?)\\s/g, '<sub>$1</sub>');
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
            // QEMS markup has no bold-only, so Bold applies bold+underline
            // (the required-answer-part convention)
            '  <a href="#" class="rich-editor-btn" data-cmd="bold,underline" title="Bold (saved as bold+underline)"><b>B</b></a>' +
            '  <a href="#" class="rich-editor-btn" data-cmd="underline" title="Underline"><u>U</u></a>' +
            '  <a href="#" class="rich-editor-btn" data-cmd="italic" title="Italic"><i>I</i></a>' +
            '  <a href="#" class="rich-editor-btn" data-cmd="subscript" title="Subscript">x<sub>2</sub></a>' +
            '  <a href="#" class="rich-editor-btn" data-cmd="superscript" title="Superscript">x<sup>2</sup></a>' +
            '  <span class="rich-editor-hint">Rich text &mdash; pasting from Google Docs/Word keeps formatting</span>' +
            '</div>');
        var $editor = $('<div class="rich-editor" contenteditable="true" spellcheck="true"></div>');
        if (multiline) { $editor.addClass('rich-editor-multiline'); }
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
        });

        if (textarea.id) {
            registry[textarea.id] = { root: $editor[0], syncDown: syncDown, resyncUp: resyncUp };
        }
    }

    /* ---------- Category tag insertion (Type Questions page) ---------- */

    function textNodesIn(el) {
        var walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, null, false);
        var nodes = [];
        while (walker.nextNode()) { nodes.push(walker.currentNode); }
        return nodes;
    }

    function appendToLine(lineNode, str) {
        if (lineNode.nodeType === Node.TEXT_NODE) {
            lineNode.textContent = lineNode.textContent.replace(/\s+$/, '') + str;
        } else {
            lineNode.appendChild(document.createTextNode(str));
        }
    }

    // Remove an existing trailing {Category - Subcategory} tag, which may
    // span text nodes
    function removeTrailingTag(lineNode) {
        var match = lineNode.textContent.match(/\s*\{[^{}]*\}\s*$/);
        if (!match) { return; }
        var remove = match[0].length;
        var nodes = lineNode.nodeType === Node.TEXT_NODE ? [lineNode] : textNodesIn(lineNode);
        for (var i = nodes.length - 1; i >= 0 && remove > 0; i--) {
            var t = nodes[i].textContent;
            var take = Math.min(remove, t.length);
            nodes[i].textContent = t.slice(0, t.length - take);
            remove -= take;
        }
    }

    // Insert a category tag onto the nearest ANSWER line at/above the caret
    // in the rich editor for the given textarea id. Returns false if no such
    // editor exists (caller falls back to plain-textarea handling).
    function insertCategoryTag(textareaId, tag) {
        var reg = registry[textareaId];
        if (!reg) { return false; }
        var editor = reg.root;

        // Line nodes are the editor's direct children (divs, or a bare text
        // node for an unwrapped first line)
        var lines = Array.prototype.filter.call(editor.childNodes, function (n) {
            return n.nodeType === Node.ELEMENT_NODE ||
                   (n.nodeType === Node.TEXT_NODE && n.textContent.trim());
        });

        // Which line is the caret on?
        var caretLine = null;
        var sel = window.getSelection();
        if (sel && sel.rangeCount && editor.contains(sel.anchorNode) && sel.anchorNode !== editor) {
            var n = sel.anchorNode;
            while (n.parentNode && n.parentNode !== editor) { n = n.parentNode; }
            if (lines.indexOf(n) !== -1) { caretLine = n; }
        }

        // Walk backward from the caret line (or the end) to the nearest
        // ANSWER line
        var start = caretLine ? lines.indexOf(caretLine) : lines.length - 1;
        var target = null;
        for (var i = start; i >= 0; i--) {
            if (/^\s*answer/i.test(lines[i].textContent)) { target = lines[i]; break; }
        }

        if (target) {
            removeTrailingTag(target);
            appendToLine(target, ' ' + tag);
        } else if (caretLine) {
            appendToLine(caretLine, ' ' + tag);
        } else if (lines.length) {
            appendToLine(lines[lines.length - 1], ' ' + tag);
        } else {
            editor.appendChild(document.createTextNode(tag));
        }

        reg.syncDown();
        editor.focus();
        return true;
    }

    window.QemsRichEditor = {
        get: function (textareaId) { return registry[textareaId] || null; },
        insertCategoryTag: insertCategoryTag
    };

    /* ---------- Wire up the pages ---------- */

    // Add-tossup / add-bonus pages: single-paragraph fields
    $('#add-tossups, #add-bonuses').first().find(FIELD_SELECTOR).each(function () {
        enhance(this, false);
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
