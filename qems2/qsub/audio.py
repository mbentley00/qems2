"""Generate an MP3 reading of a packet for the QEMS2 web UI.

edge-tts (free Microsoft neural voices, no API key) synthesizes each utterance;
segments are then stitched together with silence gaps (so there is a pause to
guess before each answer) using a single ffmpeg concat pass.

Speed: utterances are synthesized concurrently (network-bound), and assembly is
one ffmpeg process rather than one per segment. Generated files are cached on
disk keyed by packet + order + answers, and regenerated only when a question in
the packet has changed since the cache was written.
"""

import asyncio
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time

# Quizbowl reading wants a measured, even cadence, not chat prosody — so the
# defaults come from Microsoft's "News/Novel" narration voices rather than the
# conversational "Multilingual" line (Ava etc. sound expressive but rush and
# swallow clauses when reading question text). (id, friendly label) — the
# first is the default. NATURAL_VOICES validates the ?voice= param.
VOICE_CHOICES = [
    ("en-US-AriaNeural", "Aria — newscast US female (default)"),
    ("en-US-ChristopherNeural", "Christopher — measured US male"),
    ("en-US-EricNeural", "Eric — even-keeled US male"),
    ("en-US-SteffanNeural", "Steffan — steady US male"),
    ("en-US-MichelleNeural", "Michelle — clear US female"),
    ("en-US-JennyNeural", "Jenny — clear US female"),
    ("en-US-AvaMultilingualNeural", "Ava — expressive US female (conversational)"),
    ("en-US-AndrewMultilingualNeural", "Andrew — warm US male (conversational)"),
    ("en-GB-RyanNeural", "Ryan — British male"),
    ("en-GB-SoniaNeural", "Sonia — British female"),
]
NATURAL_VOICES = [v for v, _label in VOICE_CHOICES]
DEFAULT_VOICE = VOICE_CHOICES[0][0]
ANSWER_GAP = 3.0   # silence (s) after a question before its answer
INTER_GAP = 1.0    # silence (s) between questions
LEADIN_GAP = 0.5   # small breaths inside a bonus

CONCURRENCY = 24       # simultaneous edge-tts requests
SYNTH_TIMEOUT = 60     # seconds per utterance before giving up
SAMPLE_RATE = 24000    # edge-tts output rate; silence is generated to match

# Bump when the audio format/pacing/cleaning changes so stale files are rebuilt.
CACHE_VERSION = 3
STALE_LOCK_SECONDS = 600   # treat a lock older than this as a crashed job

CACHE_DIR = os.path.join(tempfile.gettempdir(), "qems2_packet_audio")


# ── Markup cleaning ──────────────────────────────────────────────────────

_HTML_RE = re.compile(r"<[^>]+>")
_LEADING_NOTE_RE = re.compile(
    r"^~\s*(written by|note|editor|moderator note|mod note)\b[^~]*~\s*\.?\s*",
    re.IGNORECASE,
)
_POWER_RE = re.compile(r"\(\*\)")
_RAW_PAREN_RE = re.compile(r"\([^)]*\)")
_WS_RE = re.compile(r"\s+")


def clean_for_speech(text):
    """Strip QEMS2 / HTML markup so the text reads naturally aloud.

    In QEMS, pronunciation guides and moderator instructions use raw parens
    (e.g. ("LIN-de-min"), (read slowly)) while parentheses meant to be read are
    escaped (\\(...\\)). So we drop raw parentheticals but keep escaped ones.
    """
    if not text:
        return ""
    text = _HTML_RE.sub(" ", text)
    text = _LEADING_NOTE_RE.sub("", text)
    text = _POWER_RE.sub(" ", text)
    # Protect intentional (escaped) parens, drop raw parenthetical guides/notes,
    # then restore the intentional ones as literal characters.
    text = text.replace("\\(", "\x00").replace("\\)", "\x01")
    text = _RAW_PAREN_RE.sub(" ", text)
    text = text.replace("\x00", "(").replace("\x01", ")")
    text = text.replace("\\[", "[").replace("\\]", "]")
    text = text.replace("[", ", ").replace("]", " ")
    # Formatting toggles that are never read aloud (bold-only, sub/sup, and
    # pronunciation-guide target markers).
    for marker in ("\\B", "\\S", "\\s", "\\P"):
        text = text.replace(marker, "")
    text = text.replace("~", "").replace("_", "").replace("*", "")
    text = _WS_RE.sub(" ", text)
    text = text.replace(" ,", ",").replace(" ;", ";")
    return text.strip()


