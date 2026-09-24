/* Edit | Read tabs on the edit-tossup and edit-bonus pages.
 *
 * The two panes (.qpane[data-pane]) share one spot: Edit is the editor card,
 * Read is the question as last saved, where anchored comments live. The tab
 * last used is remembered, so someone proofreading a set keeps reading and
 * someone editing one keeps editing.
 *
 * Requires: jQuery. Markup: _question_head.html and the two edit templates.
 */
$(function () {
    var $tabs = $('.qtab[data-tab]');
    if (!$tabs.length) { return; }

    var TAB_KEY = 'qx_question_tab';

    function remember(tab) {
        try { window.localStorage.setItem(TAB_KEY, tab); }
        catch (e) { /* private mode: the tab just is not remembered */ }
    }

    function remembered() {
        try { return window.localStorage.getItem(TAB_KEY); }
        catch (e) { return null; }
    }

    function show(tab) {
        $tabs.each(function () {
            var on = $(this).attr('data-tab') === tab;
            $(this).toggleClass('is-active', on).attr('aria-selected', on ? 'true' : 'false');
        });
        $('.qpane[data-pane]').each(function () {
            this.hidden = $(this).attr('data-pane') !== tab;
        });
        if (tab === 'edit') {
            // The card's toolbar is chosen from the fields that are showing,
            // and none were while Read was up.
            if (window.QemsRichEditor && window.QemsRichEditor.refreshSharedToolbars) {
                window.QemsRichEditor.refreshSharedToolbars();
            }
        } else {
            // Read shows the saved question, so say so when the editor holds
            // something newer.
            var $bar = $('.q-savebar');
            $('.qpane-note').prop('hidden', !($bar.length && !$bar.prop('hidden')));
        }
    }

    $tabs.on('click', function (e) {
        e.preventDefault();
        var tab = $(this).attr('data-tab');
        show(tab);
        remember(tab);
    });

    // A save that failed validation comes back to the editor whatever was
    // remembered: the errors are in the fields.
    var start = remembered() === 'read' && !$('.qedit-errors, .qedit-card .error').length
        ? 'read' : 'edit';
    show(start);

    // Clicking a comment's quoted text jumps to its highlight, which is on
    // the Read tab. Bound on the column rather than the document so it runs
    // before anchored_comments.js looks for the highlight.
    $('.edit-comments').on('click', '.comment-quote:not(.orphaned)', function () {
        if ($('.qpane[data-pane="read"]').prop('hidden')) { show('read'); }
    });
});

/* The byline and action row under the question (_question_meta.html). */
$(function () {
    var $card = $('.qmeta-card');
    if (!$card.length) { return; }

    // Status and More: a button and a panel under it, closed by a click
    // anywhere else or Escape.
    function closeMenus(except) {
        $card.find('.qmenu').not(except).each(function () {
            $(this).find('.qmenu-panel').prop('hidden', true);
            $(this).find('.qmenu-toggle').attr('aria-expanded', 'false');
        });
    }
    $card.on('click', '.qmenu-toggle', function (e) {
        e.preventDefault();
        var $menu = $(this).closest('.qmenu');
        var open = $menu.find('.qmenu-panel').prop('hidden');
        closeMenus($menu);
        $menu.find('.qmenu-panel').prop('hidden', !open);
        $(this).attr('aria-expanded', open ? 'true' : 'false');
    });
    $(document).on('mousedown', function (e) {
        if (!$(e.target).closest('.qmenu').length) { closeMenus(); }
    });
    $(document).on('keydown', function (e) {
        if (e.key === 'Escape') { closeMenus(); }
    });

    // The Status button says what is set, so the flags can be read without
    // opening it.
    var $status = $card.find('.qmeta-status');
    function summarize() {
        var on = $status.find('.qmenu-check').filter(function () {
            return $(this).find('input[type=checkbox]').prop('checked');
        }).map(function () { return $(this).find('span').text(); }).get();
        $status.find('.qmeta-status-summary').text(on.length ? on.join(', ') : 'None');
    }
    // All Power is also ticked by the page as the stem is typed, without a
    // change event, so listen for that too.
    $status.on('change', 'input', summarize);
    $(document).on('input', '.rich-editor, textarea', function () { setTimeout(summarize, 400); });
    summarize();

    // A select is as wide as its longest option; these read as words in a
    // sentence, so size each to the one it shows.
    var $measure = $('<span class="qmeta-measure" aria-hidden="true"></span>').appendTo($card);
    function fit(select) {
        var opt = select.options[select.selectedIndex];
        $measure.text(opt ? opt.text : '');
        $(select).css('width', ($measure.outerWidth() + 26) + 'px');
    }
    $card.find('.qmeta-inline select').each(function () { fit(this); })
        .on('change', function () { fit(this); });

    $card.find('.qmeta-credit input').attr('placeholder', 'none');
});
