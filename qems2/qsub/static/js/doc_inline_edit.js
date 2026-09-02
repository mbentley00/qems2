/* Inline editing in the document view.
 *
 * "Edit" mode turns each question in the packet into editable fields in
 * place, so an editor can fix wording while reading the packet as a document
 * rather than opening each question's own page in turn.
 *
 * Nothing is written until you say so: a question you have touched grows
 * Commit and Discard buttons of its own, and only Commit saves. If the
 * question changed underneath you — someone else saved it, or you edited it
 * in another tab — you are told before anything of theirs is overwritten:
 * on the 30-second poll while you type, and again at commit time, where the
 * server refuses a stale write outright.
 *
 * Requires: jQuery, rich_editor.js (QemsRichEditor), and a #doc-edit-payload
 * JSON block written by view_packet.html.
 */
(function ($) {
    'use strict';

    var payload = null;      // qkey -> {fields, revision, labels}
    var open = {};           // qkey -> state for an open editor
    var enabled = false;

    function qkey(qtype, qid) { return qtype + '-' + qid; }

    function esc(s) { return $('<div>').text(s == null ? '' : s).html(); }

    // Write a value into a field. The textarea is the form's copy; the rich
    // editor is what is on screen, and it only picks the value up when asked
    // (resyncUp), so both have to be told.
    function setFieldValue($ta, value) {
        $ta.val(value);
        var reg = window.QemsRichEditor && window.QemsRichEditor.get
            ? window.QemsRichEditor.get($ta.attr('id')) : null;
        if (reg && reg.resyncUp) { reg.resyncUp(); }
        $ta.trigger('change').trigger('input');
    }

    // ---- field layout -----------------------------------------------------

    // Which fields an editor sees, in reading order. Bonus parts are grouped
    // so a part's text and its answer sit together the way they are read.
    function fieldPlan(entry) {
        if (entry.qtype === 'tossup') {
            return [{name: 'tossup_text', label: 'Question', rows: 5},
                    {name: 'tossup_answer', label: 'ANSWER', rows: 2}];
        }
        if (entry.bonus_type === 'vhsl') {
            return [{name: 'part1_text', label: 'Question', rows: 4},
                    {name: 'part1_answer', label: 'ANSWER', rows: 2}];
        }
        var plan = [{name: 'leadin', label: 'Leadin', rows: 2}];
        [1, 2, 3].forEach(function (n) {
            plan.push({name: 'part' + n + '_text', label: 'Part ' + n, rows: 3});
            plan.push({name: 'part' + n + '_answer', label: 'ANSWER ' + n, rows: 2});
        });
        return plan;
    }

    // ---- opening and closing ----------------------------------------------

    function openQuestion($q) {
        var qtype = $q.data('qtype'), qid = $q.data('qid'), key = qkey(qtype, qid);
        if (open[key]) { return; }
        var entry = payload[key];
        if (!entry) { return; }

        var $text = $q.find('.doc-qtext').first();
        var $editor = $('<div class="doc-edit-block"></div>');
        var plan = fieldPlan(entry);
        var fields = {};

        plan.forEach(function (f) {
            var $row = $('<div class="doc-edit-field"></div>').appendTo($editor);
            $('<label class="doc-edit-label"></label>').text(f.label).appendTo($row);
            // rich_editor keys its registry by the textarea's id, so every
            // inline field needs one of its own.
            var domId = 'doc-edit-' + key + '-' + f.name;
            var $ta = $('<textarea class="doc-edit-input expanding"></textarea>')
                .attr('id', domId)
                .attr('rows', f.rows)
                .attr('data-field', f.name)
                .val(entry.fields[f.name] || '')
                .appendTo($row);
            fields[f.name] = $ta;
            // The same rich editor the edit pages use, so formatting, paste
            // conversion and pronunciation-guide tools behave identically.
            if (window.QemsRichEditor && window.QemsRichEditor.enhanceField) {
                window.QemsRichEditor.enhanceField($ta[0], {});
            }
        });

        var $bar = $('<div class="doc-edit-bar"></div>').appendTo($editor);
        $('<span class="doc-edit-status"></span>').appendTo($bar);
        $('<a href="#" class="button tiny doc-edit-commit">Commit</a>').appendTo($bar);
        $('<a href="#" class="button tiny secondary doc-edit-discard">Discard</a>').appendTo($bar);
        $('<a href="#" class="button tiny secondary doc-edit-close">Close</a>').appendTo($bar);

        $text.hide().after($editor);
        $q.addClass('doc-editing');

        open[key] = {
            $q: $q, $editor: $editor, $text: $text, qtype: qtype, qid: qid,
            fields: fields, original: $.extend({}, entry.fields),
            revision: entry.revision, conflict: null
        };
        refreshDirty(key);
        // Typing happens in the contenteditable the rich editor puts on screen,
        // not in the textarea behind it — the textarea only catches up when
        // asked (syncDown). So listen on the whole block, where the
        // contenteditable's own input events bubble to.
        $editor.on('input change keyup paste', function () {
            window.setTimeout(function () { refreshDirty(key); }, 0);
        });
    }

    function closeQuestion(key, force) {
        var st = open[key];
        if (!st) { return true; }
        if (!force && isDirty(key) &&
            !window.confirm('This question has uncommitted changes. Close and lose them?')) {
            return false;
        }
        st.$editor.remove();
        st.$text.show();
        st.$q.removeClass('doc-editing doc-edit-dirty doc-edit-conflict');
        delete open[key];
        return true;
    }

    // ---- dirty tracking ----------------------------------------------------

    function currentValues(key) {
        var st = open[key], out = {};
        $.each(st.fields, function (name, $ta) {
            // The rich editor writes through to the textarea, but only on its
            // own sync; ask for the current value the same way a form submit
            // would see it.
            var reg = window.QemsRichEditor && window.QemsRichEditor.get
                ? window.QemsRichEditor.get($ta.attr('id')) : null;
            if (reg && reg.syncDown) { reg.syncDown(); }
            out[name] = $ta.val() || '';
        });
        return out;
    }

    function isDirty(key) {
        var st = open[key];
        if (!st) { return false; }
        var now = currentValues(key), dirty = false;
        $.each(now, function (name, val) {
            if ((st.original[name] || '') !== val) { dirty = true; }
        });
        return dirty;
    }

    function refreshDirty(key) {
        var st = open[key];
        if (!st) { return; }
        var dirty = isDirty(key);
        st.$q.toggleClass('doc-edit-dirty', dirty);
        st.$editor.find('.doc-edit-commit, .doc-edit-discard').toggleClass('disabled', !dirty);
        if (!st.conflict) {
            st.$editor.find('.doc-edit-status')
                .text(dirty ? 'Uncommitted changes' : 'No changes')
                .toggleClass('doc-edit-status-dirty', dirty);
        }
    }

    function anyDirty() {
        var found = false;
        $.each(open, function (key) { if (isDirty(key)) { found = true; } });
        return found;
    }

    // ---- conflicts ---------------------------------------------------------

    // Somebody else's save landed on a question we are editing. Say so where
    // the editing is happening, and offer the only two honest options:
    // overwrite them, or throw ours away and take theirs.
    function showConflict(key, info) {
        var st = open[key];
        if (!st) { return; }
        st.conflict = info;
        st.$q.addClass('doc-edit-conflict');
        st.$editor.find('.doc-edit-conflict-box').remove();
        var who = info.changed_by ? ' by ' + info.changed_by : '';
        var when = info.changed_date ? ' on ' + info.changed_date : '';
        var $box = $('<div class="doc-edit-conflict-box"></div>');
        $box.append($('<div>').html(
            '<strong>This question was changed' + esc(who) + esc(when) + '</strong> ' +
            'since you started editing it. Committing now would overwrite that change.'));
        var $acts = $('<div class="doc-edit-conflict-acts"></div>').appendTo($box);
        $('<a href="#" class="button tiny alert doc-edit-force">Overwrite theirs</a>').appendTo($acts);
        $('<a href="#" class="button tiny secondary doc-edit-takeirs">Discard mine, take theirs</a>').appendTo($acts);
        st.$editor.prepend($box);
        st.$editor.find('.doc-edit-status').text('Changed by someone else')
            .addClass('doc-edit-status-dirty');
    }

    function clearConflict(key) {
        var st = open[key];
        if (!st) { return; }
        st.conflict = null;
        st.$q.removeClass('doc-edit-conflict');
        st.$editor.find('.doc-edit-conflict-box').remove();
        refreshDirty(key);
    }

    // Take the server's copy: refill the boxes and re-baseline.
    function takeTheirs(key, entry) {
        var st = open[key];
        if (!st) { return; }
        st.original = $.extend({}, entry.fields);
        st.revision = entry.revision;
        $.each(st.fields, function (name, $ta) {
            setFieldValue($ta, entry.fields[name] || '');
        });
        if (payload[key]) { payload[key].fields = $.extend({}, entry.fields); payload[key].revision = entry.revision; }
        clearConflict(key);
    }

    // ---- committing --------------------------------------------------------

    function commit(key, force) {
        var st = open[key];
        if (!st || (!isDirty(key) && !force)) { return; }
        var data = currentValues(key);
        data.question_type = st.qtype;
        data.question_id = st.qid;
        data.baseline = st.revision;
        if (force) { data.force = '1'; }
        st.$editor.find('.doc-edit-status').text('Saving…');
        $.post('/inline_save_question/', data, function (resp) {
            if (typeof resp === 'string') { try { resp = JSON.parse(resp); } catch (e) {} }
            if (!resp || !resp.ok) {
                st.$editor.find('.doc-edit-status').text((resp && resp.error) || 'Could not save.');
                return;
            }
            applySaved(key, resp);
        }).fail(function (xhr) {
            var resp = null;
            try { resp = JSON.parse(xhr.responseText); } catch (e) {}
            if (xhr.status === 409 && resp) {
                showConflict(key, resp);
                if (resp.fields) {
                    st.$editor.find('.doc-edit-takeirs').data('their-entry',
                        {fields: resp.fields, revision: resp.revision});
                }
                return;
            }
            st.$editor.find('.doc-edit-status').text((resp && resp.error) || 'Could not save.');
        });
    }

    // A committed question: the document text becomes what was just saved, and
    // the meta row's length / changed / edited chips catch up, so the page
    // does not have to be reloaded to tell the truth.
    function applySaved(key, resp) {
        var st = open[key];
        if (!st) { return; }
        st.$text.html(resp.html);
        st.original = $.extend({}, resp.fields);
        st.revision = resp.revision;
        if (payload[key]) { payload[key].fields = $.extend({}, resp.fields); payload[key].revision = resp.revision; }
        clearConflict(key);
        refreshDirty(key);
        st.$editor.find('.doc-edit-status').text('Committed').removeClass('doc-edit-status-dirty');

        var $meta = st.$q.find('.doc-meta');
        if (resp.length != null) {
            $meta.find('.doc-tag').filter(function () { return /^Length:/.test($(this).text()); })
                .text('Length: ' + resp.length + '/' + resp.max_length)
                .toggleClass('doc-tag-overlength', !!resp.over_length);
        }
        if (resp.changed_label) {
            $meta.find('.doc-tag').filter(function () { return /^Changed:/.test($(this).text()); })
                .text('Changed: ' + resp.changed_label);
        }
        $meta.find('.doc-tag-unedited').removeClass('doc-tag-unedited')
            .addClass('doc-tag-edited').text('Edited');
    }

    // ---- the poll ----------------------------------------------------------

    // The document already asks the server every 30s whether the packet moved.
    // While inline editing is open we use the per-question revisions in that
    // same answer, so a clash surfaces while you are still typing rather than
    // only when you press Commit.
    function checkRevisions(revs) {
        if (!revs) { return; }
        $.each(open, function (key, st) {
            var latest = revs[key] && revs[key].rev;
            if (latest && latest !== st.revision && !st.conflict && isDirty(key)) {
                showConflict(key, {changed_by: '', changed_date: ''});
            }
        });
    }

    // The revision of a question as this page last left it: what it loaded
    // with, or what it committed or accepted since. The "this packet changed"
    // banner asks, so that a change made here isn't reported back as news.
    function knownRevision(key) {
        return payload && payload[key] ? payload[key].revision : null;
    }

    // ---- wiring ------------------------------------------------------------

    function setEnabled(on) {
        enabled = on;
        $('.doc-page').toggleClass('doc-edit-mode', on);
        if (!on) {
            $.each(Object.keys(open), function (_, key) {
                if (!isDirty(key)) { closeQuestion(key, true); }
            });
        }
    }

    $(function () {
        var node = document.getElementById('doc-edit-payload');
        if (!node) { return; }
        try { payload = JSON.parse(node.textContent); } catch (e) { return; }

        $(document).on('click', '.doc-page.doc-edit-mode .doc-qtext', function (e) {
            if (window.getSelection && !window.getSelection().isCollapsed) { return; }
            e.preventDefault();
            openQuestion($(this).closest('.doc-question'));
        });

        $(document).on('click', '.doc-edit-commit', function (e) {
            e.preventDefault();
            if ($(this).hasClass('disabled')) { return; }
            commit(keyOf(this), false);
        });
        $(document).on('click', '.doc-edit-force', function (e) {
            e.preventDefault();
            commit(keyOf(this), true);
        });
        $(document).on('click', '.doc-edit-takeirs', function (e) {
            e.preventDefault();
            var key = keyOf(this), entry = $(this).data('their-entry');
            if (entry) { takeTheirs(key, entry); }
            else { window.location.reload(); }
        });
        $(document).on('click', '.doc-edit-discard', function (e) {
            e.preventDefault();
            if ($(this).hasClass('disabled')) { return; }
            var key = keyOf(this), st = open[key];
            if (!st || !window.confirm('Discard your uncommitted changes to this question?')) { return; }
            $.each(st.fields, function (name, $ta) {
                setFieldValue($ta, st.original[name] || '');
            });
            clearConflict(key);
            refreshDirty(key);
        });
        $(document).on('click', '.doc-edit-close', function (e) {
            e.preventDefault();
            closeQuestion(keyOf(this), false);
        });

        function keyOf(el) {
            var $q = $(el).closest('.doc-question');
            return qkey($q.data('qtype'), $q.data('qid'));
        }

        // Leaving with work not committed loses it — say so.
        $(window).on('beforeunload', function () {
            if (anyDirty()) {
                return 'You have uncommitted inline edits. Leave and lose them?';
            }
        });

        window.QemsDocEdit = {
            setEnabled: setEnabled,
            checkRevisions: checkRevisions,
            knownRevision: knownRevision,
            anyDirty: anyDirty
        };
    });
})(jQuery);