# ── Script building (operates on model instances) ────────────────────────

def _tossup_utterances(tu, include_answers, answer_gap):
    n = tu.question_number or 0
    items = [("Tossup {0}. {1}".format(n, clean_for_speech(tu.tossup_text)), answer_gap)]
    if include_answers:
        items.append(("Answer: {0}".format(clean_for_speech(tu.tossup_answer)), INTER_GAP))
    return items


def _bonus_utterances(b, include_answers, answer_gap):
    n = b.question_number or 0
    items = [("Bonus {0}. {1} For ten points each.".format(n, clean_for_speech(b.leadin)), LEADIN_GAP)]
    for i in (1, 2, 3):
        ptext = clean_for_speech(getattr(b, "part{0}_text".format(i)))
        if not ptext:
            continue
        items.append((ptext, answer_gap))
        if include_answers:
            pans = clean_for_speech(getattr(b, "part{0}_answer".format(i)))
            items.append(("Answer: {0}".format(pans), LEADIN_GAP))
    if items:
        items[-1] = (items[-1][0], INTER_GAP)
    return items


def build_script(tossups, bonuses, interleaved, include_answers, answer_gap=ANSWER_GAP):
    script = []
    if interleaved:
        for i in range(max(len(tossups), len(bonuses))):
            if i < len(tossups):
                script += _tossup_utterances(tossups[i], include_answers, answer_gap)
            if i < len(bonuses):
                script += _bonus_utterances(bonuses[i], include_answers, answer_gap)
    else:
        for tu in tossups:
            script += _tossup_utterances(tu, include_answers, answer_gap)
        for b in bonuses:
            script += _bonus_utterances(b, include_answers, answer_gap)
    return script


# ── Synthesis + assembly ─────────────────────────────────────────────────

def _ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    # No system ffmpeg (e.g. the Azure App Service code/Oryx deploy) — fall back
    # to the static binary bundled by the imageio-ffmpeg pip package.
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        raise RuntimeError(
            "ffmpeg not found; install ffmpeg or the imageio-ffmpeg package")


async def _synth_one(sem, text, voice, out_path, retries=2):
    import edge_tts
    for attempt in range(retries + 1):
        try:
            async with sem:
                await asyncio.wait_for(
                    edge_tts.Communicate(text, voice).save(out_path), SYNTH_TIMEOUT
                )
            if os.path.getsize(out_path) > 0:
                return
            raise RuntimeError("empty audio")
        except Exception:
            if attempt == retries:
                raise
            await asyncio.sleep(0.5 * (attempt + 1))


async def _synth_all(script, voice, tmp):
    """Synthesize every utterance concurrently. Returns ordered file paths."""
    sem = asyncio.Semaphore(CONCURRENCY)
    paths, tasks = [], []
    for idx, (text, _gap) in enumerate(script):
        p = os.path.join(tmp, "seg_{0:04d}.mp3".format(idx))
        paths.append(p)
        tasks.append(_synth_one(sem, text, voice, p))
    await asyncio.gather(*tasks)
    return paths


def _silence_path(seconds):
    """Path to a cached silent MP3 of the given duration (generated once)."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    ms = int(round(seconds * 1000))
    p = os.path.join(CACHE_DIR, "silence_{0}ms.mp3".format(ms))
    if not os.path.exists(p) or os.path.getsize(p) == 0:
        subprocess.run(
            [_ffmpeg(), "-y", "-f", "lavfi",
             "-i", "anullsrc=r={0}:cl=mono".format(SAMPLE_RATE),
             "-t", str(seconds), "-q:a", "9", p],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    return p


def _concat_to(out_path, seg_paths, gaps, tmp):
    """Stitch speech segments + silence into out_path with one ffmpeg pass."""
    lines = []
    for seg, gap in zip(seg_paths, gaps):
        lines.append("file '{0}'".format(seg.replace("\\", "/")))
        if gap and gap > 0:
            lines.append("file '{0}'".format(_silence_path(gap).replace("\\", "/")))
    list_path = os.path.join(tmp, "concat.txt")
    with open(list_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    # All inputs are 24 kHz mono MP3, so concatenate by copying frames (no
    # re-encode) for speed.
    subprocess.run(
        [_ffmpeg(), "-y", "-f", "concat", "-safe", "0", "-i", list_path,
         "-c", "copy", out_path],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _render_to(script, voice, out_path):
    with tempfile.TemporaryDirectory() as tmp:
        seg_paths = asyncio.run(_synth_all(script, voice, tmp))
        gaps = [gap for _text, gap in script]
        _concat_to(out_path, seg_paths, gaps, tmp)


def _latest_change(tossups, bonuses):
    """Most recent last_changed_date across the packet's questions (epoch)."""
    latest = 0.0
    for q in list(tossups) + list(bonuses):
        d = getattr(q, "last_changed_date", None)
        if d is not None:
            latest = max(latest, d.timestamp())
    return latest


