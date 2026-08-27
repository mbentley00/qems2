// Run the Discord formatters under node in both modes of the set option
// "Discord copy leaves a bonus's opening readable". The option is bonus-only:
// an unspoilered tossup sentence is dropped by the playtest bot's clue
// splitter, so tossups stay fully spoilered either way.
//   node qems2/qsub/static/js/checks/discord_first_clue_check.js qems2/qsub/static/js/paste_convert.js
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

const tu = 'First clue here. Second clue (*) with more. Third clue. For 10 points, name it.';
const parts = [{ text: 'Part one.', answer: '_A_', diff: 'e' }, { text: 'Part two.', answer: '_B_', diff: 'm' },
               { text: 'Part three.', answer: '_C_', diff: 'h' }];
let ok = true;
function check(name, cond) { console.log((cond ? 'PASS ' : 'FAIL ') + name); ok = ok && cond; }

global.window.qemsDiscordShowFirst = false;
let t = D.tossup(tu, '_X_', 'me', 'Cat', 1);
check('default: every tossup sentence spoilered', t.startsWith('**||First clue here.|| ||Second clue (*)||** ||with more.||') && t.includes('||Third clue.||'));
let b = D.bonus('Lead in.', parts, 'me', 'Cat', 2);
check('default: bonus leadin and part 1 spoilered, no difficulty in the label', b.startsWith('||Lead in.||\n[10] ||Part one.||\nANSWER: ||'));

global.window.qemsDiscordShowFirst = true;
t = D.tossup(tu, '_X_', 'me', 'Cat', 1);
check('on: tossups stay fully spoilered (the bot drops unspoilered clues)',
      t.startsWith('**||First clue here.|| ||Second clue (*)||** ||with more.||') && t.includes('||Third clue.||'));
b = D.bonus('Lead in.', parts, 'me', 'Cat', 2);
check('on: leadin and part 1 readable, answer 1 and later parts spoilered',
      b.startsWith('Lead in.\n[10] Part one.\nANSWER: ||') && b.includes('[10] ||Part two.||'));
t = D.tossup('No power mark at all. Next one.', '_X_', '', '', 0);
check('on: an unpowered tossup is spoilered too',
      t.startsWith('||No power mark at all.|| ||Next one.||'));
console.log(ok ? 'ALL PASS' : 'SOME FAILED');
process.exit(ok ? 0 : 1);
