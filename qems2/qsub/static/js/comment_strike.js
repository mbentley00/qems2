/**
 * comment_strike.js — let an editor cross out part of a comment to show it's
 * been handled.
 *
 * Select text inside a `.comment-strikeable` span and a small "Cross out"
 * button appears; clicking it strikes that text through (persisted server-side
 * by wrapping it in the QEMS `\D...\D` strike token). Selecting struck text —
 * or clicking a crossed-out run — offers to un-cross it. Editors only; the page
 * opts in via `window.QEMS_CAN_STRIKE`.
 *
 * Offsets are counted over the DISPLAYED text (Range#toString length), which
 * the server maps back to the raw markup (see utils.toggle_comment_strike).
 */
(function () {
    if (!window.jQuery) { return; }
    var $ = window.jQuery;

    $(function () {
        if (!window.QEMS_CAN_STRIKE) { return; }

        var $btn = $('<button type="button" class="comment-strike-btn" style="display:none;"></button>')
            .appendTo(document.body);
        var pending = null;

        function hide() { $btn.hide(); pending = null; }

        // Visible-character offset from the start of `root` to (node, nodeOffset).
        function textOffset(root, node, nodeOffset) {
            var r = document.createRange();
            r.setStart(root, 0);
            r.setEnd(node, nodeOffset);
            return r.toString().length;
        }

        // True if `node` is inside a <del> within `root` (i.e. already struck).
        function withinDel(node, root) {
            while (node && node !== root) {
                if (node.nodeType === 1 && node.tagName === 'DEL') { return true; }
                node = node.parentNode;
            }
            return false;
        }

        function post(commentId, start, end, strike) {
            $.post('/strike_comment/', {
                comment_id: commentId, start: start, end: end,
                strike: strike ? 'true' : 'false'
            }, function (resp) {
                var j = (typeof resp === 'string') ? JSON.parse(resp) : resp;
                if (j && j.success) { location.reload(); }
                else { alert((j && j.message) || 'Could not update the comment.'); }
            });
        }

        // Show the button above the current selection when it lies inside one
        // comment. Label depends on whether the selection is already struck.
        function refresh() {
            var sel = window.getSelection();
            if (!sel || sel.rangeCount === 0 || sel.isCollapsed) { hide(); return; }
            var range = sel.getRangeAt(0);
            var root = $(range.commonAncestorContainer).closest('.comment-strikeable')[0];
            if (!root || !root.contains(range.startContainer) || !root.contains(range.endContainer)) {
                hide(); return;
            }
            if (!sel.toString().trim()) { hide(); return; }
            var start = textOffset(root, range.startContainer, range.startOffset);
            var end = textOffset(root, range.endContainer, range.endOffset);
            if (end <= start) { hide(); return; }
            var struck = withinDel(range.startContainer, root) && withinDel(range.endContainer, root);
            pending = { commentId: $(root).data('comment-id'), start: start, end: end, strike: !struck };
            $btn.text(struck ? 'Un-cross out' : 'Cross out');
            var rect = range.getBoundingClientRect();
            $btn.css({
                top: (rect.top + window.pageYOffset - $btn.outerHeight() - 6) + 'px',
                left: (rect.left + window.pageXOffset) + 'px'
            }).show();
        }

        $(document).on('mouseup', function () { setTimeout(refresh, 0); });

        // Keep the selection alive through the click (mousedown would clear it).
        $btn.on('mousedown', function (e) { e.preventDefault(); });
        $btn.on('click', function () {
            if (!pending) { return; }
            post(pending.commentId, pending.start, pending.end, pending.strike);
        });

        // Plain click on a crossed-out run un-crosses that whole run.
        $(document).on('click', '.comment-strikeable del', function (e) {
            var root = $(this).closest('.comment-strikeable')[0];
            if (!root) { return; }
            // Ignore if the user is mid-selection (handled by the button instead).
            var sel = window.getSelection();
            if (sel && !sel.isCollapsed) { return; }
            e.preventDefault();
            var start = textOffset(root, this, 0);
            var end = start + (this.textContent || '').length;
            post($(root).data('comment-id'), start, end, false);
        });
    });
})();