def _voice_slug(voice):
    return re.sub(r"[^A-Za-z0-9]+", "-", voice or DEFAULT_VOICE)


def cache_file(packet_id, interleaved, include_answers, voice=DEFAULT_VOICE):
    """Path to the cached MP3 for this packet + order + answers + voice."""
    order = "interleaved" if interleaved else "separate"
    ans = "qa" if include_answers else "q"
    return os.path.join(
        CACHE_DIR,
        "packet_{0}_{1}_{2}_{3}_v{4}.mp3".format(
            packet_id, order, ans, _voice_slug(voice), CACHE_VERSION),
    )


def _is_fresh(out_path, latest):
    return os.path.exists(out_path) and os.path.getmtime(out_path) >= latest


def _generate(out_path, script, voice):
    """Render to a temp file then atomically move into place."""
    tmp_out = out_path + ".tmp.mp3"
    _render_to(script, voice, tmp_out)
    os.replace(tmp_out, out_path)


def packet_mp3_path(packet, tossups, bonuses, interleaved=False, include_answers=True,
                    voice=DEFAULT_VOICE):
    """Synchronously return the path to an up-to-date MP3, generating it (and
    caching on disk) only if missing or stale."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    out_path = cache_file(packet.id, interleaved, include_answers, voice)
    if _is_fresh(out_path, _latest_change(tossups, bonuses)):
        return out_path
    script = build_script(tossups, bonuses, interleaved, include_answers)
    if not script:
        raise ValueError("Packet has no questions to read")
    _generate(out_path, script, voice)
    return out_path


# ── Background generation (filesystem-coordinated, safe across workers) ───

def packet_status(packet, tossups, bonuses, interleaved, include_answers,
                  voice=DEFAULT_VOICE):
    """'ready' | 'running' | 'error' | 'absent' for the cached MP3."""
    out_path = cache_file(packet.id, interleaved, include_answers, voice)
    if _is_fresh(out_path, _latest_change(tossups, bonuses)):
        return "ready"
    if os.path.exists(out_path + ".err"):
        return "error"
    lock = out_path + ".lock"
    if os.path.exists(lock):
        if time.time() - os.path.getmtime(lock) > STALE_LOCK_SECONDS:
            return "absent"   # crashed job; a new request may retake the lock
        return "running"
    return "absent"


def error_message(packet_id, interleaved, include_answers, voice=DEFAULT_VOICE):
    err = cache_file(packet_id, interleaved, include_answers, voice) + ".err"
    try:
        with open(err, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def start_generation(packet, tossups, bonuses, interleaved=False, include_answers=True,
                     voice=DEFAULT_VOICE):
    """Kick off generation in a background thread if not already done/running.

    Coordination is via an exclusive lock file, so concurrent requests across
    gunicorn workers will not start duplicate renders. Returns the status.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    out_path = cache_file(packet.id, interleaved, include_answers, voice)
    if _is_fresh(out_path, _latest_change(tossups, bonuses)):
        return "ready"

    lock = out_path + ".lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        if time.time() - os.path.getmtime(lock) <= STALE_LOCK_SECONDS:
            return "running"
        # Stale lock from a crashed job — try to retake it.
        try:
            os.remove(lock)
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
        except (OSError, FileExistsError):
            return "running"

    # Clear any previous error now that we're (re)starting.
    try:
        os.remove(out_path + ".err")
    except OSError:
        pass

    def work():
        try:
            script = build_script(tossups, bonuses, interleaved, include_answers)
            if not script:
                raise ValueError("Packet has no questions to read")
            _generate(out_path, script, voice)
        except Exception as e:
            try:
                with open(out_path + ".err", "w", encoding="utf-8") as f:
                    f.write(str(e))
            except OSError:
                pass
        finally:
            try:
                os.remove(lock)
            except OSError:
                pass

    threading.Thread(target=work, daemon=True).start()
    return "running"
