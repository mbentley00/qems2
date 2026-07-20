# Changelog

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
