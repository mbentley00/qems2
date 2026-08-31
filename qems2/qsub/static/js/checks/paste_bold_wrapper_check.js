// Run paste_convert.js under node with a stub DOM, to check that pasted bold
// which is styling rather than emphasis gets dropped: a bold run ending at a
// power mark (qbreader / QEMS displays bold the pre-power text; QEMS derives
// power from the mark), and bold covering essentially the whole paste (a
// source wrapper that bolds everything). Deliberate bold stays.
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

global.Node = { TEXT_NODE: 3, ELEMENT_NODE: 1 };
global.DOMParser = function () { this.parseFromString = () => ({ body: {} }); };
global.window = {};
global.document = { addEventListener() {} };
const chain = new Proxy(function () {}, {
  get: (t, k) => (k === 'length' ? 0 : k === 'then' ? undefined : chain),
  apply: () => chain,
});
global.$ = function (a) { if (typeof a === 'function') { a(); return; } return chain; };
global.$.fn = chain;

eval(src);
const clean = global.window.QemsMarkup.cleanPastedQems;

let failures = 0;
function check(name, input, want) {
  const got = clean(input);
  if (got !== want) {
    failures++;
    console.log('FAIL', name, '\n  got  ', JSON.stringify(got), '\n  want ', JSON.stringify(want));
  } else {
    console.log('pass', name);
  }
}

// A qbreader tossup: everything up to and including (*) arrives bold.
check('power-mark bold unwrapped',
  '\\BOverabundance of this resource leads to the recycling of D1 proteins. Optimizing access to it shaped most (*)\\B phyllotactic patterns.',
  'Overabundance of this resource leads to the recycling of D1 proteins. Optimizing access to it shaped most (*) phyllotactic patterns.');
check('superpower mark too',
  '\\Bclue one (+)\\B \\Bclue two (*)\\B giveaway.',
  'clue one (+) clue two (*) giveaway.');

// A wrapper that bolds the whole paste.
check('whole-paste bold dropped',
  '\\BAn entire paragraph copied from a website that wraps everything in strong for styling reasons.\\B',
  'An entire paragraph copied from a website that wraps everything in strong for styling reasons.');
check('nearly-whole-paste bold dropped',
  '\\BAlmost all of this long pasted sentence arrives inside one bold wrapper element,\\B except this.'.replace('except this.', 'zz.'),
  'Almost all of this long pasted sentence arrives inside one bold wrapper element, zz.');

// Deliberate bold survives.
check('short bold paste kept', '\\BBayreuth\\B', '\\BBayreuth\\B');
check('partial bold kept',
  'This composer wrote the \\BRing\\B cycle, which premiered at a purpose-built festival theatre in Bavaria.',
  'This composer wrote the \\BRing\\B cycle, which premiered at a purpose-built festival theatre in Bavaria.');
check('bold+underline answers untouched',
  'ANSWER: _Richard Wagner_ [accept ~Der Ring des Nibelungen~]',
  'ANSWER: _Richard Wagner_ [accept ~Der Ring des Nibelungen~]');

console.log(failures ? 'FAIL' : 'PASS');
process.exit(failures ? 1 : 0);
