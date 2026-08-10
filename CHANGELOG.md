# Changelog

## 2026-08-10b — Plain parentheses, per-packet PDFs, a grid that stays put

- **A set can declare that only quoted parentheses are pronunciation guides.**
  QEMS has always read *every* parenthetical as a guide: grey on the page, left
  out of the character count, silent when the question is read aloud, and
  nagged at by the `\P` rules. A set that also writes ordinary asides —
  "(a portrait of the artist's wife)" — had no way to say so. With **Only quoted
  parentheses are pronunciation guides** ticked (set options, off by default),
  `("DID-er-OW")` is still a guide and everything else is ordinary text: it
  renders plain, counts toward the length, is read aloud in the audio, prints
  normally in Word and PDF, and no longer draws "guide has no marked target".
  Power marks `(*)`/`(+)` are untouched, and only double quotes count — an
  apostrophe is prose, not a respelling. The setting is applied at render time,
  so ticking or unticking it takes effect immediately on questions already
  written.
- **An anchored pronunciation guide turns grey in the editor.** The marked
  word was amber and the guide beside it looked like ordinary clue text, so
  there was no way to see at a glance that `\P…\P` and its guide were actually
  paired. Now the guide greys as soon as the pair is complete — amber word,
  grey guide — and un-greys if you remove the mark.
- **The packet grid always has one spare row.** With every packet at 20
  tossups there was no way to give one packet a 21st: the grid only drew rows
  that already existed. There is now a dim empty row under the last one; place a
  question in it and a new spare appears beneath, the way a spreadsheet always
  has one more line. Read-only viewers don't get it.
- **Placing a question on the grid no longer reloads the page.** Working on
  packet 20 meant being thrown back to packet 1 on every placement. Moves,
  swaps, placements and unassigns now repaint just the cells that changed
  (reusing the live-refresh machinery) and update the Unpacketized panel in
  place. The operations that really do need a reload — adding a packet,
  reordering packets, undo — now keep the horizontal scroll properly: it was
  being saved and restored already, but the restore ran *before* the
  scrollbar wrapper re-parented each grid, and re-parenting a scrollable
  element resets it to 0.
- **The PDF export is one PDF per packet, zipped** — the same shape as the Word
  export. A packet is what gets handed to a room, so it's its own file; the
  credits page, when enabled, goes in every packet rather than only the first.
- **New style rule: the answer cue switches number.** "these animals" early and
  "this animal" later is flagged, with the two phrases shown in context. Only
  the same noun counts, so "this novel" alongside "these poems" is left alone,
  and bonus parts are checked separately since each part has its own answer.
- **Your activity no longer backfills when you edit someone else's question.**
  Becoming a question's editor used to dump every earlier revision of it into
  your feed. Each question is now reported from your own last change to it
  onward: for one you wrote, everything after you wrote it; for one you edited,
  everything after you took it on. The badge count uses the same rule.

## 2026-08-10 — Export one question to YAPP/YAPP2

- **The edit tossup and edit bonus pages have YAPP and YAPP2 buttons.** They sit
  with Move Set / History / Delete and download just that question. MODAQ and
  the other YAPP readers open a packet rather than a question, so the file is a
  one-question packet — the question in one array, the other array empty, which
  every YAPP reader accepts. The JSON is byte-identical to what the set-wide
  export writes for that question: same power bolding, same all-power handling,
  same `anchored` pronunciation-guide fields on YAPP2, and the question keeps
  its packet number rather than being renumbered to 1. Useful for hearing how a
  single question reads without exporting the whole set, or for handing one
  question to someone. Anyone who can open the question's edit page can export
  it; the file holds nothing the page doesn't already show.

## 2026-08-07b — Power reads as power in MODAQ; pronunciation-guide fixes

- **Exported YAPP/YAPP2 packets show their power region in bold again.** MODAQ
  renders the bold formatting a packet carries and uses `(*)` only to score the
  buzz — it never infers bold from the marker. A YAPP file made from a Word
  packet has the region as bold runs; ours had only the marker, so every
  exported tossup read as unpowered. The export now bolds through the last
  power mark (the whole stem for an all-power tossup), closing and reopening any
  italics that straddle the boundary so the tags stay nested. Importing such a
  file back is unchanged — the importer already drops bold in question text.
