// Every spoilered answer line goes out at the same minimum width, so the shape
// of the blurred block does not give away that the answer is two words long.
//   node qems2/qsub/static/js/checks/discord_answer_padding_check.js qems2/qsub/static/js/paste_convert.js
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

global.window = {};
global.document = { addEventListener() {} };
global.Node = { TEXT_NODE: 3, ELEMENT_NODE: 1 };
const chain = new Proxy(function () {}, {
  get: (t, k) => (k === 'length' ? 0 : k === 'then' ? undefined : chain),
  apply: () => chain,
});
global.$ = function (a) { if (typeof a === 'function') { a(); return; } return chain; };
global.$.fn = chain;
eval(src);
const D = global.window.QemsDiscord;

const MIN = 50;
const PAD_END = '⠀';
let ok = true;
function check(name, cond) { console.log((cond ? 'PASS ' : 'FAIL ') + name); ok = ok && cond; }

// The spoilered runs on the ANSWER: lines of a formatted question.
function answerBlocks(text) {
  return text.split('\n').filter(l => l.startsWith('ANSWER: '))
             .map(l => /^ANSWER: \|\|([\s\S]*?)\|\|/.exec(l))
             .map(m => (m ? m[1] : null));
}
// What a reader sees: Discord's markup is instructions, not characters.
function width(s) { return s.replace(/\*\*|__|[*_]/g, '').length; }

const parts = [{ text: 'Part one.', answer: 'A', diff: 'e' },
               { text: 'Part two.', answer: '_B_', diff: 'm' },
               { text: 'Part three.', answer: 'C', diff: 'h' }];
const stem = 'First clue here. Second clue (*) with more. For 10 points, name it.';
const long = 'a very long answer that runs well past the fifty character minimum on its own';

global.window.qemsDiscordShowFirst = false;

let a = answerBlocks(D.tossup(stem, '_X_', 'me', 'Cat', 1));
check('a one-letter tossup answer is padded to the minimum width',
      a.length === 1 && width(a[0]) === MIN && a[0].startsWith('__**X**__'));
check('the padding sits inside the spoiler, ending in a braille blank',
      a[0].endsWith(PAD_END) && /^__\*\*X\*\*__ +⠀$/.test(a[0]));

a = answerBlocks(D.tossup(stem, long, '', '', 0));
check('an answer already past the minimum is left alone',
      a[0] === long && a[0].indexOf(PAD_END) === -1);

// Markup is not width: a padder counting raw characters would leave
// __**X**__ (9 characters, one letter) looking like a nine-letter answer.
check('markup does not count toward the width',
      width('__**X**__') === 1 && answerBlocks(D.tossup(stem, 'X', '', '', 0))[0].length <
      answerBlocks(D.tossup(stem, '_X_', '', '', 0))[0].length);

a = answerBlocks(D.bonus('Lead in.', parts, 'me', 'Cat', 2));
check('all three bonus answers are padded to the same width',
      a.length === 3 && a.every(x => width(x) === MIN));

// The qid tag rides outside the spoiler and must stay there.
check('the qid tag is still outside the padded block',
      D.tossup(stem, 'X', '', '', 7).includes(PAD_END + '|| <qid:7>'));

// Nothing is hidden in a no-spoiler copy, so there is nothing to pad.
check('the no-spoiler copies are unpadded',
      D.tossupPlain(stem, 'X', '', '', 0).indexOf(PAD_END) === -1 &&
      D.bonusPlain('Lead in.', parts, '', '', 0).indexOf(PAD_END) === -1);

// The readable-opening setting is about clues; an answer is never readable.
global.window.qemsDiscordShowFirst = true;
a = answerBlocks(D.tossup(stem, 'X', '', '', 0));
check('padding holds with the readable opening on', width(a[0]) === MIN);
global.window.qemsDiscordShowFirst = false;

console.log(ok ? 'ALL PASS' : 'SOME FAILED');
process.exit(ok ? 0 : 1);
