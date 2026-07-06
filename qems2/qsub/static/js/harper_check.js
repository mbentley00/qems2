// Client-side grammar checking via Harper (writewithharper.com) — the whole
// engine runs in the browser as WebAssembly, so question text never leaves
// the page and there is no per-check cost. The library is loaded lazily from
// the CDN the first time a check runs; pages pay nothing until then.
(function () {
    'use strict';

    var HARPER_VERSION = '2.4.0';
    var CDN = 'https://unpkg.com/harper.js@' + HARPER_VERSION + '/dist/';
    var linterPromise = null;

    function ensureLinter() {
        if (!linterPromise) {
            linterPromise = Promise.all([
                import(CDN + 'index.js'),
                import(CDN + 'binaryInlined.js')
            ]).then(function (mods) {
                var linter = new mods[0].WorkerLinter({ binary: mods[1].binaryInlined });
                var ready = linter.setup ? linter.setup() : Promise.resolve();
                return Promise.resolve(ready).then(function () { return linter; });
            });
            // A failed CDN load shouldn't poison every later attempt.
            linterPromise.catch(function () { linterPromise = null; });
        }
        return linterPromise;
    }

    // QEMS markup -> plain prose so markup tokens don't trigger false
    // positives. Collapses the gaps stripping leaves behind (the server-side
    // style checker owns real whitespace complaints).
    function stripQems(text) {
        var t = String(text || '');
        t = t.replace(/\(\*\)/g, ' ');      // power mark
        t = t.replace(/\\[BSsP]/g, '');     // \B bold, \S sup, \s sub, \P pg toggles
        t = t.replace(/[~_]/g, '');         // italic / underline markers
        t = t.replace(/[ \t]{2,}/g, ' ');
        t = t.replace(/ ([,.;:!?])/g, '$1');  // "word (*)," -> "word ,": rejoin
        return t.trim();
    }

    function lintKind(l) {
        try {
            if (l.lint_kind_pretty) { return String(l.lint_kind_pretty()); }
            if (l.lint_kind) { return String(l.lint_kind()); }
        } catch (e) {}
        return '';
    }

    // fields: [{name, text}] of QEMS-markup prose. Resolves to a list of
    // findings: {field, kind, message, problem, replacements}. Spelling lints
    // are dropped unless includeSpelling — quizbowl text is dense with proper
    // nouns the dictionary can't know.
    function lintFields(fields, includeSpelling) {
        return ensureLinter().then(function (linter) {
            var chain = Promise.resolve([]);
            fields.forEach(function (f) {
                chain = chain.then(function (acc) {
                    var text = stripQems(f.text);
                    if (!text) { return acc; }
                    return linter.lint(text).then(function (lints) {
                        (lints || []).forEach(function (l) {
                            var kind = lintKind(l);
                            if (!includeSpelling && /spell/i.test(kind)) { return; }
                            var problem = '';
                            try {
                                if (l.get_problem_text) { problem = l.get_problem_text(); }
                                else { var sp = l.span(); problem = text.slice(sp.start, sp.end); }
                            } catch (e) {}
                            var reps = [];
                            try {
                                (l.suggestions() || []).forEach(function (s) {
                                    var r = s.get_replacement_text ? s.get_replacement_text() : '';
                                    if (r && reps.indexOf(r) === -1) { reps.push(r); }
                                });
                            } catch (e) {}
                            acc.push({ field: f.name, kind: kind, message: String(l.message()),
                                       problem: String(problem), replacements: reps.slice(0, 3) });
                        });
                        return acc;
                    });
                });
            });
            return chain;
        });
    }

    window.QemsHarper = { ensure: ensureLinter, strip: stripQems, lintFields: lintFields };
})();