- **Packet grid: the header row is back above tossup 1.** `.grid-scroll` sets
  `overflow-x: auto`, which makes it a scroll container on *both* axes, so the
  header's `top: 60px` (meant to clear the app bar) was measured from the top of
  the grid, not the viewport — shoving the header 60px down, below row 1 and
  over its category line. The offset is 0 now. The names still track the columns
  when you scroll sideways; they no longer follow vertical page scroll, which
  they never actually did.
- **The character count shows red on load.** Opening an over-length question
  showed a plain black count until you typed a character; the colour is now
  applied to the server-rendered count too.
- **Style suggestions show the text they found.** "Space before punctuation",
  "use ellipsis (…)", repeated words, ampersands, number ranges, contractions,
  "ANSWER:" leaks, "For ten points" and the pronunciation-guide rules now each
  carry the same context preview the PG suggestions had, with the offending
  words bolded — so you can see what a rule is pointing at without hunting for
  it. Spans too small to see (a double space, the space before a comma) widen to
  whole words.
- **A pronunciation guide no longer splits a possessive.** Auto-applying a guide
  to "Edward Saatchi's" wrote `\PEdward Saatchi\P ("SAH-chee")'s`, breaking a
  word that's read as one. It now marks `\PEdward Saatchi's\P ("SAH-cheez")`,
  growing the respelling by the sound the possessive actually adds (/z/, /s/, or
  a whole syllable after a sibilant; a plural possessive like "Jones'" adds
  none). A new style rule flags guides already written that way, with a fix that
  moves the possessive and respells the guide.
- **No more false "no marked target" warning.** A target closed inside markup —
  `~Death of the \PDauphin\P~ ("DOFF-in")` — wasn't recognized, because the
  check only looked for a `\P` immediately before the guide. Closing italics,
  underlines and `\B`/`\S`/`\s` are now stepped over, on the server and in the
  editor's "PG auto" (which would otherwise have marked the word twice).
- **Packet prev/next moved to the top of the edit pages,** under the breadcrumb,
  instead of down in the packet meta row.

## 2026-08-07 — Pronunciation guides stay guides inside power

- **A guide in the power region is gray and unbolded, like every other guide.**
  A `("KAM-uh-flahzh")` before the `(*)` was rendered as plain bold clue text,
  so in doc view and on the question pages it competed with the words a
  moderator actually reads. It now keeps the gray, non-bold guide styling
  wherever it falls — matching what the Word export has always done — while the
  `(*)` / `(+)` marks themselves stay bold. This also means an in-power guide is
  picked up by doc view's "PGs Above" toggle, which had skipped them.
- **Guides are gray on the edit and add pages too.** The gray styling only
  existed in doc view and the category document; everywhere else guides rendered
  as bold black text. It's one rule in the app stylesheet now, so a guide looks
  the same in every formatted view (dark theme included).

## 2026-08-06 — Packet grid scrolling, doc-view Swap next to Edit

- **A horizontal scrollbar you can see and reach.** The grid's own scrollbar sat
  at the bottom of a table taller than the screen, and Firefox draws overlay
  scrollbars that fade out — so there was often nothing to grab anywhere. Each
  grid now has a bar we draw ourselves, stuck to the bottom of the viewport and
  always visible, that drives the grid when you drag or click it.
- **Packet names stay put.** The grid's header row is sticky, so ten rows in you
  can still tell which column is which packet.
- **Find-in-page works in Firefox.** The earlier fix hung entirely off the
  `selectionchange` event, which Firefox doesn't fire reliably for the find bar,
  so on Firefox it never ran at all — a match in an off-screen column was
  selected where you couldn't see it. The selection is now polled as well as
  watched, the container is scrolled explicitly (clearing the sticky number
  column and top bar), and a stuck drag-guard that could silently disable the
  whole thing is cleared on window blur.
- **Doc view: Swap moved next to Edit.** They're the two actions you take on a
  question, so they sit together in the meta row instead of at opposite ends.

## 2026-08-05 — Packet navigation, resolved comments out of the lists

- **Step through a packet from the question page.** Previous/next links beside
  the packet name walk the packet in reading order — every tossup by number,
  then every bonus — so you can work through a packet without opening a tab per
  question. Each link names where it goes, the ends read "Start/End of packet",
  and `[` / `]` work as shortcuts. An unpacketized question shows none, having
  no sequence to walk.
- **Resolved comments drop out of the comment lists.** Resolving is how an
  editor says a comment has been dealt with, so it no longer sits in the
  all-comments page or the dashboard's Recent Comments tab; replies go with the
  comment they answer, since the thread is what gets resolved. They're still on
  the question itself, marked resolved, and in its comment history.

## 2026-08-04b — Whole-number packet requirements, all-power in YAPP

- **Per-packet requirements are whole questions.** The Edit Packet status table
  divided the set total by the packet count, so 18 tossups over 11 packets read
  as "1.6 required" — a number nobody can write to. Each packet now gets an
  integer share (the first few packets carry the remainder), and the shares add
  up to the set total exactly. A category asks for what its subcategories add up
  to, so the rows can't disagree.
- **All-power tossups keep their power in YAPP.** QEMS records a whole-stem
  power as a flag with no `(*)` in the text — the marker's absence is part of
  how it's detected — but YAPP readers locate the power boundary from that
  literal marker, so MODAQ was scoring every buzz on such a tossup as a plain
  10. The export now writes the marker after the last word, which says the same
  thing in YAPP's terms.
- **MODAQ reads YAPP2 1.1.** The fork validates `readingOrder` on load (keeping
  it only when it names every question exactly once) and writes it back out, so
  a load/export round trip no longer drops it.

## 2026-08-04 — Live packet grid, comment history, export typography

- **The packet grid keeps up with other people's changes.** It polls for what
  currently occupies each slot and repaints only the cells someone else changed,
  with a brief highlight. Two editors rearranging the same packets used to
  clobber each other, each dragging on a grid that had gone stale. It never
  repaints mid-drag, while the swap dialog is open, or in a background tab; a
  structural change (a new packet, more rows) reloads instead, since a new
  column can't be patched cell-by-cell.
- **Comments are kept in a question's history even after deletion.** Deleting a
  comment only ever hid it, so nothing was lost — but there was nowhere to see
  it. The Tossup/Bonus History pages now list every comment ever left on the
  question, threaded, with deleted ones dimmed and marked. The editing pages
  still hide them. Deleting a comment no longer asks for confirmation (it's
  recoverable); "Delete all comments" still does.
- **Interlaced exports.** A new "Interlace tossups and bonuses" option on the
  export form prints tossup 1, bonus 1, tossup 2, bonus 2 — the order they're
  read — instead of all tossups then all bonuses. Word and PDF reorder directly;
  **YAPP2 gains a `readingOrder` field** (version `yapp2/1.1`) that says the same
  thing without moving a question, so a reader that ignores it still gets the
  whole packet. Plain YAPP has no way to express it and is unchanged.
- **Word export typography.** `ANSWER:` is no longer bolded, pronunciation
  guides are set in gray Source Sans Pro and never bolded (even inside a bolded
  power region, where they used to inherit it), and packets are single-spaced.
- **Editor tags show in comments.** If a commenter has a tag for the set
  ("Science", "Head editor"), it appears as a chip beside their name in every
  comment thread.
- **Smaller fixes.** The packet grid drops parentheses and pronunciation guides
  from its answer previews, where they crowded out the answer; the Edit Packet
  and Packet Grid pages name the packet and set in their titles; and the "Select
  all" checkbox on Style Check and Category Problems lines up with its label.

## 2026-07-31 — Distribution visibility, reference data admin, writing-flow fixes

- **The "since your last visit" banner now opens the right page.** It counted
  set-wide activity but linked to **My Activity**, which only ever lists your
  own @mentions and changes to questions you wrote — so a correct banner could
  land you on an empty page. It now opens **Recent Changes** windowed to that
  visit (`/recap/<set>/?since=…`), which leaves out your own questions, edits
  and comments exactly as the banner does, so the listing matches the count.
- **Distributions can be public or private** (migration 0038). A new
  *Publicly viewable* checkbox on the distribution page: a public distribution
  is offered to everyone creating a set and can be previewed and copied by
  anyone; a private one is visible only to people on a set that uses it, and to
  whoever made it. Editing is always restricted to the latter group. New
  distributions start private; the ones that already existed stay public, since
  every distribution used to be offered to everyone. Public distributions from
  other people are listed on the Distributions page and can be copied — a copy
  starts private.
- **Bonus part difficulties in one tag.** Ending a bonus's last answer line with
  `(emh)` marks part 1 easy, part 2 medium and part 3 hard, instead of writing
  `[10e]`/`[10m]`/`[10h]` per part. The tag is stripped from the answer, works
  alongside a `{Category}` tag, and a per-part marker still wins where you've
  written one. An ordinary parenthetical (`(1767-1845)`) is left alone.
- **Reference data admin** (migration 0039). A new admin-only **Reference Data**
  page manages the two bundled datasets the style checker reads — the verified
  pronunciation dictionary and the standard answer lines. Search an entry, fix
  it, add one that's missing, or withdraw one that shouldn't fire. Those files
  are generated offline, so corrections are stored separately and layered on
  top; they survive a rebuild of the bundled data and can be reverted per entry.
- **Category tag picker on Type Questions is compact.** Each category is now one
  row — heading on the left, its subcategories flowing beside it — instead of
  three, and the tags read as labels rather than buttons that look like they
  submit the form.

## 2026-07-29 — Set join links, YAPP2 packet format

- **Join links for question sets** (migration 0037). A set owner can create a
  shareable link on the **Writers and Editors** tab; anyone logged in who opens
  it can *request* access. The link never grants a role on its own — every
  request still waits for an owner's approval, now shown as a pending-requests
  queue on the same tab (previously a join request existed only as an email, so
  it was lost if the owner had no address on file). Owners can regenerate the
  link (killing the old URL), disable, or delete it. Link management and
  approval are owner-only; editors can still add members directly.
- **YAPP2 export** — a backward-compatible superset of YAPP JSON that carries
  pronunciation-guide *anchoring*: which words each guide covers. QEMS tracks
  this with `\P` markers, and a plain-YAPP export had to discard it. YAPP2 keeps
  the canonical YAPP fields byte-identical (so existing readers are unaffected)
  and puts `<pg>`-tagged copies in a parallel `anchored` object. Available as
  "Export Packetized YAPP2 JSON"; the importer reads it back, so a
  QEMS → YAPP2 → QEMS round trip is lossless. Spec in `YAPP2_FORMAT.md`;
  reader support added to the MODAQ fork.

## 2026-07-23 — Editor tags, role-group UX, comment/mention polish

- **Editor category & freeform tags** (migration 0034). Tag an editor with the
  categories/subcategories they cover, or a freeform note, on the renamed
  **Writers and Editors** tab; category tags also show per row on the Category
  Overview. Owner/editor-managed.
- **Role groups are easier to manage.** Add members by searching name, username,
  or email (live picker) instead of typing an exact username. The page now shows
  only groups you created, belong to, or requested — not every group. A group's
  creator is now automatically a member (migration 0035 backfills existing ones,
  fixing "0 members").
- **Comment strike-through updates in place** — crossing out or un-crossing part
  of a comment no longer reloads the page.
- **@mentions of email-address usernames** highlight in full (no longer stop at
  the second `@`).
- **Comment lists show real names** (with username) instead of the bare username.
- **AI features gate on `is_superuser`** rather than the literal `admin`
  username, so the admin account can be safely renamed.

## 2026-07-19 — Power marks, editor tools, comment strike-through

- **All-power tossups.** A tossup whose whole stem is in power now renders fully
  bold, and every correct buzz scores 15. Auto-detected when the stem says "for
  15 points" and carries no `(*)`/`(+)` mark; an "All Power" checkbox on the edit
  page overrides it either way (`Tossup.all_power`, migration 0033).
- **Set-level superpowers.** A per-set "Enable superpowers" option turns on the
  20-point superpower mark `(+)` (before the 15-point `(*)`). Off by default: a
  stray `(+)` then renders as plain text and never scores 20. Gated through
  rendering, play, and scoring (`QuestionSet.enable_superpower`).
- **Raw-markup editor toggle.** Every rich editor has a **Raw** button that swaps
  to the underlying QEMS markup (`~foo~` italics, `_foo_` answer underline, etc.)
  for hand-fixing anything the rich view got wrong; flips to **Rich** to switch
  back. The old always-on "Rich text…" hint that overlapped the text is gone.
- **Editors can cross out parts of comments.** Select text in a comment on the
  edit pages or the document-view gutter and click **Cross out** to strike it
  through (marking it handled); click struck text to un-cross. Stored as a new
  `\D...\D` QEMS strike token; editor/owner only (`/strike_comment/`).
- **Document view: drag-to-swap fix.** Dropping a question onto another now swaps
  with the question *under the pointer*, not whichever one the gap landed beside.
- **Comment author display.** Recent-questions / set-wide comment lists show the
  commenter's real name (with username) instead of the bare username.
- Cache busters: `base.css?v=52`, `rich_editor.js?v=15`, `comment_strike.js?v=1`.

## 2026-07-08 — Pronunciation-guide spans in the rich editor + style rule

- **Rich editor now understands `\P...\P` spans.** The contenteditable editors
  on edit-tossup/bonus, add-tossup/bonus, and Type Questions render
  pronunciation-guide targets as the teal `.pg-target` words (instead of showing
  raw `\P` markers), and round-trip them back to markup on save
  (`paste_convert.js` `walkNode`). This makes the style checker's PG auto-fix
  (which already inserts `\P...\P`) show correctly after it reloads the page.
- **New toolbar "PG" button.** Select a term together with its following
  `("...")` guide and click **PG** — it wraps just the word(s) in `\P...\P`,
  leaving the parenthetical outside the span. With no trailing guide it wraps the
  whole selection. Re-marking peels any existing span so it never nests.
- **New optional style rule `pg_span`** (Minkowski, not generic): flags a
  pronunciation guide `("...")` whose spoken word(s) aren't wrapped in `\P...\P`,
  pointing the editor at the PG button. Not auto-fixable (which word(s) a
  hand-written guide covers is ambiguous); power marks `(*)`/`(+)` and escaped
  `\(...\)` are ignored. Toggleable per set like the other rules.
- Cache busters: `base.css?v=48`, `paste_convert.js?v=20`, `rich_editor.js?v=11`.

## 2026-07-04 — PG annotation, smart-quote export, quote-punct rule, email links

- **Pronunciation-guide targets (`\P...\P`)**: new QEMS markup tying a guide to
  exactly the word(s) it covers — `Denis \PDiderot\P ("DID-er-OW")`; can span
  multiple words. Rendered as a subtle teal (`.pg-target`) in all formatted
  views and in the Word export (teal run color); the document view gained a
  **"PGs Above"** toggle (persisted per browser) that moves annotated guides
  into ruby text directly above their words. Markers are pure annotation: they
  never count toward length, are stripped from Discord copy / MP3 audio / YAPP
  export / play mode / search & AI text, and the rich-text Copy renders the
  colored words. The style checker's pronunciation-guide auto-fix now inserts
  the annotated form (falling back to unannotated when the term sits inside
  other markup, where wrapping would misnest). Odd `\P` counts are caught by
  the balance validator; documented in the formatting guide.
- **Word export: "Smart quotes" option** on the packetized-export form
  (default off): converts straight quotes/apostrophes to typographic ones with
  opening/closing chosen by context (markup-aware — `_"Ode"_` still opens;
  `'90s` stays an apostrophe). `utils.smarten_quotes`.
