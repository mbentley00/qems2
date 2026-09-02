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

        // Selected-text count: highlight part of the question (an editor or the
        // saved preview) to see what that stretch counts for, same rules.
        var $sel = $('<span id="sel-char-count" style="color:#888; font-weight:normal;"></span>')
            .appendTo($out.parent());
        var selTimer = null, selSeq = 0;

        function selectedText() {
            var el = document.activeElement;
            if (el && el.tagName === 'TEXTAREA' && el.selectionStart !== el.selectionEnd) {
                return el.value.substring(el.selectionStart, el.selectionEnd);
            }
            var s = window.getSelection();
            if (!s || s.isCollapsed || !s.rangeCount) { return ''; }
            var node = s.getRangeAt(0).commonAncestorContainer;
            if (node.nodeType === 3) { node = node.parentNode; }
            if (!$(node).closest('.rich-editor, .anchor-region').length) { return ''; }
            return s.toString();
        }

        function updateSelection() {
            var text = selectedText();
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
