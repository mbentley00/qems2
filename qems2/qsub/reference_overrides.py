"""Admin edits to the bundled reference datasets, as seen by the loaders.

``pron_dict`` and ``answer_db`` read data files that are generated offline, so a
correction can't be written back into them. Instead an admin's fix is stored as
a ``ReferenceDataOverride`` row and merged in when the loader builds its cache
(see that model for the shape).

The loaders cache aggressively — the pronunciation matcher and the answer
database are both built once and reused for the life of the process — so this
module also provides the *stamp*: a cheap (count, latest-change) summary of the
override table that a cached structure is tagged with. When the stamp moves, the
cache is stale. The stamp itself is re-queried at most once every
``STAMP_TTL_SECONDS``, so a style check over a whole set doesn't run one query
per question. An admin's own worker sees an edit immediately (the save calls the
loaders' ``reset_cache``); other workers pick it up within the TTL.
"""

import time

PRONUNCIATION = 'pron'
ANSWER_LINE = 'answer'

#: How long a stamp reading is trusted before it's re-queried.
STAMP_TTL_SECONDS = 60

# {dataset: (read_at, stamp)}
_STAMPS = {}


def _model():
    """Imported lazily: this module is loaded from ``pron_dict``, which must stay
    importable (and useful) without the Django app registry being ready."""
    from .models import ReferenceDataOverride
    return ReferenceDataOverride


def rows(dataset):
    """The overrides for `dataset` as (key, term, value, suppressed) tuples.
    Empty if the table isn't available (e.g. before migrations have run)."""
    try:
        return list(_model().objects.filter(dataset=dataset)
                    .values_list('key', 'term', 'value', 'suppressed'))
    except Exception:
        return []


def stamp(dataset):
    """A value that changes whenever `dataset`'s overrides do. Cached for
    ``STAMP_TTL_SECONDS`` so hot paths don't query per call."""
    now = time.time()
    cached = _STAMPS.get(dataset)
    if cached is not None and now - cached[0] < STAMP_TTL_SECONDS:
        return cached[1]
    try:
        from django.db.models import Count, Max
        agg = (_model().objects.filter(dataset=dataset)
               .aggregate(n=Count('id'), last=Max('changed_date')))
        value = (agg['n'], agg['last'].isoformat() if agg['last'] else '')
    except Exception:
        value = (0, '')
    _STAMPS[dataset] = (now, value)
    return value


def reset_stamp(dataset=None):
    """Force the next stamp read to hit the database. Called after a save so the
    editing admin sees their change on the very next page."""
    if dataset is None:
        _STAMPS.clear()
    else:
        _STAMPS.pop(dataset, None)