- **New style rule `quote_punct`** (Minkowski + generic): flags a period or
  comma after a closing double quote («..."sentence".») — American style puts
  them inside. Auto-fixable (swaps the punctuation in); single quotes are
  skipped so possessives like «writers'.» don't false-positive.
- **Notification e-mails** (comments + new questions) now end with a direct
  link to the recipient's per-set e-mail settings
  (`/writer_question_set_settings/<id>/`) instead of "change the settings in
  your profile" with no link.
- Cache busters: `base.css?v=47`, `paste_convert.js?v=19`.

## 2026-07-04 — Category Issues page

- New **Category Issues** page (`/category_problems/<qset_id>/`, "Category
  Issues" in the sidebar Tools section): lists every question in the set with
  **no category** or a category that belongs to a **different distribution**
  than the set's current one (e.g. after switching distributions or moving
  questions between sets). Select-all + per-question checkboxes and a
  category dropdown bulk-reassign the selected questions; the target must be
  an entry of the set's own distribution (validated server-side). Any set
  member can view; only owners/editors can reassign.

## 2026-07-04 — Set-wide distribution seeded with tossups/bonuses swapped

- Creating a question set seeded its set-wide distribution entries with the
  values CROSSED: `num_bonuses` got `packets x min_tossups` and `num_tossups`
  got `packets x min_bonuses` (`create_question_set`, views.py). A tossup-only
  template therefore produced a set expecting 0 tossups and N bonuses per
  category. Fixed; the other two seeding sites (distribution reassignment)
  were already correct. Sets created while the bug existed keep their swapped
  set-wide numbers — fix them on the set-wide distribution page (or recreate
  the set).

