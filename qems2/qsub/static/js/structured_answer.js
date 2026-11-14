/* Structured answer lines: add/remove rows, and a live preview of the line the
   fields will print as. The preview mirrors answer_structure.format_line; the
   server rebuilds the line from the same fields on save, so what the writer
   sees here is what gets stored.

   New rows are cloned from the <template> each group carries, which the server
   renders with the same include as the filled rows. */
(function () {
    'use strict';

    // A row built after load gets the same rich-text fields the server-rendered
    // rows were given (rich_editor.js enhances those at ready).
    function enrich(row) {
        var editor = window.QemsRichEditor;
        if (!editor || !editor.enhanceField) { return; }
        Array.prototype.forEach.call(
            row.querySelectorAll('.sa-text, .sa-instruction'),
            function (field) { editor.enhanceField(field, {compact: true}); });
    }

    function field(row, suffix) {
        var input = row.querySelector('[name$="' + suffix + '"]');
        return input ? (input.value || '').trim() : '';
    }

    function quote(text) {
        return /^["“'‘].*["”'’]$/.test(text) ? text : '"' + text + '"';
    }

    function syncWhen(row) {
        var select = row.querySelector('.sa-when');
        if (select) { row.setAttribute('data-when', select.value); }
    }

    function entry(row, isPrompt) {
        var out = field(row, '_text');
        if (!out) { return ''; }
        var inPlace = field(row, '_inplace');
        if (inPlace) { out += ' in place of ' + quote(inPlace); }
        var when = field(row, '_when');
        var word = field(row, '_word');
        if (when === 'until_read') {
            out += ' until read';
        } else if ((when === 'until' || when === 'after') && word) {
            out += ' ' + when + ' ' + quote(word) + ' is read';
        }
        if (isPrompt) {
            var ask = field(row, '_instruction');
            if (ask) { out += ' by asking ' + quote(ask); }
        }
        return out;
    }

    function rows(block, group) {
        return block.querySelectorAll('.sa-group[data-group="' + group + '"] .sa-rows > .sa-row');
    }

    function preview(block) {
        var target = block.querySelector('.sa-preview-text');
        if (!target) { return; }

        var clauses = [];
        Array.prototype.forEach.call(rows(block, 'or'), function (row) {
            var text = entry(row, false);
            if (text) { clauses.push('or ' + text); }
        });
        Array.prototype.forEach.call(rows(block, 'accept'), function (row) {
            var text = entry(row, false);
            if (text) { clauses.push('accept ' + text); }
        });
        Array.prototype.forEach.call(rows(block, 'prompt'), function (row) {
            var text = entry(row, true);
            if (!text) { return; }
            var kind = row.querySelector('.sa-kind');
            clauses.push((kind && kind.value === 'anti' ? 'antiprompt on ' : 'prompt on ') + text);
        });
        Array.prototype.forEach.call(rows(block, 'reject'), function (row) {
            var text = field(row, '_text');
            if (text) { clauses.push('reject ' + text); }
        });
        Array.prototype.forEach.call(rows(block, 'unparsed'), function (row) {
            var text = field(row, '_unparsed');
            if (text) { clauses.push(text); }
        });

        var primaryInput = block.querySelector('.sa-primary-input');
        var line = primaryInput ? (primaryInput.value || '').trim() : '';
        if (clauses.length) { line += ' [' + clauses.join('; ') + ']'; }
        var noteInput = block.querySelector('.sa-note');
        var note = noteInput ? (noteInput.value || '').trim() : '';
        if (/^\(.*\)$/.test(note)) { note = note.slice(1, -1).trim(); }
        if (note) { line += ' (' + note + ')'; }

        // Shown the way the answer prints -- bold-underlined required answer
        // and all -- rather than as the markup that produces it. The server
        // renders the same line the same way on load.
        var toHtml = window.QemsRichEditor && window.QemsRichEditor.markupToHtml;
        if (toHtml) {
            target.innerHTML = toHtml(line);
        } else {
            target.textContent = line;
        }
    }

    document.addEventListener('click', function (event) {
        var add = event.target.closest ? event.target.closest('.sa-add') : null;
        if (add) {
            event.preventDefault();
            var group = add.closest('.sa-group');
            var template = group.querySelector('template.sa-template');
            if (!template) { return; }
            var row = template.content.firstElementChild.cloneNode(true);
            group.querySelector('.sa-rows').appendChild(row);
            enrich(row);
            var first = row.querySelector('.rich-editor, input');
            if (first) { first.focus(); }
            preview(add.closest('.structured-answer'));
            return;
        }

        var remove = event.target.closest ? event.target.closest('.sa-remove') : null;
        if (remove) {
            event.preventDefault();
            var owner = remove.closest('.structured-answer');
            remove.closest('.sa-row').remove();
            preview(owner);
        }
    });

    document.addEventListener('input', function (event) {
        var block = event.target.closest ? event.target.closest('.structured-answer') : null;
        if (block) { preview(block); }
    });

    document.addEventListener('change', function (event) {
        var block = event.target.closest ? event.target.closest('.structured-answer') : null;
        if (!block) { return; }
        if (event.target.classList.contains('sa-when')) { syncWhen(event.target.closest('.sa-row')); }
        preview(block);
    });
}());
