/* Google Docs-style anchored comments: select text in the rendered question
 * panel, comment on it, and see existing anchored comments highlighted. */
$(function () {

    var CONTEXT_CHARS = 60;

    var $region = $('.anchor-region').first();
    if (!$region.length) { return; }
    var regionEl = $region[0];
    var questionType = $region.attr('data-question-type');
    var questionId = $region.attr('data-question-id');

    var pendingAnchor = null;

    function regionText() {
        return regionEl.textContent;
    }

    function getTextNodes() {
        var walker = document.createTreeWalker(regionEl, NodeFilter.SHOW_TEXT, null, false);
        var nodes = [];
        while (walker.nextNode()) { nodes.push(walker.currentNode); }
        return nodes;
    }

    // Character offset of a range boundary within the region's text content
    function rangeStartOffset(range) {
        var pre = range.cloneRange();
        pre.selectNodeContents(regionEl);
        pre.setEnd(range.startContainer, range.startOffset);
        return pre.toString().length;
    }

    /* ---------- Highlighting existing anchored comments ---------- */

    // Find the character offset of the anchored text, using surrounding
    // context to disambiguate. Returns -1 if the text is gone (stale anchor).
    function locateAnchor(text, prefix, suffix) {
        var hay = regionText();
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

    // Wrap the characters in [start, end) in <mark> elements, splitting
    // text nodes as needed so highlights can span formatting tags.
    function highlightRange(start, end, commentId) {
        var nodes = getTextNodes();
        var pos = 0;
        for (var i = 0; i < nodes.length && pos < end; i++) {
            var node = nodes[i];
            var len = node.length;
            var nodeStart = pos;
            pos += len;
            if (pos <= start || nodeStart >= end) { continue; }
            var s = Math.max(start - nodeStart, 0);
            var e = Math.min(end - nodeStart, len);
            var target = node;
            if (s > 0) { target = target.splitText(s); }
            if (e - s < target.length) { target.splitText(e - s); }
            var mark = document.createElement('mark');
            mark.className = 'anchored-highlight';
            mark.setAttribute('data-comment-id', commentId);
            target.parentNode.insertBefore(mark, target);
            mark.appendChild(target);
        }
    }

    function flash($el) {
        $el.addClass('anchor-flash');
        setTimeout(function () { $el.removeClass('anchor-flash'); }, 1500);
    }

    $('.comment-item[data-anchor-text]').each(function () {
        var $item = $(this);
        var commentId = $item.attr('data-comment-id');
        var start = locateAnchor($item.attr('data-anchor-text'),
                                 $item.attr('data-anchor-prefix') || '',
                                 $item.attr('data-anchor-suffix') || '');
        if (start >= 0) {
            highlightRange(start, start + $item.attr('data-anchor-text').length, commentId);
        } else {
            $item.find('.comment-quote').addClass('orphaned')
                .attr('title', 'The question text this comment referred to has changed');
        }
    });

    // Clicking a highlight scrolls to its comment, and vice versa
    $region.on('click', '.anchored-highlight', function () {
        var $item = $('.comment-item[data-comment-id="' + $(this).attr('data-comment-id') + '"]');
        if ($item.length) {
            $('html, body').animate({ scrollTop: $item.offset().top - 100 }, 300);
            flash($item);
        }
    });

    $(document).on('click', '.comment-quote:not(.orphaned)', function () {
        var commentId = $(this).closest('.comment-item').attr('data-comment-id');
        var $marks = $region.find('.anchored-highlight[data-comment-id="' + commentId + '"]');
        if ($marks.length) {
            $('html, body').animate({ scrollTop: $marks.first().offset().top - 100 }, 300);
            flash($marks);
        }
    });

    $(document).on('mouseenter', '.comment-item[data-anchor-text]', function () {
        var commentId = $(this).attr('data-comment-id');
        $region.find('.anchored-highlight[data-comment-id="' + commentId + '"]').addClass('active');
    }).on('mouseleave', '.comment-item[data-anchor-text]', function () {
        var commentId = $(this).attr('data-comment-id');
        $region.find('.anchored-highlight[data-comment-id="' + commentId + '"]').removeClass('active');
    });

    /* ---------- Creating a new anchored comment ---------- */

    var $button = $('<a href="#" id="anchor-comment-btn" class="button tiny" style="display:none;">&#128172; Comment</a>').appendTo('body');
    var $popup = $(
        '<div id="anchor-comment-popup" style="display:none;">' +
        '  <div class="anchor-popup-quote"></div>' +
        '  <textarea rows="3" placeholder="Comment on the selected text..."></textarea>' +
        '  <button class="button tiny primary anchor-popup-post">Post</button> ' +
        '  <button class="button tiny secondary anchor-popup-cancel">Cancel</button>' +
        '</div>').appendTo('body');

    function hideButton() {
        $button.hide();
    }

    function hidePopup() {
        $popup.hide();
        pendingAnchor = null;
    }

    $(document).on('mouseup', function (e) {
        if ($(e.target).closest('#anchor-comment-btn, #anchor-comment-popup').length) { return; }
        // Let the selection settle before reading it
        setTimeout(function () {
            var sel = window.getSelection();
            if (!sel || sel.isCollapsed || sel.rangeCount === 0) { hideButton(); return; }
            var range = sel.getRangeAt(0);
            if (!regionEl.contains(range.commonAncestorContainer)) { hideButton(); return; }
            var text = range.toString();
            if (!text.trim()) { hideButton(); return; }

            var start = rangeStartOffset(range);
            var hay = regionText();
            pendingAnchor = {
                text: text,
                prefix: hay.substring(Math.max(0, start - CONTEXT_CHARS), start),
                suffix: hay.substring(start + text.length, start + text.length + CONTEXT_CHARS)
            };

            var rect = range.getBoundingClientRect();
            $button.css({
                top: rect.bottom + window.pageYOffset + 5,
                left: rect.left + window.pageXOffset
            }).show();
        }, 0);
    });

    $button.on('click', function (e) {
        e.preventDefault();
        if (!pendingAnchor) { return; }
        var pos = $button.position();
        hideButton();
        $popup.find('.anchor-popup-quote').text(
            pendingAnchor.text.length > 120 ? pendingAnchor.text.substring(0, 120) + '…' : pendingAnchor.text);
        $popup.find('textarea').val('');
        $popup.css({ top: pos.top, left: pos.left }).show();
        $popup.find('textarea').focus();
    });

    $popup.on('click', '.anchor-popup-cancel', function (e) {
        e.preventDefault();
        hidePopup();
    });

    $popup.on('click', '.anchor-popup-post', function (e) {
        e.preventDefault();
        var commentText = $popup.find('textarea').val().trim();
        if (!commentText || !pendingAnchor) { return; }
        $.post('/add_anchored_comment/', {
            question_type: questionType,
            question_id: questionId,
            comment_text: commentText,
            selected_text: pendingAnchor.text,
            prefix: pendingAnchor.prefix,
            suffix: pendingAnchor.suffix
        }, function (data) {
            var response = JSON.parse(data);
            if (response.message_class.indexOf('success') >= 0) {
                location.reload();
            } else {
                alert(response.message);
            }
        });
    });
});
