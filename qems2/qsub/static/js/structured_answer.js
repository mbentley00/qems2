/* Structured answer lines: add/remove rows, and a live preview of the line the
   fields will print as. The preview mirrors answer_structure.format_line; the
   server rebuilds the line from the same fields on save, so what the writer
   sees here is what gets stored. */
(function () {
    'use strict';

    function el(tag, attrs) {
        var node = document.createElement(tag);
        Object.keys(attrs || {}).forEach(function (key) { node.setAttribute(key, attrs[key]); });
        return node;
    }

    function removeButton() {
        var button = el('button', {'type': 'button', 'class': 'sa-remove', 'title': 'Remove'});
        button.innerHTML = '&times;';
        return button;
    }

    function textInput(name, placeholder, className) {
        return el('input', {'type': 'text', 'name': name, 'placeholder': placeholder, 'class': className});
    }

    function buildRow(block, group) {
        var prefix = block.getAttribute('data-prefix');
        var isTossup = block.getAttribute('data-is-tossup') === '1';
        var row = el('div', {'class': 'sa-row'});

        if (group === 'note') {
            row.appendChild(textInput(prefix + '_unparsed', 'printed as typed', 'sa-text'));
        } else if (group === 'prompt') {
            var kind = el('select', {'name': prefix + '_prompt_anti', 'class': 'sa-kind'});
            [['prompt', 'prompt'], ['anti', 'antiprompt']].forEach(function (pair) {
                var option = el('option', {'value': pair[0]});
                option.textContent = pair[1];
                kind.appendChild(option);
            });
            row.appendChild(kind);
            row.appendChild(textInput(prefix + '_prompt_text', 'what to prompt on', 'sa-text'));
            row.appendChild(textInput(prefix + '_prompt_instruction', 'by asking…', 'sa-instruction'));
            if (isTossup) {
                row.appendChild(textInput(prefix + '_prompt_until', 'until this word is read', 'sa-until'));
            }
        } else {
            row.appendChild(textInput(prefix + '_accept_text', 'another acceptable answer', 'sa-text'));
            if (isTossup) {
                row.appendChild(textInput(prefix + '_accept_until', 'until this word is read', 'sa-until'));
            }
        }

        row.appendChild(removeButton());
        return row;
    }

    // A row built after load gets the same rich-text fields the server-rendered
    // rows were given (rich_editor.js enhances those at ready).
    function enrich(row) {
        var editor = window.QemsRichEditor;
        if (!editor || !editor.enhanceField) { return; }
        Array.prototype.forEach.call(
            row.querySelectorAll('.sa-text, .sa-instruction'),
            function (field) { editor.enhanceField(field, {compact: true}); });
    }

    function values(block, name) {
        return Array.prototype.map.call(
            block.querySelectorAll('[name="' + block.getAttribute('data-prefix') + '_' + name + '"]'),
            function (input) { return (input.value || '').trim(); });
    }

    function until(value) {
        return value ? ' until "' + value + '" is read' : '';
    }

    function preview(block) {
        var target = block.querySelector('.sa-preview-text');
        if (!target) { return; }

        var clauses = [];
        var acceptText = values(block, 'accept_text');
        var acceptUntil = values(block, 'accept_until');
        acceptText.forEach(function (text, i) {
            if (text) { clauses.push('accept ' + text + until(acceptUntil[i] || '')); }
        });

        var promptText = values(block, 'prompt_text');
        var promptInstruction = values(block, 'prompt_instruction');
        var promptUntil = values(block, 'prompt_until');
        var promptKind = values(block, 'prompt_anti');
        promptText.forEach(function (text, i) {
            if (!text) { return; }
            var directive = promptKind[i] === 'anti' ? 'antiprompt on' : 'prompt on';
            var instruction = promptInstruction[i] ? ' by asking "' + promptInstruction[i] + '"' : '';
            clauses.push(directive + ' ' + text + instruction + until(promptUntil[i] || ''));
        });

        values(block, 'unparsed').forEach(function (clause) {
            if (clause) { clauses.push(clause); }
        });

        var primaryInput = block.querySelector('.sa-primary-input');
        var primary = primaryInput ? (primaryInput.value || '').trim() : '';
        var line = clauses.length ? primary + ' [' + clauses.join('; ') + ']' : primary;

        // Shown the way the answer prints — bold-underlined required answer and
        // all — rather than as the markup that produces it. The server renders
        // the same line the same way on load.
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
            var block = add.closest('.structured-answer');
            var group = add.getAttribute('data-group');
            var rows = block.querySelector('.sa-group[data-group="' + group + '"] .sa-rows');
            var row = buildRow(block, group);
            rows.appendChild(row);
            enrich(row);
            var first = row.querySelector('.rich-editor, input');
            if (first) { first.focus(); }
            preview(block);
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
        if (block) { preview(block); }
    });
}());
