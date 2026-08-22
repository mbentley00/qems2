// Exercise splitIntoSentences from paste_convert.js under node.
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8').split('\r\n').join('\n');
const start = src.indexOf('    function splitIntoSentences(');
const end = src.indexOf('\n    }\n', start) + 7;
if (start < 0 || end < start) { throw new Error('function not found'); }
eval(src.substring(start, end));

const t = 'Kinoshita and Terasaka name one of these objects named for John H. Conway, which Lisa showed is not "slice" in 2020. These objects can be joined into links. The simplest one has a crossing number of three and is called "trefoil." For 10 points, name these objects.';
const out = splitIntoSentences(t);
console.log(JSON.stringify(out, null, 1));
let ok = out.length === 4 && out[0].includes('John H. Conway') &&
         out[2].endsWith('"trefoil."') && out[3] === 'For 10 points, name these objects.';
const a = splitIntoSentences('Bold end.** Next one.');
const b = splitIntoSentences('No punctuation at all');
const c = splitIntoSentences('Ends mid (*)');
const d = splitIntoSentences('He said “done.” Then left!   Really?');
console.log(JSON.stringify([a, b, c, d]));
ok = ok && a.length === 2 && a[0] === 'Bold end.**' && b.length === 1 && c.length === 1 &&
     d.length === 3 && d[0] === 'He said “done.”';
console.log(ok ? 'PASS' : 'FAIL');
process.exit(ok ? 0 : 1);
