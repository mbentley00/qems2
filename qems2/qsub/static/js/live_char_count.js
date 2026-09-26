/* Live character count for a question being written or edited.
 *
 * Drives the "N / max" readout on the add and edit pages for tossups and
 * bonuses. Counting is done server-side (/live_char_count/) so it follows the
 * set's own rules -- whether pronunciation guides and moderator instructions
 * count -- and can never drift from the number the question is saved with.
 *
 * Markup contract: an element with id="live-char-count" carrying
 *   data-qset-id  the set whose counting rules apply
 *   data-max      the limit for this question type (0 or absent = no limit)
 *   data-fields    comma-separated ids of the textareas to sum
 * Its text is the starting count (the server-rendered one when editing a saved
 * question, 0 on an add page). Fields named but not on the page are skipped, so
 * one list covers an ACF bonus and a VHSL one alike.
 *
 * Requires: jQuery.
 */
(function ($) {
    'use strict';

    $(function () {
        var $out = $('#live-char-count');
        if (!$out.length) { return; }

        var max = parseInt($out.data('max'), 10) || 0;
        var qsetId = $out.data('qset-id');
        var ids = String($out.data('fields') || '').split(',')
            .map(function (name) { return $.trim(name); })
            .filter(function (name) { return name; })
            .map(function (name) { return '#' + name; });
        if (!ids.length) { return; }

        var timer = null;

        function paint(count) {
            $out.css('color', (max && count > max) ? '#c60f13' : '');
        }

        // The starting count is rendered server-side, so colour it on load: an
        // over-length question has to read as over-length before you touch it.
        paint(parseInt($out.text(), 10) || 0);

        function update() {
            // In the unified bonus editor the per-part fields only catch up on
            // submit; pull the unified text down first or the count never moves.
            if (window.qemsSyncBonusUnified) { window.qemsSyncBonusUnified(); }
            var data = { qset_id: qsetId, 'text[]': [] };
            ids.forEach(function (id) {
                var $f = $(id);
                if ($f.length) { data['text[]'].push($f.val() || ''); }
            });
            $.post('/live_char_count/', data, function (resp) {
                if (typeof resp === 'string') { try { resp = JSON.parse(resp); } catch (e) { return; } }
                $out.text(resp.count);
                paint(resp.count);
            });
        }

        // The rich editor types into a contenteditable and writes through to the
        // textarea, so both have to be listened to.
        $(document).on('input', ['.rich-editor'].concat(ids).join(', '), function () {
            clearTimeout(timer);
            timer = setTimeout(update, 350);
        });

    });

    /* Selected-text count: highlight part of a question -- in an editor, the
     * Type Questions box, or the saved question on Proof -- to see what that
     * stretch counts for, by the same rules as the whole.
     *
     * What is sent is the selection's QEMS markup, not the words on screen: a
     * note to the moderator or players (\N...\N) is read aloud but never
     * counted, and it is the markers that say so. Sending the plain text
     * counted a selected note like any other sentence. */
    $(function () {
        var $out = $('#live-char-count');
        // Where the "N selected" readout goes: beside the question's own count,
        // or in the Type Questions count panel's header.
        var $host = $out.length ? $out.parent() : $('#tq-counts-summary').parent();
        var qsetId = $out.length ? $out.data('qset-id') : $('#tq-counts').data('qset-id');
        if (!$host.length || !qsetId) { return; }

        var $sel = $('<span id="sel-char-count" style="color:#888; font-weight:normal;"></span>')
            .appendTo($host);
        var selTimer = null, selSeq = 0;

        function selectedMarkup() {
            var el = document.activeElement;
            // A raw textarea already holds markup.
            if (el && el.tagName === 'TEXTAREA' && el.selectionStart !== el.selectionEnd) {
                return el.value.substring(el.selectionStart, el.selectionEnd);
            }
            var s = window.getSelection();
            if (!s || s.isCollapsed || !s.rangeCount) { return ''; }
            var range = s.getRangeAt(0);
            var node = range.commonAncestorContainer;
            if (node.nodeType === 3) { node = node.parentNode; }
            if (!$(node).closest('.rich-editor, .anchor-region').length) { return ''; }
            var box = document.createElement('div');
            box.appendChild(range.cloneContents());
            // A selection that starts or ends inside a note carries the note's
            // span with it, so the markers survive the conversion.
            if (window.QemsMarkup && window.QemsMarkup.htmlToQems) {
                return window.QemsMarkup.htmlToQems(box.innerHTML).replace(/\u00a0/g, ' ');
            }
            return s.toString();
        }

        function updateSelection() {
            var text = selectedMarkup();
            var seq = ++selSeq;
            if (!text) { $sel.text(''); return; }
            $.post('/live_char_count/', { qset_id: qsetId, text: text }, function (resp) {
                if (seq !== selSeq) { return; }
                if (typeof resp === 'string') { try { resp = JSON.parse(resp); } catch (e) { return; } }
                $sel.text(' · ' + resp.count + ' selected');
            });
        }

        $(document).on('selectionchange', function () {
            clearTimeout(selTimer);
            selTimer = setTimeout(updateSelection, 250);
        });
    });
})(jQuery);
