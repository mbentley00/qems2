/* Show an anchored comment's target in the document view.
 *
 * A comment card only quotes the span it was left on. That is enough to find
 * the words by eye while the question is rendered, but in Edit mode the
 * rendered question is replaced by editor boxes and the quote is all you have.
 * Hovering a comment draws a highlight over the span it points at -- in the
 * rendered question, or inside the open editor for that question -- and draws
 * nothing when the words are gone, which is itself the answer: the comment no
 * longer points anywhere.
 *
 * The highlight is an overlay positioned over the text's client rects, never a
 * change to the text's own DOM. The editor is a contenteditable the rich editor
 * owns, and wrapping its nodes in <mark> under a live cursor is not safe.
 *
 * Requires: jQuery. Reads the data-anchor-* attributes on a comment node.
 */
(function ($) {
    'use strict';

    // Where the anchored text starts in `hay`, using the context it was taken
    // with to pick the right occurrence. -1 when the text is gone.
    function locate(hay, text, prefix, suffix) {
        var idx;
        if (prefix || suffix) {
            idx = hay.indexOf(prefix + text + suffix);
            if (idx >= 0) { return idx + prefix.length; }
        }
        if (prefix) {
            idx = hay.indexOf(prefix + text);
            if (idx >= 0) { return idx + prefix.length; }
        }
        if (suffix) {
            idx = hay.indexOf(text + suffix);
            if (idx >= 0) { return idx; }
        }
        return hay.indexOf(text);
    }

    // A Range over characters [start, end) of rootEl's text, so a span that
    // crosses formatting tags still measures as one run.
    function rangeFor(rootEl, start, end) {
        var walker = document.createTreeWalker(rootEl, NodeFilter.SHOW_TEXT, null, false);
        var range = document.createRange();
        var pos = 0, started = false, node;
        while ((node = walker.nextNode())) {
            var nodeStart = pos;
            pos += node.length;
            if (!started && pos > start) {
                range.setStart(node, start - nodeStart);
                started = true;
            }
            if (started && pos >= end) {
                range.setEnd(node, end - nodeStart);
                return range;
            }
        }
        return null;
    }

    // What is on screen for this question: the editor's fields while it is open
    // for editing, the rendered text otherwise. A field the rich editor did not
    // take over is a bare textarea, whose contents no Range can reach -- there
    // is nothing to point at, so it is left out.
    function targets($q) {
        var $rich = $q.find('.doc-edit-block .rich-editor');
        if ($rich.length) { return $rich.toArray(); }
        return $q.find('.doc-qtext').toArray();
    }

    var $overlay = null;

    function clear() {
        if ($overlay) { $overlay.remove(); $overlay = null; }
    }

    function draw(range) {
        var rects = range.getClientRects();
        var drawn = 0;
        var $box = $('<div class="doc-anchor-overlay"></div>');
        for (var i = 0; i < rects.length; i++) {
            var r = rects[i];
            if (!r.width || !r.height) { continue; }
            $('<div class="doc-anchor-hit"></div>').css({
                top: r.top + window.pageYOffset,
                left: r.left + window.pageXOffset,
                width: r.width,
                height: r.height
            }).appendTo($box);
            drawn++;
        }
        if (!drawn) { return false; }
        clear();
        $overlay = $box.appendTo(document.body);
        return true;
    }

    function show($comment) {
        var $card = $comment.closest('.doc-comment-card');
        var $q = $('.doc-question[data-qtype="' + $card.data('qtype') + '"]' +
                   '[data-qid="' + $card.data('qid') + '"]').first();
        var text = $comment.attr('data-anchor-text') || '';
        if (!$q.length || !text) { return; }
        var prefix = $comment.attr('data-anchor-prefix') || '';
        var suffix = $comment.attr('data-anchor-suffix') || '';

        var found = false;
        $.each(targets($q), function (_, root) {
            var start = locate(root.textContent, text, prefix, suffix);
            if (start < 0) { return; }
            var range = rangeFor(root, start, start + text.length);
            if (range && draw(range)) { found = true; return false; }
        });
        $comment.toggleClass('doc-anchor-lost', !found);
        $comment.children('.doc-comment-selection').attr('title', found
            ? 'The text this comment is on, highlighted in the question'
            : 'These words are not in the question as it reads now');
    }

    $(function () {
        $(document).on('mouseenter', '.doc-comment[data-anchor-text]', function () {
            show($(this));
        }).on('mouseleave', '.doc-comment[data-anchor-text]', clear);

        // The overlay is placed in page coordinates over rects measured in
        // viewport ones, so anything that moves the text invalidates it.
        $(window).on('scroll resize', clear);
    });
})(jQuery);
