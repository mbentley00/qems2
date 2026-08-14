# A richer answer line — proposal

**Status: partly built.** The recommendation below — type prose, show fields,
store both — now exists as the opt-in **Structured answer lines** setting
(`QuestionSet.structured_answers`, off by default; see
`qsub/answer_structure.py`). What shipped is the editor view (#4) over a stored
structure, with the prose line still canonical and best-effort parsing for
answers that arrive as prose.

What has *not* been built: the typed grammar as a lint (#2 below), the
`answerSpec` projection into YAPP2, and the corpus measurement in the open
questions. Those are still proposals.

## The problem

An answer line is the densest thing in a packet. It has to say all of this:

| | example |
|---|---|
| the answer, and which part of it is required | _**Louis XIV**_ |
| other acceptable answers | or the **Sun King** |
| what to prompt on | prompt on **Louis** |
| *what to say* when prompting | by asking "which Louis?" |
| what to antiprompt on | antiprompt on **the Bourbons** |
| what to reject outright | do not accept "Louis XVI" |
| answers that stop being acceptable partway through | accept **the King** until "Sun King" is read |
| how to pronounce it | ("loo-EE") |
| a note to the moderator | \Ndon't accept a regnal number alone\N |

Today all of that is prose in one string, and the structure is implied by word
order. QEMS marks *words* (`_x_` is accepted, `__x__` is a prompt target) but
not *clauses*, so anything that needs to know which alternative belongs to which
directive has to guess from the prose. `style_checker._last_directive` is that
guess: it scans backwards from a markup run for the nearest "prompt"/"accept"/
"reject" and hopes. It is right most of the time, and quietly wrong when a line
nests ("prompt on X, but accept Y or Z"), when a directive is implied rather
than written, or when a second clause borrows the first's verb.

Three things want the structure, not the prose:

- **the style checker** — every answer-line rule currently re-derives it;
- **MODAQ and other readers** — which today re-parse the printed line at game time;
- **the answer database** — which splits on `;`/`,`/`or` and cannot tell an
  acceptable alternate from a rejected one.

## What not to do

The temptation is to design a clean format and convert. That fails on two
counts. Writers type an answer line the way a moderator reads it, and any format
whose printed form has to be *generated* breaks that — a writer who can't see
the output while typing it will get the output wrong. And a packet's answer
lines arrive by paste, from packets written in the prose form, forever.

So the constraint is: **the thing the writer types stays the thing the moderator
reads.** Structure has to be added to that, not substituted for it.

## Four candidates

### 1. Prose, as today (the baseline)

```
_Louis XIV_ [or __the Sun King__; prompt on __Louis__]
```

Nothing to learn, nothing parseable. This is the status quo and the thing to beat.

### 2. Keyword-led clauses — formalize what good writers already type

Everything after the core answer lives in one bracket, clauses are separated by
`;`, and **every clause begins with a directive word**.

```
_Louis XIV_ [accept _the Sun King_; prompt on __Louis__ by asking "which Louis?";
             accept _the King_ until "Sun King" is read; reject Louis XVI]
```

The only change from a well-written line today is that the leading keyword
becomes *required* rather than customary — no new punctuation, no new
characters, and the printed line is unchanged. A clause the grammar can't read
is then a lint, not a silent misparse, so the format teaches itself: the
checker tells a writer the one clause it couldn't follow.

**Pro:** costs the writer ~nothing; parses with a small grammar; degrades
gracefully (an unparsed clause still prints correctly).
**Con:** still prose inside a clause — the target list has to be split on
"or"/"," — and the window ("until … is read") needs a fixed phrasing to be
recognized.

### 3. Sigils per alternative — terse

```
_Louis XIV_ [+the Sun King; ?Louis="which Louis?"; -Louis XVI; ?<"Sun King">the King]
```

**Pro:** short and unambiguous; trivial to parse.
**Con:** it is no longer what a moderator reads, so the printed form must be
generated — which is the failure mode above. QEMS markup already spends `_`,
`~`, `\`, and parentheses; adding `+ ? - < =` deepens the escaping problem for
answers that legitimately contain them. Highest risk, and the risk buys only
brevity.

### 4. One clause per line — block form

```
ANSWER: Louis XIV
accept: the Sun King
prompt: Louis — "which Louis?"
until "the Sun King": the King
reject: Louis XVI
```

**Pro:** unambiguous, diffs beautifully, and is the obvious shape for an
*editing UI*.
**Con:** a different input widget, hostile to pasting, and the single-line
printed form has to be assembled.

## Recommendation

**Type #2. Show #4. Store both.** (#4 and the storage are built; #2 is not.)

1. **#2 is the typed format.** Tighten the existing convention into a grammar,
   and enforce it with a style rule rather than a parser error. Writers keep
   typing what they type now.

2. **#4 is an editor view.** The same line, exploded into one row per clause,
   for people who would rather fill in fields than punctuate a sentence. It
   round-trips to #2 on save. This is where a set can *require* a directed
   prompt: the "what do you say" box is simply there, empty and waiting.

3. **Store the parse beside the prose, never instead of it.** The canonical
   `answer` string stays byte-identical; a parallel structured field carries the
   clauses. This is precisely the trick [YAPP2_FORMAT.md](YAPP2_FORMAT.md) uses
   for pronunciation anchoring — canonical fields untouched, richness alongside,
   older readers unaffected — and it would extend naturally as `yapp2/1.2`:

   ```json
   "answer": "Louis XIV [prompt on Louis by asking \"which Louis?\"]",
   "answerSpec": {
     "core": "Louis XIV",
     "clauses": [
       {"directive": "prompt", "targets": ["Louis"], "say": "which Louis?"},
       {"directive": "accept", "targets": ["the King"], "until": "Sun King"}
     ]
   }
   ```

   A reader that understands `answerSpec` can judge a buzz mechanically. One
   that doesn't reads the same line it reads today.

## A sketch of the grammar

```
line      := core [ "[" clause { ";" clause } "]" ] [ note ]
clause    := directive targets [ direction ] [ window ]
directive := "or" | "accept" | "prompt on" | "antiprompt on" | "reject"
           | "do not accept"
targets   := run { ("or" | ",") run }
direction := "by" verb-phrase | "—" quoted        # what the moderator says
window    := "until" quoted "is read"             # stops being acceptable
           | "after" quoted "is read"             # starts being acceptable
run       := existing QEMS markup: _accepted_, __prompt target__, ~italic~
```

Two properties worth keeping:

- **The word markup and the clause keyword say the same thing twice.** `_x_`
  inside a `prompt` clause is a contradiction — and catching exactly that is
  what the existing `answer_format` rule already does. A grammar makes that
  check total rather than heuristic.
- **`or` at the start of a clause means "accept".** It is what everyone writes,
  and dropping it from the grammar would make the format's most common clause
  its most awkward one.

## Open questions

- Is `;` enough of a separator, given answers that contain one? (Probably —
  it is already the de facto clause separator — but it needs an escape.)
- Should a window be expressible against the *power mark* ("accept until (*)")
  rather than a quoted phrase? Readers know where the power mark is; they don't
  know where an arbitrary phrase is until they get there.
- Does `reject` need a spoken direction too ("do not accept X — say 'no'")?
- How much of the existing corpus parses cleanly under #2 as written? That is a
  measurement, and it should be taken before any of this is built: the answer
  decides whether the grammar is a lint or a migration.
