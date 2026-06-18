"""Build the bundled verified-OL pronunciation dictionary used by the style
checker to suggest missing pronunciation guides.

Source: the Minkowski quizbowl pronouncing dictionary
(https://minkowski.space/quizbowl/pronouncing-dictionary/index.html), a single
large HTML page. Each entry looks like:

    <div class="entry keep" id="...">
      <a class="mr"><b class="headword">Abilene</b></a>
      <span class="mr"><span class="pron">AB-uh-leen</span></span>
      <span class="etym mr">...</span>
      <a class="author" data-author="OL">OL</a>      <!-- the verified author -->
      <div class="usage">...category | definition...</div>
      <div class="extlinks">Forvo | ...</div>
    </div>

We keep only entries whose author is "OL" (the maintainer-verified guides, as
opposed to crowd-sourced Forvo links), and write a compact
``{normalized_term: "RESPELLING"}`` JSON to qsub/data/pronunciations.json, which
ships with the app and is loaded by style_checker.

Refresh with:  python manage.py build_pron_dict
(use --source <path> to parse a previously-downloaded copy instead of fetching).
"""

import json
import os
import re
import urllib.request

from django.core.management.base import BaseCommand

from qems2.qsub.pron_dict import DATA_PATH, normalize_term

DEFAULT_URL = 'https://minkowski.space/quizbowl/pronouncing-dictionary/index.html'
VERIFIED_AUTHOR = 'OL'


class Command(BaseCommand):
    help = 'Build the bundled verified-OL pronunciation dictionary for the style checker.'

    def add_arguments(self, parser):
        parser.add_argument('--source', default=DEFAULT_URL,
                            help='URL or local file path of the pronouncing dictionary HTML.')
        parser.add_argument('--author', default=VERIFIED_AUTHOR,
                            help='data-author code to treat as verified (default: OL).')

    def handle(self, *args, **opts):
        from bs4 import BeautifulSoup

        source = opts['source']
        author = opts['author']

        if os.path.exists(source):
            self.stdout.write('Reading {0}'.format(source))
            with open(source, encoding='utf-8') as fh:
                html = fh.read()
        else:
            self.stdout.write('Downloading {0}'.format(source))
            with urllib.request.urlopen(source, timeout=120) as resp:
                html = resp.read().decode('utf-8', 'replace')

        soup = BeautifulSoup(html, 'html.parser')

        # Stoplist: the ~10k most common English words. A single-word headword
        # that is a common word (e.g. "were", "grave", "reading", or a common
        # given name like "Robert") almost never needs a guide and would fire on
        # nearly every question, so we drop those. Multi-word and hyphenated
        # headwords are specific enough to keep.
        stop_path = os.path.join(os.path.dirname(DATA_PATH), 'common_words.txt')
        stopwords = set()
        if os.path.exists(stop_path):
            with open(stop_path, encoding='utf-8') as fh:
                stopwords = {w.strip().lower() for w in fh if w.strip()}

        out = {}
        kept = skipped = stopped = 0
        for entry in soup.select('div.entry'):
            author_el = entry.select_one('a.author')
            if author_el is None or (author_el.get('data-author') or '').strip() != author:
                continue
            hw_el = entry.select_one('b.headword')
            pron_el = entry.select_one('span.pron')
            if hw_el is None or pron_el is None:
                continue
            term = hw_el.get_text(' ', strip=True)
            pron = pron_el.get_text(' ', strip=True)
            key = normalize_term(term)
            # Keep only alphabetic headwords worth matching on. Reject anything
            # with digits or symbols (e.g. "A260", "SO(3)", "1:1") — those collapse
            # to a bare letter token and match everywhere — and require real length.
            if not pron or len(key) < 3:
                skipped += 1
                continue
            if not re.fullmatch(r"[^\W\d_]+(?:[ '’\-][^\W\d_]+)*", key):
                skipped += 1
                continue
            single_word = not re.search(r"[ '’\-]", key)
            if single_word and key in stopwords:
                stopped += 1
                continue
            # First verified pronunciation wins (entries are alphabetized; a later
            # duplicate headword shouldn't clobber the primary respelling).
            if key not in out:
                out[key] = {'term': term, 'pron': pron}
                kept += 1

        os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)
        with open(DATA_PATH, 'w', encoding='utf-8') as fh:
            json.dump(out, fh, ensure_ascii=False, sort_keys=True, indent=0)

        size_kb = os.path.getsize(DATA_PATH) / 1024
        self.stdout.write(self.style.SUCCESS(
            'Wrote {0} verified-{1} entries ({2} skipped, {3} common-word) to {4} ({5:.0f} KB)'.format(
                kept, author, skipped, stopped, DATA_PATH, size_kb)))
