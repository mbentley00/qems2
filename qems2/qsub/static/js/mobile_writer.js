/* The phone writer (/m/write/<set>/): screenshots of highlighted research in,
   a saved question out. One page, five steps, drawn from `S` (the state) by
   render(); every button carries a data-act that the click handler below
   dispatches on. The server does the thinking (mobile_writer_views.py); the
   page holds the work in progress, and keeps it in localStorage so a phone
   putting the tab to sleep loses nothing. */
(function () {
    'use strict';

    var CFG = JSON.parse(document.getElementById('mw-config').textContent);
    var BASE = '/m/write/' + CFG.qset.id + '/';
    var STORE = 'mw:' + CFG.qset.id;
    var app = document.getElementById('mw-app');

    var S = fresh();
    var shots = [];          // {blob, url}: not saved, a blob can't be
    var lastTap = {key: null, at: 0};

    function fresh() {
        return {
            step: 1, pasted: '', topic: '', sources: [], clues: [], newClue: '',
            kind: 'tossup', angles: null, angle: -1, answer: '', categoryId: null, notes: '',
            related: null, relatedFor: '', relTab: 'set',
            q: null, history: [], pending: null, ask: '',
            sel: null, mode: 'actions', changeAsk: '', proposal: null, quotes: null,
            lookupAnswer: '', lookupAsk: '', editText: '', editAnswer: '',
            check: null, saved: null, busy: '', error: '', toast: ''
        };
    }

    // --- persistence -------------------------------------------------------

    var TRANSIENT = ['busy', 'error', 'toast', 'sel', 'mode', 'proposal', 'quotes', 'lookupAnswer'];

    function persist() {
        try {
            var copy = {};
            Object.keys(S).forEach(function (k) { if (TRANSIENT.indexOf(k) < 0) { copy[k] = S[k]; } });
            localStorage.setItem(STORE, JSON.stringify(copy));
        } catch (e) { /* private mode or full: the page still works */ }
    }

    function restore() {
        try {
            var saved = JSON.parse(localStorage.getItem(STORE) || 'null');
            if (saved) { S = Object.assign(fresh(), saved); }
        } catch (e) { S = fresh(); }
    }

    // --- helpers -------------------------------------------------------------

    function esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
            return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
        });
    }

    /* QEMS markup for display: ~titles~ in italics, _required words_ bold and
       underlined. */
    function md(s) {
        return esc(s).replace(/~([^~]+)~/g, '<i>$1</i>').replace(/_([^_]+)_/g, '<b><u>$1</u></b>');
    }

    function csrf() {
        var el = document.querySelector('input[name=csrfmiddlewaretoken]');
        return el ? el.value : '';
    }

    function api(path, body, isForm) {
        return fetch(BASE + path + '/', {
            method: 'POST', credentials: 'same-origin',
            headers: isForm ? {'X-CSRFToken': csrf()}
                            : {'X-CSRFToken': csrf(), 'Content-Type': 'application/json'},
            body: isForm ? body : JSON.stringify(body)
        }).then(function (r) {
            return r.json().catch(function () { return {ok: false, error: 'The server sent back an error (' + r.status + ').'}; });
        }).then(function (data) {
            if (!data.ok) { throw new Error(data.error || 'Something went wrong.'); }
            return data;
        });
    }

    /* Run a request with the busy line showing; failures land in S.error. */
    function work(message, promise, then) {
        S.busy = message; S.error = ''; render();
        return promise.then(function (data) {
            S.busy = ''; then(data); persist(); render();
        }, function (err) {
            S.busy = ''; S.error = err.message || String(err); render();
        });
    }

    function shrink(file) {
        // Phone photos run to several MB; Claude reads 1600 px just as well.
        return new Promise(function (resolve) {
            var img = new Image();
            var url = URL.createObjectURL(file);
            img.onload = function () {
                var scale = Math.min(1, 1600 / Math.max(img.width, img.height));
                var c = document.createElement('canvas');
                c.width = Math.round(img.width * scale); c.height = Math.round(img.height * scale);
                c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
                URL.revokeObjectURL(url);
                c.toBlob(function (b) { resolve(b || file); }, 'image/jpeg', 0.85);
            };
            img.onerror = function () { URL.revokeObjectURL(url); resolve(file); };
            img.src = url;
        });
    }

    function addShots(files) {
        Array.prototype.slice.call(files).forEach(function (f) {
            if (!/^image\//.test(f.type) || shots.length >= 6) { return; }
            shrink(f).then(function (blob) {
                shots.push({blob: blob, url: URL.createObjectURL(blob)});
                render();
            });
        });
    }

    function category(id) {
        return CFG.categories.filter(function (c) { return c.id === id; })[0] || null;
    }

    function categoryProgress(c, kind) {
        var have = kind === 'bonus' ? c.bonuses : c.tossups;
        var want = kind === 'bonus' ? c.bonuses_wanted : c.tossups_wanted;
        return want == null ? have + ' written' : have + ' of ' + want + ' written';
    }

    function chosenClues() {
        return S.clues.filter(function (c) { return c.on; }).map(function (c) { return c.text; });
    }

    function clone(q) { return JSON.parse(JSON.stringify(q)); }

    function setQuestion(q, toast) {
        if (S.q) { S.history.push(clone(S.q)); S.history = S.history.slice(-20); }
        S.q = q; S.check = null; S.toast = toast || '';
    }

    /* Rough length while drafting; the Check step gets the set's real count. */
    function roughLength(q) {
        var text = S.kind === 'tossup' ? q.sentences.join(' ')
            : [q.leadin].concat(q.parts.map(function (p) { return p.text; })).join(' ');
        return text.replace(/\([^)]*"[^)]*\)/g, '').replace(/[_~]/g, '').length;
    }

    // --- diffing two versions of the question ------------------------------

    function lcsDiff(a, b) {
        var n = a.length, m = b.length, i, j, t = [];
        for (i = 0; i <= n; i++) { t.push(new Array(m + 1).fill(0)); }
        for (i = n - 1; i >= 0; i--) {
            for (j = m - 1; j >= 0; j--) {
                t[i][j] = a[i] === b[j] ? t[i + 1][j + 1] + 1 : Math.max(t[i + 1][j], t[i][j + 1]);
            }
        }
        var out = []; i = 0; j = 0;
        while (i < n && j < m) {
            if (a[i] === b[j]) { out.push({same: a[i]}); i++; j++; }
            else if (t[i + 1][j] >= t[i][j + 1]) { out.push({del: a[i]}); i++; }
            else { out.push({ins: b[j]}); j++; }
        }
        while (i < n) { out.push({del: a[i++]}); }
        while (j < m) { out.push({ins: b[j++]}); }
        return out;
    }

    function diffHtml(oldQ, newQ) {
        var rows = [];
        function pair(label, a, b) {
            if (a === b) { return; }
            rows.push('<p class="mw-serif mw-diff" style="margin:0;line-height:1.5">' + (label ? '<span class="mw-tag">' + label + '</span> ' : '')
                + (a ? '<del>' + md(a) + '</del> ' : '') + (b ? '<ins>' + md(b) + '</ins>' : '') + '</p>');
        }
        if (S.kind === 'tossup') {
            lcsDiff(oldQ.sentences, newQ.sentences).forEach(function (d) {
                if (d.del) { pair('', d.del, ''); }
                if (d.ins) { pair('', '', d.ins); }
            });
            pair('Answer', oldQ.answer, newQ.answer);
            if (oldQ.power_after !== newQ.power_after) {
                rows.push('<p class="mw-note" style="margin:0">Power mark moves to after sentence ' + newQ.power_after + '.</p>');
            }
        } else {
            pair('Leadin', oldQ.leadin, newQ.leadin);
            newQ.parts.forEach(function (p, k) {
                pair('Part ' + (k + 1), oldQ.parts[k].text, p.text);
                pair('Answer ' + (k + 1), oldQ.parts[k].answer, p.answer);
            });
        }
        return rows.length ? rows.join('') : '<p class="mw-note" style="margin:0">No changes.</p>';
    }

    // --- drawing -------------------------------------------------------------

    var ICON = {
        back: '<path d="M15 6l-6 6 6 6"></path>',
        up: '<path d="M12 19V5M6 11l6-6 6 6"></path>',
        down: '<path d="M12 5v14M6 13l6 6 6-6"></path>',
        pen: '<path d="M4 20h4L19 9l-4-4L4 16z"></path>',
        search: '<circle cx="11" cy="11" r="7"></circle><path d="M20 20l-3.5-3.5"></path>',
        trash: '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"></path>',
        star: '<path d="M12 4l2.4 5 5.6.6-4.2 3.8 1.2 5.6L12 16.2 7 19l1.2-5.6L4 9.6l5.6-.6z"></path>',
        type: '<path d="M5 6h14M12 6v13M9 19h6"></path>',
        plus: '<path d="M12 5v14M5 12h14"></path>',
        send: '<path d="M12 19V5M6 11l6-6 6 6"></path>',
        x: '<path d="M6 6l12 12M18 6L6 18"></path>'
    };

    function icon(name) {
        return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + ICON[name] + '</svg>';
    }

    var TITLES = ['', 'New question', 'What are you writing?', 'Already out there', 'Draft', 'Check and save'];

    function header() {
        var kicker = S.step === 1 ? CFG.qset.name
            : (S.kind === 'tossup' ? 'Tossup' : 'Bonus') + (S.topic ? ' · ' + S.topic : '');
        var back = S.step === 1
            ? '<a class="mw-icon-btn" href="/m/write/" aria-label="Choose another set">' + icon('back') + '</a>'
            : '<button type="button" class="mw-icon-btn" data-act="back" aria-label="Back">' + icon('back') + '</button>';
        var steps = '';
        for (var i = 1; i <= 5; i++) { steps += '<span class="' + (i <= S.step ? 'on' : '') + '"></span>'; }
        var extra = S.step === 4 && S.q
            ? '<span class="mw-chip" style="display:flex;align-items:center;cursor:default">≈' + roughLength(S.q) + ' / ' + CFG.limits[S.kind] + '</span>' : '';
        return '<div class="mw-top">' + back + '<div class="mw-top-text"><span class="mw-kicker">' + esc(kicker)
            + '</span><span class="mw-title">' + TITLES[S.step] + '</span></div>' + extra + '</div>'
            + '<div class="mw-steps">' + steps + '</div>';
    }

    function status() {
        var out = '';
        if (S.busy) { out += '<div class="mw-busy"><span class="mw-spin"></span>' + esc(S.busy) + '</div>'; }
        if (S.error) { out += '<div class="mw-warn" role="alert">' + esc(S.error) + '</div>'; }
        if (!CFG.ai) { out += '<div class="mw-warn">AI features aren\'t configured on this server (no API key), so nothing here will work yet.</div>'; }
        return out;
    }

    function stepCapture() {
        var tiles = shots.map(function (s, i) {
            return '<div class="mw-shot"><img src="' + s.url + '" alt="Screenshot ' + (i + 1) + '">'
                + '<button type="button" data-act="drop-shot" data-i="' + i + '" aria-label="Remove screenshot ' + (i + 1) + '">×</button></div>';
        }).join('');
        if (shots.length < 6) {
            tiles += '<label class="mw-add-shot">' + icon('plus').replace('<svg ', '<svg width="22" height="22" ')
                + 'Add screenshots<input type="file" accept="image/*" multiple data-in="files" hidden></label>';
        }
        var clues = S.clues.map(function (c, i) {
            return '<label class="mw-card mw-clue"><input type="checkbox" data-in="clue" data-i="' + i + '"' + (c.on ? ' checked' : '') + '>'
                + '<span style="display:flex;flex-direction:column;gap:3px"><span class="mw-clue-text">' + md(c.text) + '</span>'
                + (c.quote ? '<span class="mw-note">“' + esc(c.quote) + '”' + (c.source != null && S.sources[c.source] ? ' · ' + esc(S.sources[c.source].label) : '') + '</span>' : '')
                + '</span></label>';
        }).join('');
        var body = '<div class="mw-shots">' + tiles + '</div>'
            + '<label class="mw-label" for="mw-paste">Or paste text (bracket the parts you want)</label>'
            + '<textarea id="mw-paste" class="mw-field" data-in="pasted" placeholder="Paste from anywhere…">' + esc(S.pasted) + '</textarea>'
            + '<button type="button" class="mw-btn mw-btn-quiet" data-act="extract"' + (shots.length || S.pasted.trim() ? '' : ' disabled') + '>Read my highlights</button>'
            + status();
        if (S.clues.length || S.topic) {
            body += '<label class="mw-label" for="mw-topic">Topic</label>'
                + '<input id="mw-topic" class="mw-field" data-in="topic" value="' + esc(S.topic) + '">'
                + '<div class="mw-row" style="justify-content:space-between"><span class="mw-label">Clues</span><span class="mw-note">' + chosenClues().length + ' of ' + S.clues.length + ' chosen</span></div>'
                + clues;
        } else {
            body += '<p class="mw-note" style="margin:0">Snap or paste screenshots of what you highlighted, or skip that and add clues by hand below.</p>'
                + '<label class="mw-label" for="mw-topic">Topic</label>'
                + '<input id="mw-topic" class="mw-field" data-in="topic" value="' + esc(S.topic) + '" placeholder="Who or what is it about?">';
        }
        body += '<div class="mw-row"><input class="mw-field mw-grow" data-in="newClue" aria-label="Add a clue by hand" placeholder="Add a clue by hand" value="' + esc(S.newClue) + '">'
            + '<button type="button" class="mw-btn mw-btn-quiet mw-btn-small" data-act="add-clue">Add</button></div>';
        var foot = '<button type="button" class="mw-btn" data-act="to-plan"' + (S.topic.trim() && chosenClues().length ? '' : ' disabled') + '>Use ' + chosenClues().length + ' clue' + (chosenClues().length === 1 ? '' : 's') + '</button>'
            + (S.clues.length || S.q ? '<button type="button" class="mw-btn mw-btn-quiet mw-btn-small" data-act="start-over">Start over</button>' : '');
        return {body: body, foot: foot};
    }

    function stepPlan() {
        var kinds = CFG.qset.tossups_only ? '' : '<div class="mw-seg">'
            + ['tossup', 'bonus'].map(function (k) {
                return '<button type="button" data-act="kind" data-kind="' + k + '" class="' + (S.kind === k ? 'on' : '') + '">' + (k === 'tossup' ? 'Tossup' : 'Bonus') + '</button>';
            }).join('') + '</div>';
        var angles = (S.angles || []).map(function (a, i) {
            return '<button type="button" class="mw-card mw-card-pick' + (S.angle === i ? ' on' : '') + '" data-act="angle" data-i="' + i + '">'
                + '<span style="font-weight:600">' + md(a.title) + '</span><span class="mw-note">' + esc(a.note) + '</span></button>';
        }).join('');
        var options = '<option value="">Choose a category</option>' + CFG.categories.map(function (c) {
            return '<option value="' + c.id + '"' + (S.categoryId === c.id ? ' selected' : '') + '>' + esc(c.label) + '</option>';
        }).join('');
        var cat = category(S.categoryId);
        var body = kinds + status()
            + (angles ? '<span class="mw-label">Answer ideas</span>' + angles
                + '<button type="button" class="mw-btn mw-btn-quiet mw-btn-small" data-act="replan">Other ideas</button>' : '')
            + '<label class="mw-label" for="mw-answer">Answer line</label>'
            + '<input id="mw-answer" class="mw-field mw-serif" data-in="answer" value="' + esc(S.answer) + '" placeholder="_Required words_ [accept …]">'
            + '<label class="mw-label" for="mw-cat">Category</label>'
            + '<select id="mw-cat" class="mw-field" data-in="category">' + options + '</select>'
            + (cat ? '<span class="mw-note">' + esc(cat.label) + ': ' + categoryProgress(cat, S.kind) + '</span>' : '')
            + '<label class="mw-label" for="mw-notes">Notes for the draft (optional)</label>'
            + '<input id="mw-notes" class="mw-field" data-in="notes" value="' + esc(S.notes) + '" placeholder="e.g. lead with the Van Dyck sketch">';
        var foot = '<button type="button" class="mw-btn" data-act="to-related"' + (S.answer.trim() && S.categoryId ? '' : ' disabled') + '>Check what\'s out there</button>';
        return {body: body, foot: foot};
    }

    function stepRelated() {
        var r = S.related;
        var tabs = '<div class="mw-row">' + [['set', 'This set' + (r ? ' · ' + r.set_hits.length : '')], ['qb', 'QB Reader']].map(function (t) {
            return '<button type="button" class="mw-chip' + (S.relTab === t[0] ? ' on' : '') + '" data-act="rel-tab" data-tab="' + t[0] + '">' + t[1] + '</button>';
        }).join('') + '</div>';
        var body = tabs + status();
        if (r && S.relTab === 'set') {
            body += r.set_hits.length ? r.set_hits.map(function (h) {
                return '<a class="mw-card mw-hit" href="' + h.url + '" target="_blank" rel="noopener" style="text-decoration:none;color:inherit">'
                    + '<span class="mw-row"><span class="mw-tag">' + h.kind + '</span><span class="mw-note">' + esc(h.where) + '</span></span>'
                    + '<span class="mw-serif" style="font-size:14px;line-height:1.4">' + esc(h.text) + '</span>'
                    + '<span class="mw-note">ANSWER: <b>' + esc(h.answer) + '</b></span></a>';
            }).join('') : '<p class="mw-ok" style="margin:0">Nothing in this set mentions ' + esc(r.answer) + ' yet.</p>';
        }
        if (r && S.relTab === 'qb') {
            var qb = r.qbreader || {};
            if (qb.error) {
                body += '<div class="mw-warn">' + esc(qb.error) + '</div>';
            } else {
                body += '<div class="mw-row" style="justify-content:space-between"><span><b>' + qb.tossups + ' tossups · ' + qb.bonuses + ' bonuses</b> mention ' + esc(r.answer) + '</span>'
                    + '<a href="https://www.qbreader.org/db?query=' + encodeURIComponent(r.answer) + '" target="_blank" rel="noopener">See all</a></div>';
                var counts = qb.clue_counts || [];
                var top = Math.max.apply(null, [1].concat(counts.map(function (c) { return c.count; })));
                if (counts.length) {
                    // A clue in more than a quarter of the past questions is worn.
                    body += '<span class="mw-label">How many of those use each clue</span>' + counts.map(function (c) {
                        return '<div class="mw-card" style="display:flex;flex-direction:column;gap:5px"><div class="mw-row" style="justify-content:space-between"><span>' + esc(c.term)
                            + '</span><b>' + c.count + '</b></div><div class="mw-bar"><span class="' + (c.count / Math.max(qb.searched, 1) > 0.25 ? 'hot' : '') + '" style="width:' + Math.round(c.count / top * 100) + '%"></span></div></div>';
                    }).join('') + '<span class="mw-note">Counted across the ' + qb.searched + ' past questions on QB Reader that mention ' + esc(r.answer) + '.</span>';
                }
                body += (qb.items || []).map(function (h) {
                    return '<div class="mw-card mw-hit"><span class="mw-row"><span class="mw-tag">' + h.kind + '</span><span class="mw-note">' + esc(h.set) + '</span></span>'
                        + '<span class="mw-serif" style="font-size:14px;line-height:1.4">' + esc(h.text.slice(0, 320)) + (h.text.length > 320 ? '…' : '') + '</span>'
                        + '<span class="mw-note">ANSWER: <b>' + esc(h.answer) + '</b></span></div>';
                }).join('');
            }
        }
        var foot = S.q
            ? '<button type="button" class="mw-btn" data-act="to-draft">Back to the draft</button><button type="button" class="mw-btn mw-btn-quiet mw-btn-small" data-act="redraft">Start a fresh draft</button>'
            : '<button type="button" class="mw-btn" data-act="redraft">Draft it</button>';
        return {body: body, foot: foot};
    }

    function stepDraft() {
        var q = S.q;
        var body = '<div class="mw-row"><span class="mw-note mw-grow">Double-tap a sentence to move, change or look it up</span>'
            + '<button type="button" class="mw-chip" data-act="research" style="display:flex;align-items:center;gap:6px">' + icon('search').replace('<svg ', '<svg width="14" height="14" ') + 'Research</button></div>';
        var card = '';
        if (S.kind === 'tossup') {
            card = '<p class="mw-question">' + q.sentences.map(function (s, i) {
                return '<span class="mw-sent' + (i < q.power_after ? ' power' : '') + (S.sel === i ? ' sel' : '') + '" data-seg="' + i + '">' + md(s) + '</span>'
                    + (q.power_after && i === q.power_after - 1 && i < q.sentences.length - 1 ? ' <b>(*)</b>' : '');
            }).join(' ') + '</p>'
                + '<p class="mw-answer"><span class="mw-sent' + (S.sel === 'answer' ? ' sel' : '') + '" data-seg="answer">ANSWER: ' + md(q.answer) + '</span></p>';
        } else {
            card = '<p class="mw-question"><span class="mw-sent' + (S.sel === 'leadin' ? ' sel' : '') + '" data-seg="leadin">' + md(q.leadin) + '</span></p>'
                + q.parts.map(function (p, k) {
                    return '<div class="mw-part"><p class="mw-question" style="font-size:16px"><span class="mw-tag">[10' + (p.difficulty ? p.difficulty : '') + ']</span> '
                        + '<span class="mw-sent' + (S.sel === k ? ' sel' : '') + '" data-seg="' + k + '">' + md(p.text) + '</span></p>'
                        + '<p class="mw-answer">ANSWER: ' + md(p.answer) + '</p></div>';
                }).join('');
        }
        body += '<div class="mw-card" style="display:flex;flex-direction:column;gap:12px;padding:16px">' + card + '</div>';
        if (S.pending) {
            body += '<div class="mw-bubble">' + esc(S.pending.ask) + '</div>'
                + '<div class="mw-card mw-proposal" style="display:flex;flex-direction:column;gap:10px">'
                + '<span class="mw-proposal-head">Suggested revision</span>'
                + (S.pending.summary ? '<span class="mw-note">' + esc(S.pending.summary) + '</span>' : '')
                + diffHtml(S.q, S.pending.q)
                + '<div class="mw-actions"><button type="button" class="mw-btn mw-btn-quiet mw-btn-small" data-act="reject-pending">Keep mine</button>'
                + '<button type="button" class="mw-btn mw-btn-small" data-act="accept-pending">Accept</button></div></div>';
        }
        if (S.toast) {
            body += '<div class="mw-toast"><span class="mw-grow">' + esc(S.toast) + '</span>'
                + (S.history.length ? '<button type="button" data-act="undo">Undo</button>' : '') + '</div>';
        }
        body += status();
        var foot = '<div class="mw-composer"><input data-in="ask" aria-label="Tell it what to fix in the whole question" placeholder="Tell it what to fix…" value="' + esc(S.ask) + '">'
            + '<button type="button" class="mw-round" data-act="send-ask" aria-label="Send"' + (S.busy ? ' disabled' : '') + '>' + icon('send').replace('<svg ', '<svg width="20" height="20" ') + '</button></div>'
            + '<button type="button" class="mw-btn mw-btn-quiet" data-act="to-check">Looks good — check it</button>';
        return {body: body, foot: foot};
    }

    function stepCheck() {
        var c = S.check, cat = category(S.categoryId);
        var options = CFG.categories.map(function (x) {
            return '<option value="' + x.id + '"' + (S.categoryId === x.id ? ' selected' : '') + '>' + esc(x.label) + '</option>';
        }).join('');
        var body = status();
        if (S.saved) {
            body += '<div class="mw-ok">Saved to ' + esc(CFG.qset.name) + '. <a href="' + S.saved.url + '">Open it in QEMS</a></div>';
        }
        if (c) {
            var over = c.chars > c.limit;
            body += '<div class="mw-card" style="display:flex;flex-direction:column;gap:8px">'
                + '<span class="' + (over ? 'mw-warn' : 'mw-ok') + '">' + c.chars + ' of ' + c.limit + ' characters' + (over ? ' — over the limit' : '') + '</span>'
                + (c.style.length ? c.style.map(function (i) { return '<span style="font-size:14px">• ' + esc(i.message) + '</span>'; }).join('')
                    : '<span style="font-size:14px">No style issues.</span>')
                + '</div>';
            if (c.repeats.length) {
                body += '<div class="mw-warn"><b>Same answer already in this set:</b>' + c.repeats.map(function (r) {
                    return '<br><a href="' + r.url + '" target="_blank" rel="noopener">' + esc(r.answer) + '</a> <span>' + esc([r.where, r.packet].filter(Boolean).join(' · ')) + '</span>';
                }).join('') + '</div>';
            }
        }
        body += '<label class="mw-label" for="mw-cat2">Category</label><select id="mw-cat2" class="mw-field" data-in="category">' + options + '</select>'
            + (cat ? '<span class="mw-note">' + categoryProgress(cat, S.kind) + '</span>' : '');
        var unused = S.clues.filter(function (x) { return x.on && !usedClue(x.text); });
        if (unused.length && !S.saved) {
            body += '<div class="mw-card"><span class="mw-label">Highlights not used yet</span>' + unused.map(function (x) {
                return '<p class="mw-serif" style="margin:6px 0 0;font-size:14px">' + md(x.text) + '</p>';
            }).join('') + '</div>';
        }
        var foot = S.saved
            ? '<button type="button" class="mw-btn" data-act="another">Write another from these highlights</button><button type="button" class="mw-btn mw-btn-quiet mw-btn-small" data-act="start-over">Start over with new material</button>'
            : '<button type="button" class="mw-btn" data-act="save"' + (S.busy ? ' disabled' : '') + '>Save ' + S.kind + '</button>';
        return {body: body, foot: foot};
    }

    /* Rough check of whether a highlight made it into the question: most of
       its longer words appear in the text. */
    function usedClue(text) {
        if (!S.q) { return false; }
        var hay = (S.kind === 'tossup' ? S.q.sentences.join(' ') : [S.q.leadin].concat(S.q.parts.map(function (p) { return p.text; })).join(' ')).toLowerCase();
        var words = text.toLowerCase().match(/[a-zÀ-ɏ]{5,}/g) || [];
        if (!words.length) { return false; }
        var hits = words.filter(function (w) { return hay.indexOf(w) >= 0; }).length;
        return hits / words.length >= 0.5;
    }

    // --- the sentence sheet -----------------------------------------------------

    function segText(sel) {
        var q = S.q;
        if (sel === 'topic') { return ''; }
        if (sel === 'answer') { return q.answer; }
        if (S.kind === 'tossup') { return q.sentences[sel]; }
        return sel === 'leadin' ? q.leadin : q.parts[sel].text;
    }

    function segLabel(sel) {
        if (sel === 'topic') { return 'Research · ' + (S.topic || 'topic'); }
        if (sel === 'answer') { return 'Answer line'; }
        if (S.kind === 'bonus') { return sel === 'leadin' ? 'Leadin' : 'Part ' + (sel + 1); }
        var n = S.q.sentences.length;
        var where = sel < S.q.power_after ? ' · in power' : (sel === n - 1 ? ' · giveaway' : '');
        return 'Sentence ' + (sel + 1) + ' of ' + n + where;
    }

    function sheet() {
        if (S.sel === null || !S.q) { return ''; }
        var sel = S.sel, tossupSentence = S.kind === 'tossup' && typeof sel === 'number';
        var html = '<div class="mw-grip"></div><div style="display:flex;flex-direction:column;gap:4px"><span class="mw-kicker">' + esc(segLabel(sel)) + '</span>'
            + (sel !== 'topic' ? '<span class="mw-serif" style="font-size:15px;line-height:1.45">' + md(segText(sel)) + '</span>' : '') + '</div>';

        if (S.mode === 'actions') {
            var n = S.kind === 'tossup' ? S.q.sentences.length : 0;
            html += '<div class="mw-actions">';
            if (tossupSentence) {
                html += '<button type="button" class="mw-action" data-act="move" data-d="-1"' + (sel === 0 ? ' disabled' : '') + '>' + icon('up') + 'Move earlier</button>'
                    + '<button type="button" class="mw-action" data-act="move" data-d="1"' + (sel === n - 1 ? ' disabled' : '') + '>' + icon('down') + 'Move later</button>';
            }
            html += '<button type="button" class="mw-action" data-act="mode" data-mode="change">' + icon('pen') + 'Change</button>'
                + '<button type="button" class="mw-action" data-act="lookup">' + icon('search') + 'Look up more</button>'
                + '<button type="button" class="mw-action" data-act="mode" data-mode="edit">' + icon('type') + 'Edit by hand</button>';
            if (tossupSentence) {
                html += '<button type="button" class="mw-action" data-act="power"' + (sel >= n - 1 ? ' disabled' : '') + '>' + icon('star') + 'Power mark after this</button>'
                    + '<button type="button" class="mw-action wide danger" data-act="delete">' + icon('trash') + 'Delete sentence</button>';
            }
            html += '</div>';
        }

        if (S.mode === 'change') {
            var chips = ['Harder', 'Easier', 'Shorter', 'More precise', 'Check the fact against the sources'];
            html += '<div class="mw-row">' + chips.map(function (c) {
                return '<button type="button" class="mw-chip" data-act="change-chip" data-text="' + esc(c) + '">' + esc(c) + '</button>';
            }).join('') + '</div>'
                + '<div class="mw-composer"><input data-in="changeAsk" aria-label="How should it change?" placeholder="Or say it your way…" value="' + esc(S.changeAsk) + '">'
                + '<button type="button" class="mw-round" data-act="change-send" aria-label="Ask for a rewrite">' + icon('send').replace('<svg ', '<svg width="20" height="20" ') + '</button></div>';
            html += proposalHtml() + status() + '<button type="button" class="mw-btn mw-btn-quiet mw-btn-small" data-act="mode" data-mode="actions">Back</button>';
        }

        if (S.mode === 'edit') {
            var bonusPart = S.kind === 'bonus' && typeof sel === 'number';
            html += '<textarea class="mw-field mw-serif" data-in="editText" aria-label="Text">' + esc(S.editText) + '</textarea>'
                + (bonusPart ? '<label class="mw-label">Answer<input class="mw-field mw-serif" data-in="editAnswer" value="' + esc(S.editAnswer) + '"></label>' : '')
                + '<div class="mw-actions"><button type="button" class="mw-btn mw-btn-quiet mw-btn-small" data-act="mode" data-mode="actions">Back</button>'
                + '<button type="button" class="mw-btn mw-btn-small" data-act="edit-save">Use this</button></div>';
        }

        if (S.mode === 'lookup') {
            html += status();
            if (S.quotes) {
                html += S.quotes.length ? S.quotes.map(function (q, i) {
                    var quote = esc(q.quote);
                    if (q.key) {
                        var at = q.quote.toLowerCase().indexOf(q.key.toLowerCase());
                        if (at >= 0) {
                            quote = esc(q.quote.slice(0, at)) + '<mark>' + esc(q.quote.slice(at, at + q.key.length)) + '</mark>' + esc(q.quote.slice(at + q.key.length));
                        }
                    }
                    return '<div class="mw-card" style="display:flex;flex-direction:column;gap:8px"><span class="mw-row"><span class="mw-tag">' + esc(q.label) + '</span></span>'
                        + '<blockquote class="mw-quote">“' + quote + '”</blockquote>'
                        + '<div class="mw-row"><button type="button" class="mw-btn mw-btn-small" data-act="quote-clue" data-i="' + i + '">Turn into a clue</button>'
                        + '<button type="button" class="mw-btn mw-btn-quiet mw-btn-small" data-act="quote-copy" data-i="' + i + '">Copy quote</button>'
                        + (q.url ? '<a class="mw-btn mw-btn-quiet mw-btn-small" href="' + esc(q.url) + '" target="_blank" rel="noopener">Open source</a>' : '')
                        + '</div></div>';
                }).join('') : '<p class="mw-note" style="margin:0">No passage in your sources could be quoted word for word for this.</p>';
                if (S.lookupAnswer) { html += '<div class="mw-card" style="font-size:14px">' + esc(S.lookupAnswer) + '</div>'; }
                html += '<span class="mw-note">Quotes are checked against the source text and dropped unless they match word for word.</span>';
            }
            html += proposalHtml()
                + '<div class="mw-composer"><input data-in="lookupAsk" aria-label="Ask a follow-up question" placeholder="Ask about this… e.g. what year?" value="' + esc(S.lookupAsk) + '">'
                + '<button type="button" class="mw-round" data-act="lookup-ask" aria-label="Ask">' + icon('send').replace('<svg ', '<svg width="20" height="20" ') + '</button></div>'
                + '<button type="button" class="mw-btn mw-btn-quiet mw-btn-small" data-act="' + (sel === 'topic' ? 'close' : 'mode') + '" data-mode="actions">' + (sel === 'topic' ? 'Done' : 'Back') + '</button>';
        }

        return '<div class="mw-scrim"><button type="button" class="mw-scrim-close" data-act="close" aria-label="Close"></button>'
            + '<div class="mw-sheet" role="dialog" aria-label="' + esc(segLabel(sel)) + '">' + html + '</div></div>';
    }

    function proposalHtml() {
        if (!S.proposal) { return ''; }
        return '<div class="mw-card mw-proposal" style="display:flex;flex-direction:column;gap:8px">'
            + '<span class="mw-proposal-head">Suggested</span>'
            + (S.proposal.summary ? '<span class="mw-note">' + esc(S.proposal.summary) + '</span>' : '')
            + diffHtml(S.q, S.proposal.q)
            + '<button type="button" class="mw-btn mw-btn-small" data-act="use-proposal">Use this</button></div>';
    }

    function render() {
        var parts = [null, stepCapture, stepPlan, stepRelated, stepDraft, stepCheck][S.step]();
        var focused = document.activeElement && document.activeElement.getAttribute('data-in');
        app.innerHTML = header() + '<div class="mw-body">' + parts.body + '</div><div class="mw-foot">' + parts.foot + '</div>'
            + (S.step === 4 ? sheet() : '');
        if (focused) {
            var again = app.querySelector('[data-in="' + focused + '"]');
            if (again && again.focus) {
                again.focus();
                if (again.setSelectionRange && again.value != null) {
                    try { again.setSelectionRange(again.value.length, again.value.length); } catch (e) { /* select */ }
                }
            }
        }
    }

    // --- steps ---------------------------------------------------------------

    function go(step) { S.step = step; S.error = ''; S.sel = null; persist(); render(); window.scrollTo(0, 0); }

    function plan() {
        return work('Thinking of answers…', api('plan', {kind: S.kind, topic: S.topic, clues: chosenClues()}), function (d) {
            S.angles = d.angles; S.angle = -1;
            if (d.angles.length && !S.answer) { pickAngle(0); }
        });
    }

    function pickAngle(i) {
        var a = S.angles[i];
        S.angle = i; S.answer = a.answer_line;
        if (a.category_id) { S.categoryId = a.category_id; }
    }

    function related() {
        var terms = S.clues.filter(function (c) { return c.on && c.search; }).map(function (c) { return c.search; });
        return work('Searching this set and QB Reader…', api('related', {answer: S.answer, topic: S.topic, terms: terms}), function (d) {
            S.related = d; S.relatedFor = S.answer;
        });
    }

    function draft() {
        S.pending = null;
        return work('Drafting…', api('draft', {kind: S.kind, answer: S.answer, clues: chosenClues(), sources: S.sources, notes: S.notes}), function (d) {
            S.history = []; S.q = d.question; S.check = null; S.toast = ''; S.step = 4;
        });
    }

    function revise(instruction, target, then) {
        return work('Revising…', api('revise', {kind: S.kind, question: S.q, instruction: instruction, target: target, sources: S.sources}), then)
            .then(function () {
                // The suggestion lands below the question; bring it into view.
                var card = app.querySelector('.mw-proposal');
                if (card) { card.scrollIntoView({behavior: 'smooth', block: 'nearest'}); }
            });
    }

    function lookup(ask) {
        var sel = S.sel;
        S.proposal = null;
        return work('Looking it up…', api('lookup', {topic: S.topic, focus: sel === 'topic' ? '' : segText(sel), ask: ask || '',
            sources: S.sources, used: S.kind === 'tossup' ? S.q.sentences : []}), function (d) {
            S.quotes = d.quotes; S.lookupAnswer = d.answer; S.lookupAsk = '';
        });
    }

    function check() {
        return work('Checking…', api('check', {kind: S.kind, question: S.q, category_id: S.categoryId}), function (d) {
            S.check = d;
        });
    }

    function openSheet(sel) {
        S.sel = sel; S.mode = 'actions'; S.proposal = null; S.quotes = null; S.lookupAnswer = ''; S.changeAsk = ''; S.error = '';
        render();
    }

    function targetFor(sel) {
        return sel === 'topic' ? null : sel;
    }

    // --- events --------------------------------------------------------------

    app.addEventListener('click', function (e) {
        var el = e.target.closest('[data-act]');
        if (!el || el.disabled) { return; }
        var act = el.getAttribute('data-act'), i = parseInt(el.getAttribute('data-i'), 10);
        var q = S.q;

        switch (act) {
        case 'back':
            go(S.step - 1); break;
        case 'drop-shot':
            URL.revokeObjectURL(shots[i].url); shots.splice(i, 1); render(); break;
        case 'extract':
            var form = new FormData();
            shots.forEach(function (s, k) { form.append('images', s.blob, 'shot' + (k + 1) + '.jpg'); });
            form.append('text', S.pasted);
            work('Reading your highlights…', api('extract', form, true), function (d) {
                S.topic = d.topic || S.topic; S.sources = d.sources;
                S.clues = d.clues.map(function (c) { return Object.assign({on: true}, c); });
                S.angles = null; S.related = null;
            });
            break;
        case 'add-clue':
            if (S.newClue.trim()) { S.clues.push({text: S.newClue.trim(), quote: '', source: null, search: '', on: true}); S.newClue = ''; persist(); render(); }
            break;
        case 'start-over':
            shots = []; S = fresh(); persist(); render(); break;
        case 'to-plan':
            go(2); if (!S.angles) { plan(); } break;
        case 'kind':
            if (S.kind !== el.getAttribute('data-kind')) { S.kind = el.getAttribute('data-kind'); S.answer = ''; S.q = null; S.angles = null; plan(); }
            break;
        case 'replan':
            S.answer = ''; plan(); break;
        case 'angle':
            pickAngle(i); persist(); render(); break;
        case 'to-related':
            go(3); if (!S.related || S.relatedFor !== S.answer) { related(); } break;
        case 'rel-tab':
            S.relTab = el.getAttribute('data-tab'); render(); break;
        case 'redraft':
            draft(); break;
        case 'to-draft':
            go(4); break;
        case 'research':
            openSheet('topic'); S.mode = 'lookup'; lookup(''); break;
        case 'send-ask':
            if (!S.ask.trim()) { break; }
            var ask = S.ask.trim(); S.ask = '';
            revise(ask, null, function (d) { S.pending = {ask: ask, q: d.question, summary: d.summary}; });
            break;
        case 'accept-pending':
            setQuestion(S.pending.q, 'Revision applied.'); S.pending = null; persist(); render(); break;
        case 'reject-pending':
            S.pending = null; render(); break;
        case 'undo':
            if (S.history.length) { S.q = S.history.pop(); S.toast = ''; S.check = null; persist(); render(); }
            break;
        case 'to-check':
            go(5); check(); break;
        case 'save':
            work('Saving…', api('save', {kind: S.kind, question: S.q, category_id: S.categoryId}), function (d) {
                S.saved = d;
                var c = category(S.categoryId);
                if (c) { if (S.kind === 'bonus') { c.bonuses += 1; } else { c.tossups += 1; } }
            });
            break;
        case 'another':
            S.clues.forEach(function (c) { if (usedClue(c.text)) { c.on = false; } });
            S.q = null; S.history = []; S.angles = null; S.angle = -1; S.answer = ''; S.related = null;
            S.check = null; S.saved = null; S.pending = null; S.notes = '';
            go(2); plan(); break;

        // the sheet
        case 'close':
            S.sel = null; render(); break;
        case 'mode':
            S.mode = el.getAttribute('data-mode'); S.proposal = null; S.error = '';
            if (S.mode === 'edit') {
                S.editText = segText(S.sel);
                S.editAnswer = S.kind === 'bonus' && typeof S.sel === 'number' ? q.parts[S.sel].answer : '';
            }
            render(); break;
        case 'move':
            var d = parseInt(el.getAttribute('data-d'), 10), j = S.sel + d;
            var next = clone(q), t = next.sentences[S.sel];
            next.sentences[S.sel] = next.sentences[j]; next.sentences[j] = t;
            setQuestion(next, d < 0 ? 'Moved earlier.' : 'Moved later.'); S.sel = null; persist(); render(); break;
        case 'delete':
            var cut = clone(q); cut.sentences.splice(S.sel, 1);
            cut.power_after = Math.min(cut.power_after - (S.sel < q.power_after ? 1 : 0), Math.max(cut.sentences.length - 1, 0));
            setQuestion(cut, 'Sentence deleted.'); S.sel = null; persist(); render(); break;
        case 'power':
            var p = clone(q); p.power_after = S.sel + 1;
            setQuestion(p, 'Power mark moved.'); S.sel = null; persist(); render(); break;
        case 'edit-save':
            var ed = clone(q), text = S.editText.trim();
            if (S.sel === 'answer') { ed.answer = text; }
            else if (S.kind === 'tossup') { ed.sentences[S.sel] = text; }
            else if (S.sel === 'leadin') { ed.leadin = text; }
            else { ed.parts[S.sel].text = text; ed.parts[S.sel].answer = S.editAnswer.trim(); }
            setQuestion(ed, 'Edited.'); S.sel = null; persist(); render(); break;
        case 'change-chip':
            S.changeAsk = el.getAttribute('data-text');
            /* falls through */
        case 'change-send':
            if (!S.changeAsk.trim()) { break; }
            revise(S.changeAsk.trim(), targetFor(S.sel), function (r) { S.proposal = {q: r.question, summary: r.summary}; });
            break;
        case 'use-proposal':
            setQuestion(S.proposal.q, S.proposal.summary || 'Changed.'); S.proposal = null; S.sel = null; persist(); render(); break;
        case 'lookup':
            S.mode = 'lookup'; lookup(''); break;
        case 'lookup-ask':
            if (S.lookupAsk.trim()) { lookup(S.lookupAsk.trim()); }
            break;
        case 'quote-copy':
            if (navigator.clipboard) { navigator.clipboard.writeText(S.quotes[i].quote); }
            el.textContent = 'Copied'; break;
        case 'quote-clue':
            var quote = S.quotes[i];
            var where = S.sel === 'topic' || S.kind === 'bonus'
                ? 'Add it as one new sentence wherever it fits the pyramid.'
                : 'Add it as one new sentence right after this sentence.';
            revise('Add a new clue based on this passage from ' + quote.label + ': "' + quote.quote + '". ' + where,
                S.kind === 'tossup' && typeof S.sel === 'number' ? S.sel : null,
                function (r) { S.proposal = {q: r.question, summary: r.summary}; });
            break;
        }
    });

    // Typing updates the state but doesn't redraw (that would move the caret);
    // only the buttons that depend on what's typed are touched.
    function enable(act, on) {
        var btn = app.querySelector('[data-act="' + act + '"]');
        if (btn) { btn.disabled = !on; }
    }

    var CHOICES = ['files', 'clue', 'category'];   // handled on change, below

    app.addEventListener('input', function (e) {
        var key = e.target.getAttribute('data-in');
        if (!key || CHOICES.indexOf(key) >= 0) { return; }
        S[key] = e.target.value;
        if (key === 'answer') { S.angle = -1; }
        persist();
        enable('extract', shots.length || S.pasted.trim());
        enable('to-plan', S.topic.trim() && chosenClues().length);
        enable('to-related', S.answer.trim() && S.categoryId);
    });

    app.addEventListener('change', function (e) {
        var key = e.target.getAttribute('data-in');
        if (key === 'files') { addShots(e.target.files); return; }
        if (key === 'clue') { S.clues[parseInt(e.target.getAttribute('data-i'), 10)].on = e.target.checked; }
        if (key === 'category') { S.categoryId = parseInt(e.target.value, 10) || null; S.check = null; }
        if (CHOICES.indexOf(key) >= 0) { persist(); render(); }
        if (key === 'category' && S.step === 5 && !S.saved) { check(); }
    });

    app.addEventListener('keydown', function (e) {
        if (e.key !== 'Enter') { return; }
        var key = e.target.getAttribute('data-in');
        var send = {ask: 'send-ask', changeAsk: 'change-send', lookupAsk: 'lookup-ask', newClue: 'add-clue'}[key];
        if (send) {
            e.preventDefault();
            var btn = app.querySelector('[data-act="' + send + '"]');
            if (btn) { btn.click(); }
        }
    });

    // Double-tap (or double-click) a sentence to open its sheet. Phones don't
    // reliably send dblclick, so taps are timed here; touch-action on the
    // sentence keeps the browser from zooming instead.
    function segAt(target) {
        var el = target.closest && target.closest('[data-seg]');
        if (!el) { return null; }
        var v = el.getAttribute('data-seg');
        return v === 'leadin' || v === 'answer' ? v : parseInt(v, 10);
    }

    app.addEventListener('touchend', function (e) {
        var seg = segAt(e.target);
        if (seg === null) { return; }
        var now = Date.now();
        if (lastTap.key === seg && now - lastTap.at < 400) {
            e.preventDefault(); lastTap = {key: null, at: 0}; openSheet(seg);
        } else {
            lastTap = {key: seg, at: now};
        }
    });

    app.addEventListener('dblclick', function (e) {
        var seg = segAt(e.target);
        if (seg !== null) { e.preventDefault(); openSheet(seg); }
    });

    document.addEventListener('paste', function (e) {
        if (S.step !== 1 || !e.clipboardData) { return; }
        var files = Array.prototype.filter.call(e.clipboardData.files || [], function (f) { return /^image\//.test(f.type); });
        if (files.length) { e.preventDefault(); addShots(files); }
    });

    restore();
    if (S.step === 4 && !S.q) { S.step = 3; }
    if (S.step === 5 && !S.q) { S.step = 1; }
    render();
}());