## 2026-07-04 — Home action buttons alignment

- The home page's action buttons rendered with a phantom empty first cell and
  ragged heights: the `home-actions` ul is a CSS grid, and Foundation's
  `.button-group` clearfix `::before` (`content: " "; display: table`) became
  an anonymous grid item occupying the first cell, shoving all five buttons
  right. The clearfix pseudo-elements are now suppressed on that grid, and
  each button fills its cell (flex-centered label, equal heights across the
  row). `base.css?v=46`.

## 2026-07-04 — Edit-page Copy button actually copies

- The "Copy" button on edit tossup mistakenly reused the add-page's
  `paste-full-tossup` id, so clicking it opened the **paste** dialog. It's now
  `copy-full-tossup` and copies the current form's question + answer line to
  the clipboard in two flavors: rich text (`text/html` — real
  bold/underline/italics/sup/sub, power bolded, escapes resolved) for
  Word/Docs/email, and the raw QEMS markup as `text/plain` (falls back to
  plain-only where `ClipboardItem` is unavailable). Edit bonus gained a
  matching "Copy" button (`copy-full-bonus`, `[10x]`-labelled parts). The add
  page's "Paste Full Tossup" dialog is unchanged. `paste_convert.js?v=18`.

## 2026-07-03 — Rich-editor italics + per-question style check

