/* Frequency lookup against the qbreader database for highlighted text.
 *
 * Select a phrase in a question field (the editor, a textarea, or a saved
 * question's preview) and a small popup reports how often it appears in the
 * qbreader database — a fast read on whether a clue is fresh or worn out —
 * with a link to the full qbreader results for the term.
 *
 * The counts come through /qbreader_freq/ on the QEMS server (same rules as
 * the db page's "exact phrase" search), so the browser never talks to
 * qbreader directly and repeated lookups of the same term are served from
 * the server's cache.
 */
(function ($) {
    'use strict';

    var MIN_LEN = 3, MAX_LEN = 120, DEBOUNCE_MS = 600;
    var $pop = null, timer = null, seq = 0, lastShownTerm = '';
    var mouse = { x: null, y: null };
    var cache = {};   // term -> response, for this page view

    // Where a selection counts as "part of a question": the writing fields,
    // the rich editor that fronts them, and the saved-question previews.
    var FIELD_SELECTOR = [
        '#id_tossup_text', '#id_tossup_answer',
        '#id_leadin', '#id_part1_text', '#id_part2_text', '#id_part3_text',
        '#id_part1_answer', '#id_part2_answer', '#id_part3_answer',
        '#unified-bonus-text', '#id_questions'
    ].join(', ');

    function selectedQuestionText() {
        var el = document.activeElement;
        if (el && (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') &&
                $(el).is(FIELD_SELECTOR) && el.selectionStart !== el.selectionEnd) {
            return el.value.substring(el.selectionStart, el.selectionEnd);
        }
        var s = window.getSelection();
        if (!s || s.isCollapsed || !s.rangeCount) { return ''; }
        var node = s.getRangeAt(0).commonAncestorContainer;
        if (node.nodeType === 3) { node = node.parentNode; }
        if (!$(node).closest('.rich-editor, .anchor-region').length) { return ''; }
        return s.toString();
    }

    function cleanTerm(text) {
        // Markup characters and the guide the term drags along would poison an
        // exact-phrase search; a multi-line grab was a miss, not a term.
        var t = (text || '').replace(/[_~]|\\[SsBbNP]|\(\*\)|\(\+\)/g, ' ')
                            .replace(/\s+/g, ' ').trim();
        if (t.length < MIN_LEN || t.length > MAX_LEN) { return ''; }
        if ((text || '').indexOf('\n') !== -1) { return ''; }
        return t;
    }

    function popupAnchor() {
        // The last mouseup is where the writer's eyes are. A keyboard
        // selection in the DOM has a rectangle to sit under instead.
        if (mouse.x !== null) { return { x: mouse.x, y: mouse.y + 14 }; }
        var s = window.getSelection();
        if (s && s.rangeCount && !s.isCollapsed) {
            var r = s.getRangeAt(0).getBoundingClientRect();
            if (r && (r.width || r.height)) { return { x: r.left, y: r.bottom + 6 }; }
        }
        return null;
    }

    function ensurePopup() {
        if ($pop) { return $pop; }
        $pop = $('<div id="qbr-pop" role="status"></div>').css({
            position: 'fixed', zIndex: 4000, maxWidth: '22rem',
            background: '#2c3540', color: '#f2f4f6', padding: '0.35rem 0.6rem',
            borderRadius: '5px', boxShadow: '0 2px 10px rgba(0,0,0,0.35)',
            fontSize: '0.82rem', lineHeight: 1.5, display: 'none'
        }).appendTo(document.body);
        // Keep the popup alive while the pointer is over it (clicking its link
        // collapses the selection, which would otherwise hide it instantly).
        $pop.on('mousedown', function (e) { e.stopPropagation(); });
        return $pop;
    }

    function hide() { if ($pop) { $pop.hide(); } lastShownTerm = ''; }

    function show(term, resp) {
        var at = popupAnchor();
        if (!at) { return; }
        var $p = ensurePopup().empty();
        var head = $('<div>').appendTo($p);
        $('<strong>').text('“' + (term.length > 40 ? term.slice(0, 40) + '…' : term) + '”')
            .css('color', '#fff').appendTo(head);
        var counts;
        if (resp.ok) {
            counts = resp.tossups + ' tossup' + (resp.tossups === 1 ? '' : 's') +
                     ' · ' + resp.bonuses + ' bonus' + (resp.bonuses === 1 ? '' : 'es') +
                     ' on qbreader';
        } else {
            counts = resp.error || 'lookup failed';
        }
        $('<div>').text(counts).appendTo($p);
        $('<a target="_blank" rel="noopener">full qbreader results ↗</a>')
            .attr('href', resp.url ||
                  ('https://www.qbreader.org/db/?q=' + encodeURIComponent(term) + '&exactPhrase=true'))
            .css({ color: '#8ec9e8', fontWeight: 'bold' }).appendTo($p);
        // Clamp to the viewport.
        $p.css({ left: 0, top: 0, display: 'block' });
        var w = $p.outerWidth(), h = $p.outerHeight();
        $p.css({
            left: Math.max(6, Math.min(at.x, $(window).width() - w - 6)) + 'px',
            top: Math.max(6, Math.min(at.y, $(window).height() - h - 6)) + 'px'
        });
        lastShownTerm = term;
    }

    function lookup() {
        var term = cleanTerm(selectedQuestionText());
        if (!term) { hide(); return; }
        if (term === lastShownTerm) { return; }
        var mySeq = ++seq;
        if (cache[term]) { show(term, cache[term]); return; }
        $.get('/qbreader_freq/', { q: term }, function (resp) {
            if (typeof resp === 'string') { try { resp = JSON.parse(resp); } catch (e) { return; } }
            cache[term] = resp;
            if (mySeq !== seq) { return; }
            // Only if the selection is still this term — it may be long gone.
            if (cleanTerm(selectedQuestionText()) === term) { show(term, resp); }
        }).fail(function () {
            if (mySeq === seq) { show(term, { ok: false, error: 'lookup failed' }); }
        });
    }

    $(function () {
        // Nothing to do on pages without question fields.
        if (!$(FIELD_SELECTOR).length && !$('.rich-editor, .anchor-region').length) { return; }
        $(document).on('mouseup', function (e) { mouse.x = e.clientX; mouse.y = e.clientY; });
        $(document).on('selectionchange', function () {
            clearTimeout(timer);
            if (!cleanTerm(selectedQuestionText())) { hide(); return; }
            timer = setTimeout(lookup, DEBOUNCE_MS);
        });
        $(document).on('keydown', function (e) { if (e.key === 'Escape') { hide(); } });
    });
})(jQuery);
