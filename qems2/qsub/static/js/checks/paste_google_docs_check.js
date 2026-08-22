// Run paste_convert.js under node with a hand-built DOM, to check the
// Google Docs wrapper case without a browser.
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

global.Node = { TEXT_NODE: 3, ELEMENT_NODE: 1 };
function text(s) { return { nodeType: 3, textContent: s }; }
function el(tag, attrs, children) {
  return {
    nodeType: 1, tagName: tag.toUpperCase(), childNodes: children,
    getAttribute: k => (attrs && attrs[k]) || null,
  };
}
// Google Docs clipboard shape: a <b style="font-weight:normal"> wrapper,
// real bold as <span style="font-weight:700">, underline via text-decoration.
const body = el('body', {}, [
  el('b', { style: 'font-weight:normal;', id: 'docs-internal-guid-abc' }, [
    el('p', {}, [
      el('span', { style: 'font-size:11pt;font-weight:400' }, [text('[10m] Name this town. ')]),
    ]),
    el('p', {}, [
      el('span', { style: 'font-weight:400' }, [text('ANSWER: ')]),
      el('span', { style: 'font-weight:700;text-decoration:underline' }, [text('Bayreuth')]),
    ]),
    el('p', {}, [
      el('span', { style: 'font-weight:700' }, [text('really bold')]),
      el('b', {}, [text(' plain b')]),
    ]),
  ]),
]);
body.querySelectorAll = () => [];
global.DOMParser = function () { this.parseFromString = () => ({ body }); };
global.window = {};
global.document = { addEventListener() {} };
const chain = new Proxy(function () {}, {
  get: (t, k) => (k === 'length' ? 0 : k === 'then' ? undefined : chain),
  apply: () => chain,
});
global.$ = function (a) { if (typeof a === 'function') { a(); return; } return chain; };
global.$.fn = chain;

eval(src);
const out = global.window.QemsMarkup.htmlToQems('');
console.log(JSON.stringify(out));
const ok = !out.includes('\\B[10m]') && out.includes('_Bayreuth_') &&
           out.includes('\\Breally bold\\B') && out.includes(' \\Bplain b\\B');
console.log(ok ? 'PASS' : 'FAIL');
process.exit(ok ? 0 : 1);