- **Rich editor legibility (italics)**: the editing surface now uses
  `Georgia, 'Times New Roman', serif` — a true italic instead of the sans
  stack's oblique (an oblique `\` read as `|`, and the slant hid where italic
  runs ended). Italic runs get their own ink color (blue; lighter variant in
  dark mode) plus a hair of clearance after the run, so their extent is
  obvious without a background box (a tint was tried and rejected). Replaced
  the old `-0.07em` negative-margin hack. `base.css?v=45`.
- **Style check on the edit pages**: new "Style Check" panel on edit
  tossup/bonus (`_style_check_panel.html`, loaded via the new
  `/question_style_issues/` JSON endpoint). Shows the question's style issues
  using the set's rule configuration, honoring per-question and set-wide
  dismissals; issues can be Fixed (auto-fixable ones) or dismissed inline via
  the existing `/apply_style_fix/` + `/dismiss_style_issue/` endpoints, with
  a "recheck" link and a link to the whole-set style check.
- **Packet grid status tags**: each grid cell now shows explicit **E**
  (edited, blue) and **P** (proofread, green) chips on the category line,
  replacing the old barely-visible gray pencil that only covered "edited".

## 2026-07-03 — Swap-dialog search fix

- **Swap dialog search missed questions when the query spanned stored markup**
  (e.g. "marc jacobs" never matched an answer stored as `Marc _Jacobs_`),
  HTML entities (`&#x27;` from imports), or pronunciation guides. The
  `/swap_candidates/` search now matches in Python on normalized text
  (entities decoded, markup/tags stripped, diacritics removed, casefolded)
  with every search word required somewhere in the question's answer/text —
  so word order and accents don't matter either. Applies to both the packet
  grid and doc-view swap dialogs.
- Both dialogs also drop stale out-of-order search responses (a slower older
  request can no longer overwrite the results of the latest keystroke).

## 2026-07-03 — Alternate-answer formatting (style check)

On `packetization-and-editor-tools`.

- **Standard "; or" separators** for alternate-answer suggestions everywhere:
  - The `answer_alts` style rule now writes "also accept X; or Y" (was "X / Y").
  - The AI alternate-answer prompt requires "; or" between names and forbids
    slashes.
- **Answer-line formatting in suggestions**:
  - `answer_alts` suggestions render each alternate underlined + bold (italic
    too when the primary answer is a tilde-marked title), via a new optional
    `message_html` field on style issues (escaped server-side; used by the
    style-check page and the doc view's style-issues toggle).
  - The AI answer pass now *sees* the raw answer-line markup (a separate
    markup-preserving `answer_items` list; the grammar pass still gets plain
    text) and is instructed to emit QEMS markup: `_underscores_` around
    required words, `~tildes~` for titles, plain names after "prompt on".
    Suggestions render formatted on the style-check page (`suggestion_html`).

## 2026-06-17 → 2026-06-19 — Playtesting integration, packet audio, deploy tooling

Commits `6cc2a25` and `b84a70b` on `packetization-and-editor-tools`
(pushed to fork `mbentley00/qems2` `master`; deployed to Azure).

### Packet → MP3 audio export
- New `qsub/audio.py`: reads a packet's tossups/bonuses and renders an MP3.
  - Synthesis: **edge-tts** (free Microsoft neural voices, no API key).
    Default voice **Ava** (`en-US-AvaMultilingualNeural`); selectable from a set
    of natural voices.
  - Assembly: single **ffmpeg** concat pass with silence gaps (pause before
    answers). ffmpeg comes from the `imageio-ffmpeg` pip package in production
    (the Oryx App Service has no system ffmpeg).
  - `clean_for_speech()` strips QEMS markup and **drops pronunciation guides /
    moderator notes** (raw `("...")`, `(read slowly)`) while keeping escaped
    `\(...\)`.
  - On-disk cache keyed by packet + order + answers + voice + version.
- **Background generation** (safe across the 3 gunicorn workers via lock/status/
  err files): `/packet_mp3/<id>/` serves the file when ready, otherwise starts a
  render and shows a polling "preparing" page that auto-downloads when done;
  `/packet_mp3_status/<id>/` returns JSON status (no-store to avoid stale polls).
- **"Turn into MP3"** buttons on the packet page (`edit_packet.html`) and the
  document view (`view_packet.html`, with a voice picker and order toggle).

### Discord bot ↔ QEMS integration
- **Bot comments are attributed to the persona "Cliff"** (`api.DISCORD_BOT_NAME`).
- **Discord thread links on the question** (not buried in comment text):
  - `DiscordThread` model (migration `0016_discordthread`).
  - `POST /api/v1/threads` — records a thread URL against a question, matched by
    answer line, idempotent via `external_id`.
  - "Discord Playtest Thread(s)" panel on the edit tossup/bonus pages
    (`_discord_threads.html`).
- Fixed the comment-notification email signal (`signals.py email_on_comments`)
  to be null-safe for userless bot comments — no more "Error sending mail".
- (Bot API overall: `qsub/api.py` — `ping`, `buzzes`, `bonus_results`,
  `comments`, `threads`; per-set key from *Edit Question Set → Bot API*.)

### UI
- Moved the **Bot API** button to the bottom of the edit-question-set page, next
  to **Set-Specific Distribution**.

### Deployment & infra
- **`DEPLOYMENT.md`** runbook + **`build_deploy_zip.py`**. Production is a
  code/Oryx **Python 3.14 App Service** `qems2` (RG `qems2_group`), deployed
  manually with `az webapp deploy --type zip`.
  - **Deploy from the local tree, never `git archive`**: the front-end deps live
    in git-ignored `components/` (bower); a git-only deploy drops them and breaks
    all interactive JS and Font Awesome.
- Added `edge-tts` and `imageio-ffmpeg` to `requirements.txt`.

### Git / repo
- Retargeted `origin` to the fork `mbentley00/qems2`; kept `grapesmoker/qems2` as
  `upstream`. Pushed work to the fork's `master`.

### Companion project (Discord bot — `discord_qems`, separate, not in this repo)
- `QEMS_BOT_API.md`: full reference for the bot's API calls (auth, answer-line
  matching, idempotency, all endpoints incl. `/threads`, Python client).
- Reader scans the playtest server's `#results` threads, captures the Botero
  bot "Results" posts, and extracts answers; `audio_writer.py` is a CLI MP3
  generator mirroring the in-app one.
