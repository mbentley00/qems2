"""The per-packet distribution: one page that says what every packet holds.

A set on a per-packet distribution has DistributionLine rows ("4 / 4
Literature", with a breakdown underneath) and four numbers of its own --
tiebreakers in each packet and extras for the set, tossups and bonuses.
Everything else the app reads is worked out from those and the set's size:

* the set totals the category overview, the dashboard and the packet page
  read (SetWideDistributionEntry), and
* the per-packet quotas the packetizer reads (PacketizationEntry), plus one
  CategorySuperGroup per pool so the overview shows the pool as one row.

`derive` writes them. Nothing here is typed into those tables any more, so
there is nothing to keep in step: change the packet count and every total
moves with it.

Sets that have not switched are not touched by any of this. Switching keeps a
copy of what the set had (`snapshot_legacy`), so it can be undone, and gives
the set its own copy of its distribution when the one it is on is shared
(`give_set_own_distribution`), so editing its categories never reaches
another set.
"""

import difflib
import json
import math
import re
from collections import Counter
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_HALF_UP
from itertools import combinations

from django.db import transaction
from django.db.models import Count

from qems2.qsub.models import (
    Bonus, CategorySuperGroup, CategoryTag, Distribution, DistributionEntry,
    DistributionLine, DistributionLineItem, EditorTag, PacketizationEntry,
    PerCategoryWriterSettings, QuestionSet, SetWideDistributionEntry,
    TieBreakDistributionEntry, Tossup, Writer)

ZERO = Decimal('0')


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def to_decimal(raw, allow_blank=False):
    """A per-packet count from a form or a pasted line. Accepts "1.25", "1¼"
    is not attempted. Blank is None when allowed, else 0."""
    if raw is None:
        return None if allow_blank else ZERO
    raw = str(raw).strip().replace(',', '.')
    if raw == '':
        return None if allow_blank else ZERO
    if raw in ('½', '.5'):
        raw = '0.5'
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise ValueError('"{0}" is not a number'.format(raw))
    if value < 0:
        raise ValueError('Counts cannot be negative')
    if value > 1000:
        raise ValueError('"{0}" is too large for a per-packet count'.format(raw))
    return value.quantize(Decimal('0.01'))


def fmt(value):
    """1.50 -> "1.5", 4.00 -> "4", None -> ""."""
    if value is None:
        return ''
    d = Decimal(value).quantize(Decimal('0.01')).normalize()
    s = format(d, 'f')
    return '0' if s in ('-0', '') else s


def pair(tu, bs):
    return '{0} / {1}'.format(fmt(tu), fmt(bs))


def largest_remainder(total, weights):
    """Split the whole number `total` in proportion to `weights` so the parts
    are whole and add up to `total` exactly: everyone gets the floor of their
    share and the largest remainders get the leftover ones. All-zero weights
    split evenly."""
    n = len(weights)
    if n == 0 or total <= 0:
        return [0] * n
    weights = [max(0.0, float(w)) for w in weights]
    s = sum(weights)
    if s <= 0:
        weights = [1.0] * n
        s = float(n)
    shares = [total * w / s for w in weights]
    floors = [int(math.floor(x + 1e-9)) for x in shares]
    left = total - sum(floors)
    order = sorted(range(n), key=lambda i: (-(shares[i] - floors[i]), i))
    for i in order[:max(0, left)]:
        floors[i] += 1
    return floors


def path_parts(path):
    return tuple(p.strip() for p in (path or '').split(' - ') if p.strip())


def entry_parts(entry):
    parts = [entry.category.strip()] if entry.category else []
    for sub in (entry.subcategory or '').split(' - '):
        if sub.strip():
            parts.append(sub.strip())
    return tuple(parts)


def entry_path(entry):
    return ' - '.join(entry_parts(entry))


def _norm(text):
    text = (text or '').lower().replace('&', ' and ').replace('+', ' and ')
    text = re.sub(r'[^a-z0-9 ]+', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def set_entries(qset):
    return list(qset.distribution.distributionentry_set.all())


def entries_by_top(qset):
    """{top-level name (lower case): [DistributionEntry, ...]}"""
    out = {}
    for e in set_entries(qset):
        parts = entry_parts(e)
        if parts:
            out.setdefault(parts[0].lower(), []).append(e)
    return out


def top_level_names(qset):
    """The set's top-level category names, as spelled in its distribution."""
    seen = {}
    for e in set_entries(qset):
        parts = entry_parts(e)
        if parts and parts[0].lower() not in seen:
            seen[parts[0].lower()] = parts[0]
    return sorted(seen.values(), key=str.lower)


def category_paths(qset):
    """Every category path of the set (tops and their subcategories)."""
    paths = set()
    for e in set_entries(qset):
        parts = entry_parts(e)
        for i in range(1, len(parts) + 1):
            paths.add(' - '.join(parts[:i]))
    return sorted(paths, key=str.lower)


# --------------------------------------------------------------------------
# A set's own distribution
# --------------------------------------------------------------------------

def owns_distribution(qset):
    dist = qset.distribution
    return dist.owner_set_id == qset.id


def give_set_own_distribution(qset):
    """Put the set on a distribution nobody else uses, copying the one it is
    on if that one is shared -- by another set, or as a template people pick
    when they create a set. The set's questions, set totals, tiebreak rows and
    writers' per-category settings move onto the copy's categories, so they
    read exactly as before. Returns True if a copy was made.

    A distribution only this set uses, and that is not offered to anyone
    else, is simply marked as the set's own.
    """
    dist = qset.distribution
    if dist.owner_set_id == qset.id:
        return False
    others = dist.questionset_set.exclude(id=qset.id).exists()
    shared = others or dist.public or dist.owner_set_id is not None
    if not shared:
        dist.owner_set = qset
        dist.save(update_fields=['owner_set'])
        return False

    with transaction.atomic():
        copy = Distribution.objects.create(
            name='{0} (distribution)'.format(qset.name)[:100],
            acf_tossup_per_period_count=dist.acf_tossup_per_period_count,
            acf_bonus_per_period_count=dist.acf_bonus_per_period_count,
            vhsl_bonus_per_period_count=dist.vhsl_bonus_per_period_count,
            created_by=qset.owner, public=False, owner_set=qset)
        moved = {}
        for e in dist.distributionentry_set.all():
            ne = DistributionEntry.objects.create(
                distribution=copy, category=e.category, subcategory=e.subcategory,
                min_tossups=e.min_tossups, min_bonuses=e.min_bonuses,
                max_tossups=e.max_tossups, max_bonuses=e.max_bonuses)
            moved[e.id] = ne.id
        for old_id, new_id in moved.items():
            Tossup.objects.filter(question_set=qset, category_id=old_id).update(category_id=new_id)
            Bonus.objects.filter(question_set=qset, category_id=old_id).update(category_id=new_id)
            SetWideDistributionEntry.objects.filter(
                question_set=qset, dist_entry_id=old_id).update(dist_entry_id=new_id)
            TieBreakDistributionEntry.objects.filter(
                question_set=qset, dist_entry_id=old_id).update(dist_entry_id=new_id)
            PerCategoryWriterSettings.objects.filter(
                writer_question_set_settings__question_set=qset,
                distribution_entry_id=old_id).update(distribution_entry_id=new_id)
        qset.distribution = copy
        qset.save(update_fields=['distribution'])
    return True


def copy_distribution_for(new_qset, source_dist):
    """A fresh copy of `source_dist`'s categories for a new set that has no
    questions yet. Returns the copy, already attached to `new_qset`."""
    copy = Distribution.objects.create(
        name='{0} (distribution)'.format(new_qset.name)[:100],
        acf_tossup_per_period_count=source_dist.acf_tossup_per_period_count,
        acf_bonus_per_period_count=source_dist.acf_bonus_per_period_count,
        vhsl_bonus_per_period_count=source_dist.vhsl_bonus_per_period_count,
        created_by=new_qset.owner, public=False, owner_set=new_qset)
    for e in source_dist.distributionentry_set.all():
        DistributionEntry.objects.create(
            distribution=copy, category=e.category, subcategory=e.subcategory,
            min_tossups=e.min_tossups, min_bonuses=e.min_bonuses,
            max_tossups=e.max_tossups, max_bonuses=e.max_bonuses)
    new_qset.distribution = copy
    new_qset.save(update_fields=['distribution'])
    return copy


def ensure_entry(qset, path):
    """The set's category at `path`, made if it isn't there. Only ever called
    on a set that owns its distribution."""
    parts = path_parts(path)
    if not parts:
        return None
    category, sub = parts[0], ' - '.join(parts[1:])
    for e in qset.distribution.distributionentry_set.all():
        if entry_parts(e) == parts:
            return e
        if (len(parts) == 1 and entry_parts(e)[:1] == parts) or \
                (len(parts) > 1 and entry_parts(e)[:len(parts)] == parts):
            # Something under this path already exists; the path is covered.
            return e
    # Match a top-level name case-insensitively before inventing a new one.
    for e in qset.distribution.distributionentry_set.all():
        if entry_parts(e)[:1] and entry_parts(e)[0].lower() == category.lower():
            category = entry_parts(e)[0]
            break
    entry = DistributionEntry.objects.create(
        distribution=qset.distribution, category=category, subcategory=sub,
        min_tossups=0, min_bonuses=0, max_tossups=0, max_bonuses=0)
    SetWideDistributionEntry.objects.get_or_create(
        question_set=qset, dist_entry=entry, defaults={'num_tossups': 0, 'num_bonuses': 0})
    return entry


# --------------------------------------------------------------------------
# Working out the totals
# --------------------------------------------------------------------------

def line_totals(qset, lines=None):
    """How many of each type every line needs across the set, split into what
    goes in the packets, the line's share of the tiebreakers, and its share of
    the extras. {line.id: {'tu': {...}, 'bs': {...}}} plus a 'set' summary.

    Each column is split with largest remainders so it adds up to its total
    exactly: 1.25 Biology over 10 packets is 12.5, and one of the lines with a
    half gets the extra question.
    """
    if lines is None:
        lines = list(qset.distribution_lines.all())
    n = max(qset.num_packets or 0, 0)
    out = {line.id: {'tu': {}, 'bs': {}} for line in lines}
    summary = {}

    def split(total, fixed, weights):
        """Lines given a number by hand keep it; the rest of `total` is
        shared by the others in proportion to their weights."""
        rest = max(0, total - sum(f for f in fixed if f is not None))
        free = [i for i, f in enumerate(fixed) if f is None]
        shares = largest_remainder(rest, [weights[i] for i in free]) if free else []
        parts = [f if f is not None else 0 for f in fixed]
        for i, share in zip(free, shares):
            parts[i] = share
        return parts

    for key, attr, tb_pp, extras, tb_attr, x_attr in (
            ('tu', 'tossups', qset.tiebreak_tossups_per_packet, qset.extra_tossups,
             'tiebreak_tossups', 'extra_tossups'),
            ('bs', 'bonuses', qset.tiebreak_bonuses_per_packet, qset.extra_bonuses,
             'tiebreak_bonuses', 'extra_bonuses')):
        weights = [float(getattr(l, attr) or 0) for l in lines]
        in_packets_total = int(round(sum(weights) * n))
        in_packets = largest_remainder(in_packets_total, weights)
        tb_fixed = [getattr(l, tb_attr, None) for l in lines]
        tiebreak = split((tb_pp or 0) * n, tb_fixed, weights)
        x_fixed = [getattr(l, x_attr) for l in lines]
        extra = split(extras or 0, x_fixed, weights)

        for i, line in enumerate(lines):
            out[line.id][key] = {
                'packets': in_packets[i], 'tiebreak': tiebreak[i], 'extras': extra[i],
                'total': in_packets[i] + tiebreak[i] + extra[i],
                'tiebreak_edited': tb_fixed[i] is not None,
                'extras_edited': x_fixed[i] is not None,
            }
        summary[key] = {
            'per_packet': sum(Decimal(str(getattr(l, attr) or 0)) for l in lines),
            'packets': sum(in_packets), 'tiebreak': sum(tiebreak),
            'tiebreak_asked': (tb_pp or 0) * n,
            'extras': sum(extra), 'extras_asked': extras or 0,
            'total': sum(in_packets) + sum(tiebreak) + sum(extra),
        }
    out['set'] = summary
    return out


def _allocate_line(line, total, attr, entries):
    """Split one line's total for one type over its categories' entries.

    The breakdown's counts are weights: an entry under a breakdown row gets
    that row's share (all of it if the entry *is* that path, an even split if
    the row covers several entries); entries under no row share whatever of
    the line the breakdown leaves over, preferring the category's own
    top-level entry when it has one.
    """
    if not entries:
        return {}
    all_items = list(line.items.all())
    parents = item_parents([it.path for it in all_items])
    # A row with rows under it keeps only what they leave over of its count:
    # 1.5 Auditory with 1 Classical under it is 0.5 for the rest of Auditory.
    under = {}
    for it, parent in zip(all_items, parents):
        if parent is not None and getattr(it, attr) is not None:
            under[parent] = under.get(parent, 0.0) + float(getattr(it, attr))
    item_parts = []
    for k, it in enumerate(all_items):
        if getattr(it, attr) is not None:
            item_parts.append((path_parts(it.path), max(0.0, float(getattr(it, attr)) - under.get(k, 0.0))))
    line_pp = float(getattr(line, attr) or 0)
    leftover = max(0.0, line_pp - sum(float(getattr(it, attr)) for it, parent in zip(all_items, parents)
                                      if parent is None and getattr(it, attr) is not None))

    def owner(parts):
        best = None
        for idx, (ip, _) in enumerate(item_parts):
            if ip and parts[:len(ip)] == tuple(ip):
                if best is None or len(ip) > len(item_parts[best][0]):
                    best = idx
        return best

    groups = {}
    for e in entries:
        groups.setdefault(owner(entry_parts(e)), []).append(e)

    weights = {}
    for idx, members in groups.items():
        if idx is None:
            share = leftover
            tops = [e for e in members if len(entry_parts(e)) == 1]
            takers = tops or members
        else:
            share = item_parts[idx][1]
            exact = [e for e in members if entry_parts(e) == item_parts[idx][0]]
            takers = exact or members
        for e in takers:
            weights[e.id] = weights.get(e.id, 0.0) + share / len(takers)
    # Breakdown rows naming a path with no entry under it lose their weight;
    # what is left is scaled up to the line's total below.
    ids = [e.id for e in entries]
    split = largest_remainder(total, [weights.get(i, 0.0) for i in ids])
    return dict(zip(ids, split))


def derive(qset):
    """Write the set totals, packetizer quotas and pool super-groups from the
    set's lines. Does nothing for a set that has not switched."""
    if not qset.per_packet_distribution:
        return
    lines = list(qset.distribution_lines.prefetch_related('items'))
    totals = line_totals(qset, lines)
    by_top = entries_by_top(qset)

    claimed = set()
    per_entry = {}
    for line in lines:
        entries = []
        for name in line.category_list():
            if name.lower() in claimed:
                continue
            claimed.add(name.lower())
            entries.extend(by_top.get(name.lower(), []))
        for key, attr in (('tu', 'tossups'), ('bs', 'bonuses')):
            alloc = _allocate_line(line, totals[line.id][key]['total'], attr, entries)
            for entry_id, value in alloc.items():
                per_entry.setdefault(entry_id, {'tu': 0, 'bs': 0})[key] = value

    with transaction.atomic():
        existing = {s.dist_entry_id: s for s in qset.setwidedistributionentry_set.all()}
        for e in set_entries(qset):
            want = per_entry.get(e.id, {'tu': 0, 'bs': 0})
            row = existing.get(e.id)
            if row is None:
                SetWideDistributionEntry.objects.create(
                    question_set=qset, dist_entry=e,
                    num_tossups=want['tu'], num_bonuses=want['bs'])
            elif (row.num_tossups, row.num_bonuses) != (want['tu'], want['bs']):
                row.num_tossups, row.num_bonuses = want['tu'], want['bs']
                row.save(update_fields=['num_tossups', 'num_bonuses'])

        quotas = []
        for line in lines:
            if line.is_pool:
                continue
            for name in line.category_list()[:1]:
                top = _spelling(by_top, name)
                quotas.append(PacketizationEntry(
                    question_set=qset, path=top, depth=0,
                    min_tossups=line.tossups, max_tossups=line.tossups,
                    min_bonuses=line.bonuses, max_bonuses=line.bonuses))
                for item in line.items.all():
                    parts = path_parts(item.path)
                    if len(parts) < 2:
                        continue
                    quotas.append(PacketizationEntry(
                        question_set=qset, path=' - '.join(parts), depth=len(parts) - 1,
                        max_tossups=item.tossups, max_bonuses=item.bonuses))
        PacketizationEntry.objects.filter(question_set=qset).delete()
        PacketizationEntry.objects.bulk_create(quotas)

        pools = [l for l in lines if l.is_pool]
        keep = set()
        for i, line in enumerate(pools):
            group = CategorySuperGroup.objects.filter(question_set=qset, name=line.name[:100]).first()
            if group is not None and not group.from_distribution:
                # An editor's own group already has this name; leave it alone.
                keep.add(group.id)
                continue
            if group is None:
                group = CategorySuperGroup(question_set=qset, name=line.name[:100],
                                           from_distribution=True)
            group.set_category_list([_spelling(by_top, c) for c in line.category_list()])
            group.sort_order = 1000 + i
            group.save()
            keep.add(group.id)
        (CategorySuperGroup.objects.filter(question_set=qset, from_distribution=True)
         .exclude(id__in=keep).delete())


def _spelling(by_top, name):
    entries = by_top.get((name or '').lower())
    if entries:
        return entry_parts(entries[0])[0]
    return name


def unbudgeted(qset):
    """Top-level categories no line asks for: [(name, questions filed)]."""
    covered = set()
    for line in qset.distribution_lines.all():
        covered.update(c.lower() for c in line.category_list())
    counts = {}
    for model in (Tossup, Bonus):
        for cat_id, n in (model.objects.filter(question_set=qset)
                          .values_list('category_id').annotate(n=Count('id'))
                          .values_list('category_id', 'n')):
            counts[cat_id] = counts.get(cat_id, 0) + n
    out = {}
    for e in set_entries(qset):
        top = entry_parts(e)[:1]
        if not top or top[0].lower() in covered:
            continue
        out[top[0]] = out.get(top[0], 0) + counts.get(e.id, 0)
    return sorted(out.items(), key=lambda kv: (-kv[1], kv[0].lower()))


def written_by_line(qset, lines):
    """{line.id: (tossups written, bonuses written)} in the line's categories."""
    by_top = entries_by_top(qset)
    tu = dict(qset.tossup_set.values_list('category_id').annotate(n=Count('id'))
              .values_list('category_id', 'n'))
    bs = dict(qset.bonus_set.values_list('category_id').annotate(n=Count('id'))
              .values_list('category_id', 'n'))
    out = {}
    for line in lines:
        ids = [e.id for name in line.category_list() for e in by_top.get(name.lower(), [])]
        out[line.id] = (sum(tu.get(i, 0) for i in ids), sum(bs.get(i, 0) for i in ids))
    return out


# --------------------------------------------------------------------------
# Switching an existing set, and going back
# --------------------------------------------------------------------------

def snapshot_legacy(qset):
    """Everything the switch is about to start writing for the set, by path,
    so `restore_legacy` can put it back even after the set has been given
    its own copy of the distribution."""
    data = {
        'set_totals': {entry_path(s.dist_entry): [s.num_tossups, s.num_bonuses]
                       for s in qset.setwidedistributionentry_set.select_related('dist_entry')},
        'tiebreak': {entry_path(t.dist_entry): [t.num_tossups, t.num_bonuses]
                     for t in qset.tiebreakdistributionentry_set.select_related('dist_entry')},
        'quotas': [{'path': p.path, 'depth': p.depth,
                    'min_tu': fmt(p.min_tossups) if p.min_tossups is not None else None,
                    'max_tu': fmt(p.max_tossups) if p.max_tossups is not None else None,
                    'min_bs': fmt(p.min_bonuses) if p.min_bonuses is not None else None,
                    'max_bs': fmt(p.max_bonuses) if p.max_bonuses is not None else None}
                   for p in PacketizationEntry.objects.filter(question_set=qset)],
    }
    return json.dumps(data)


def restore_legacy(qset):
    """Go back to the set totals and quotas the set had before it switched.
    The lines stay, so switching again starts from where you left off."""
    if not qset.legacy_requirements:
        return False
    data = json.loads(qset.legacy_requirements)
    totals = data.get('set_totals', {})
    tiebreak = data.get('tiebreak', {})

    def dec(v):
        return None if v in (None, '') else Decimal(v)

    with transaction.atomic():
        for row in qset.setwidedistributionentry_set.select_related('dist_entry'):
            tu, bs = totals.get(entry_path(row.dist_entry), [0, 0])
            row.num_tossups, row.num_bonuses = tu or 0, bs or 0
            row.save(update_fields=['num_tossups', 'num_bonuses'])
        for row in qset.tiebreakdistributionentry_set.select_related('dist_entry'):
            path = entry_path(row.dist_entry)
            if path in tiebreak:
                row.num_tossups, row.num_bonuses = tiebreak[path]
                row.save(update_fields=['num_tossups', 'num_bonuses'])
        PacketizationEntry.objects.filter(question_set=qset).delete()
        PacketizationEntry.objects.bulk_create([
            PacketizationEntry(question_set=qset, path=q['path'], depth=q['depth'],
                               min_tossups=dec(q['min_tu']), max_tossups=dec(q['max_tu']),
                               min_bonuses=dec(q['min_bs']), max_bonuses=dec(q['max_bs']))
            for q in data.get('quotas', [])])
        CategorySuperGroup.objects.filter(question_set=qset, from_distribution=True).delete()
        qset.per_packet_distribution = False
        qset.legacy_requirements = ''
        qset.save(update_fields=['per_packet_distribution', 'legacy_requirements'])
    return True


def _quarter(x):
    """Nearest quarter: 5 / 12 packets is about 0.5, i.e. one every two."""
    return (Decimal(str(x)) * 4).quantize(Decimal('1'), rounding=ROUND_HALF_UP) / 4


def _half(x):
    return (Decimal(str(x)) * 2).quantize(Decimal('1'), rounding=ROUND_HALF_UP) / 2


def propose_lines(qset):
    """What the set's distribution would look like per packet, worked out
    from what it already stores. One row per proposed line, plus the facts
    the review page shows beside it.

    In order of preference a line's count comes from the packetizer quota
    (which already says "per packet"), else the set total divided by the
    packet count -- exact when it divides, otherwise rounded to the nearest
    half and flagged for a decision. A super-group becomes a pool. A
    category with questions but no requirement is a gap: unbudgeted unless
    the owner adds it.
    """
    n = max(qset.num_packets or 0, 1)
    quotas = {p.path: p for p in PacketizationEntry.objects.filter(question_set=qset)}
    tops = {}
    for row in qset.setwidedistributionentry_set.select_related('dist_entry'):
        parts = entry_parts(row.dist_entry)
        if not parts:
            continue
        node = tops.setdefault(parts[0], {'tu': 0, 'bs': 0, 'subs': {}})
        node['tu'] += row.num_tossups or 0
        node['bs'] += row.num_bonuses or 0
        if len(parts) > 1:
            sub = node['subs'].setdefault(' - '.join(parts[:2]), [0, 0])
            sub[0] += row.num_tossups or 0
            sub[1] += row.num_bonuses or 0
    for e in set_entries(qset):
        parts = entry_parts(e)
        if parts:
            tops.setdefault(parts[0], {'tu': 0, 'bs': 0, 'subs': {}})

    written = {}
    for model in (Tossup, Bonus):
        for cat, n_q in (model.objects.filter(question_set=qset)
                         .values_list('category__category').annotate(c=Count('id'))
                         .values_list('category__category', 'c')):
            if cat:
                written[cat] = written.get(cat, 0) + n_q
    tiebreak = {}
    for t in qset.tiebreakdistributionentry_set.select_related('dist_entry'):
        top = entry_parts(t.dist_entry)[:1]
        if top:
            agg = tiebreak.setdefault(top[0], [0, 0])
            agg[0] += t.num_tossups or 0
            agg[1] += t.num_bonuses or 0

    def one(top):
        node = tops[top]
        tu_avg, bs_avg = node['tu'] / float(n), node['bs'] / float(n)
        from_totals = '{0} / {1} → {2} / {3}'.format(
            node['tu'], node['bs'], _short(tu_avg), _short(bs_avg))
        q = quotas.get(top)
        items = []
        for sub, (stu, sbs) in sorted(node['subs'].items()):
            sq = quotas.get(sub)
            itu = sq.max_tossups if sq is not None and sq.max_tossups is not None else _quarter(stu / float(n))
            ibs = sq.max_bonuses if sq is not None and sq.max_bonuses is not None else _quarter(sbs / float(n))
            if itu or ibs:
                # The unrounded shares too, so a line whose rounded rows
                # come to more than it holds can be shared out fairly.
                items.append({'path': sub, 'tu': itu, 'bs': ibs,
                              'w_tu': stu / float(n), 'w_bs': sbs / float(n)})
        if len(items) == 1:
            items = []          # one subcategory is the whole line
        # The packetize page fills each quota in with the set total divided
        # by the packets, to one decimal. A quota still holding that is a
        # rounding of the totals, not a decision anyone made, so the totals
        # are the better source.
        prefilled = (q is not None and q.min_tossups is not None and q.min_bonuses is not None
                     and abs(float(q.min_tossups) - round(tu_avg, 1)) < 1e-6
                     and abs(float(q.min_bonuses) - round(bs_avg, 1)) < 1e-6)
        if q is not None and q.min_tossups is not None and q.min_bonuses is not None and not prefilled:
            tu, bs = q.min_tossups, q.min_bonuses
            same = abs(float(tu) - tu_avg) < 0.01 and abs(float(bs) - bs_avg) < 0.01
            return {'kind': 'same' if same else 'quota', 'tu': tu, 'bs': bs,
                    'from_totals': from_totals, 'from_quota': pair(q.min_tossups, q.min_bonuses),
                    'items': items,
                    'why': 'Divides evenly; nothing to decide.' if same else
                           'The packetizer quota already says {0} per packet, so it wins over the set '
                           'total ({1} / {2} across {3} packets).'.format(
                               pair(tu, bs), node['tu'], node['bs'], n)}
        exact_tu, exact_bs = _quarter(tu_avg), _quarter(bs_avg)
        exact = (float(exact_tu) * n == node['tu'] and float(exact_bs) * n == node['bs'])
        if exact:
            return {'kind': 'same', 'tu': exact_tu, 'bs': exact_bs, 'from_totals': from_totals,
                    'from_quota': '—', 'items': items,
                    'why': 'Divides evenly; nothing to decide.'}
        tu, bs = exact_tu, exact_bs
        return {'kind': 'decide', 'tu': tu, 'bs': bs, 'from_totals': from_totals,
                'from_quota': pair(q.min_tossups, q.min_bonuses) + ' (pre-filled)' if prefilled else '—',
                'items': items,
                'why': '{0} / {1} do not divide into {2} packets. {3} per packet, the nearest quarter, '
                       'asks for {4} / {5}; change the count to choose differently.'.format(
                           node['tu'], node['bs'], n, pair(tu, bs),
                           int(round(float(tu) * n)), int(round(float(bs) * n)))}

    rows, pooled = [], set()
    for group in qset.super_groups.filter(from_distribution=False):
        members = [m for m in group.category_list() if m in tops]
        if not members:
            continue
        pooled.update(members)
        parts = [one(m) for m in members]
        tu = sum((p['tu'] for p in parts), ZERO)
        bs = sum((p['bs'] for p in parts), ZERO)
        rows.append({'kind': 'pool', 'name': group.name, 'categories': members,
                     'tu': tu, 'bs': bs,
                     'from_totals': ' + '.join('{0} / {1}'.format(tops[m]['tu'], tops[m]['bs']) for m in members),
                     'from_quota': '—',
                     'items': [{'path': m, 'tu': p['tu'], 'bs': p['bs']} for m, p in zip(members, parts)],
                     'why': 'These were a super-group on the category overview, so they become one '
                            'line that shares its count; their own numbers become its rough split.'})
    for top in sorted(tops, key=str.lower):
        if top in pooled:
            continue
        node = tops[top]
        if node['tu'] == 0 and node['bs'] == 0:
            if written.get(top):
                rows.append({'kind': 'gap', 'name': top, 'categories': [top], 'tu': ZERO, 'bs': ZERO,
                             'from_totals': '0 / 0', 'from_quota': '—', 'items': [],
                             'why': '{0} question(s) are filed here but nothing asks for them. Tick it to '
                                    'add it as a line, or leave it unbudgeted.'.format(written[top])})
            continue
        row = one(top)
        row.update({'name': top, 'categories': [top]})
        rows.append(row)
    rows.sort(key=lambda r: (r['kind'] == 'gap', r['name'].lower()))
    for i, r in enumerate(rows):
        r['index'] = i
        r['include'] = r['kind'] != 'gap'
        # What the page needs to show the change as you type.
        r['old_tu'] = sum(tops[c]['tu'] for c in r['categories'] if c in tops)
        r['old_bs'] = sum(tops[c]['bs'] for c in r['categories'] if c in tops)
        if r['kind'] == 'quota':
            r['note'] = 'From its packetizer quota'
        elif r['kind'] == 'decide':
            r['note'] = 'Doesn’t divide evenly: {0} / {1} a packet'.format(
                _short(r['old_tu'] / float(n)), _short(r['old_bs'] / float(n)))
        elif r['kind'] == 'pool':
            r['note'] = 'Shared by ' + ', '.join(r['categories'])
        elif r['kind'] == 'gap':
            r['note'] = '{0} question{1} filed, nothing asked for'.format(
                written.get(r['name'], 0), '' if written.get(r['name'], 0) == 1 else 's')
        else:
            r['note'] = ''
    tb_tu = sum(v[0] for v in tiebreak.values())
    # Every category gets a 1 / 1 tiebreak row when a set is made, and the
    # old tiebreak tab never worked, so rows still at 1 / 1 are that default
    # rather than anyone's decision -- not worth offering as extras.
    tiebreak_edited = any(
        (t.num_tossups, t.num_bonuses) != (1, 1)
        for t in qset.tiebreakdistributionentry_set.all())
    tb_bs = sum(v[1] for v in tiebreak.values())
    kept = [r for r in rows if r['include']]
    sum_tu = sum((r['tu'] for r in kept), ZERO)
    sum_bs = sum((r['bs'] for r in kept), ZERO)
    old_tu = sum(t['tu'] for t in tops.values())
    old_bs = sum(t['bs'] for t in tops.values())
    return {'rows': rows, 'tiebreak': tiebreak, 'tiebreak_tu': tb_tu, 'tiebreak_bs': tb_bs,
            'num_packets': n, 'sum': pair(sum_tu, sum_bs), 'tiebreak_edited': tiebreak_edited,
            'size': pair(qset.tossups_per_packet, qset.bonuses_per_packet),
            'size_ok': (sum_tu == qset.tossups_per_packet
                        and (qset.tossups_only or sum_bs == qset.bonuses_per_packet)),
            'old_totals': '{0} / {1}'.format(old_tu, old_bs),
            'old_per_packet': pair(_quarter(old_tu / float(n)), _quarter(old_bs / float(n))),
            'decide': sum(1 for r in rows if r['kind'] in ('decide', 'gap'))}


def _short(x):
    return fmt(Decimal(str(round(x, 2))))


def apply_lines(qset, specs, replace=True):
    """Save lines from plain dicts ({'name', 'categories', 'is_pool', 'tu',
    'bs', 'extra_tu', 'extra_bs', 'child_max', 'links_auto', 'groups_differ',
    'items': [{'path', 'tu', 'bs', 'link', 'child_max'}]}). Makes sure the set
    owns its distribution and that every category a line names exists in it,
    then rewrites everything derived. Links are worked out from the counts
    unless the spec says they were set by hand."""
    give_set_own_distribution(qset)
    qset.refresh_from_db()
    with transaction.atomic():
        if replace:
            qset.distribution_lines.all().delete()
        start = qset.distribution_lines.count()
        for i, spec in enumerate(specs):
            names = [c for c in spec.get('categories', []) if c and c.strip()]
            # Breakdown rows first: a category with subcategories needs no
            # entry of its own, and making one first would leave a bare
            # top-level category beside them.
            for item in spec.get('items', []):
                if path_parts(item.get('path')):
                    ensure_entry(qset, item['path'])
            for name in names:
                ensure_entry(qset, name)
            line = DistributionLine(
                question_set=qset, name=(spec.get('name') or (names[0] if names else 'Line'))[:200],
                is_pool=bool(spec.get('is_pool')), tossups=spec.get('tu') or ZERO,
                bonuses=spec.get('bs') or ZERO, rule=spec.get('rule', '') or '',
                extra_tossups=spec.get('extra_tu'), extra_bonuses=spec.get('extra_bs'),
                tiebreak_tossups=spec.get('tb_tu'), tiebreak_bonuses=spec.get('tb_bs'),
                child_max=spec.get('child_max'), links_auto=spec.get('links_auto', True) is not False,
                groups_differ=bool(spec.get('groups_differ')), sort_order=start + i)
            line.set_category_list(names)
            line.save()
            items = [dict(it, path=' - '.join(path_parts(it.get('path'))))
                     for it in spec.get('items', []) if path_parts(it.get('path'))]
            links = resolve_links(items, line.links_auto)
            for j, (item, link) in enumerate(zip(items, links)):
                DistributionLineItem.objects.create(
                    line=line, path=item['path'], tossups=item.get('tu'), bonuses=item.get('bs'),
                    link=link, child_max=item.get('child_max'), sort_order=j)
            line.link = spec.get('link') or None
            line.save(update_fields=['link'])
        lines = list(qset.distribution_lines.all())
        for line, link in zip(lines, top_links(qset, lines)):
            if line.link != link:
                line.link = link
                line.save(update_fields=['link'])
    qset.refresh_from_db()
    derive(qset)


def switch_set(qset, specs, extras=(0, 0), extra_overrides=None):
    """Put an existing set on the per-packet distribution. What it had is
    kept for undo; nothing is deleted, every question keeps its category,
    packet and number."""
    if qset.per_packet_distribution:
        return
    with transaction.atomic():
        qset.legacy_requirements = snapshot_legacy(qset)
        qset.per_packet_distribution = True
        qset.extra_tossups, qset.extra_bonuses = extras
        qset.save(update_fields=['legacy_requirements', 'per_packet_distribution',
                                 'extra_tossups', 'extra_bonuses'])
        for spec in specs:
            if extra_overrides is not None:
                for top in spec.get('categories', []):
                    if top in extra_overrides:
                        spec['extra_tu'] = (spec.get('extra_tu') or 0) + extra_overrides[top][0]
                        spec['extra_bs'] = (spec.get('extra_bs') or 0) + extra_overrides[top][1]
        apply_lines(qset, specs)


def specs_from_proposal(proposal, chosen=None):
    """Turn proposal rows into line specs. `chosen` overrides by index:
    {index: {'include': bool, 'tu': Decimal, 'bs': Decimal}}."""
    specs = []
    for row in proposal['rows']:
        pick = (chosen or {}).get(row['index'], {})
        if not pick.get('include', row['include']):
            continue
        tu, bs = pick.get('tu', row['tu']), pick.get('bs', row['bs'])
        specs.append({'name': row['name'], 'categories': row['categories'],
                      'is_pool': row['kind'] == 'pool', 'tu': tu, 'bs': bs,
                      'items': _fit_items(row['items'], tu, bs)})
    return specs


def _fit_items(items, tu, bs):
    """Rows worked out from rounded totals can add up to more than the count
    chosen for their category (four quarter-rounded shares of 4.2 make 4.25),
    which the Distribution page refuses to save. Such rows share the
    category's count out again instead, in quarters and in proportion to what
    they had, so they add up to it exactly: 27 / 25 / 25 / 25 over 24
    packets of 4 Literature makes 1 each, not 1.25 / 1 / 1 / 1."""
    items = [dict(it) for it in items]
    for key, limit in (('tu', tu), ('bs', bs)):
        if sum((it[key] or ZERO for it in items), ZERO) > (limit or ZERO):
            quarters = int(math.floor(float(limit or 0) * 4 + 1e-9))
            weights = [it.get('w_' + key, float(it[key] or 0)) for it in items]
            for it, share in zip(items, largest_remainder(quarters, weights)):
                it[key] = Decimal(share) / 4
    return items


def lines_from_distribution(dist):
    """A template distribution's per-packet minimums as line specs, one line
    per top-level category with its subcategories as the breakdown."""
    tops = {}
    for e in dist.distributionentry_set.all().order_by('category', 'subcategory'):
        parts = entry_parts(e)
        if not parts:
            continue
        node = tops.setdefault(parts[0], {'tu': ZERO, 'bs': ZERO, 'items': []})
        tu = Decimal(e.min_tossups or 0)
        bs = Decimal(e.min_bonuses or 0)
        node['tu'] += tu
        node['bs'] += bs
        if len(parts) > 1 and (tu or bs):
            node['items'].append({'path': ' - '.join(parts), 'tu': tu, 'bs': bs})
    specs = []
    for top in sorted(tops, key=str.lower):
        node = tops[top]
        specs.append({'name': top, 'categories': [top], 'tu': node['tu'], 'bs': node['bs'],
                      'items': node['items'] if len(node['items']) > 1 else []})
    return specs


def specs_from_set(qset):
    """The lines of a set as specs (for copying), working them out from its
    totals first if it has not switched."""
    if qset.per_packet_distribution and qset.distribution_lines.exists():
        return [{'name': l.name, 'categories': l.category_list(), 'is_pool': l.is_pool,
                 'tu': l.tossups, 'bs': l.bonuses, 'rule': l.rule,
                 'extra_tu': l.extra_tossups, 'extra_bs': l.extra_bonuses,
                 'tb_tu': l.tiebreak_tossups, 'tb_bs': l.tiebreak_bonuses,
                 'child_max': l.child_max, 'links_auto': l.links_auto, 'groups_differ': l.groups_differ,
                 'link': l.link,
                 'items': [{'path': it.path, 'tu': it.tossups, 'bs': it.bonuses,
                            'link': it.link, 'child_max': it.child_max}
                           for it in l.items.all()]}
                for l in qset.distribution_lines.prefetch_related('items')]
    return specs_from_proposal(propose_lines(qset))


# --------------------------------------------------------------------------
# Tags
# --------------------------------------------------------------------------

def _scale(value, factor):
    if value is None:
        return None
    if not value:
        return 0
    return max(1, int(round(value * factor)))


def copy_tags(dst, src, scale=True):
    """Copy `src`'s category tags (not which questions carry them) onto `dst`,
    quotas scaled from src's packet count to dst's unless `scale` is off.
    A tag already on dst with the same name and category is left alone.
    Returns how many were added."""
    factor = 1.0
    if scale and src.num_packets and dst.num_packets:
        factor = dst.num_packets / float(src.num_packets)
    have = set(CategoryTag.objects.filter(question_set=dst)
               .values_list('category_path', 'name'))
    added = 0
    for t in CategoryTag.objects.filter(question_set=src):
        if (t.category_path, t.name) in have:
            continue
        CategoryTag.objects.create(
            question_set=dst, category_path=t.category_path, name=t.name,
            group_name=t.group_name, sort_order=t.sort_order, show_in_output=t.show_in_output,
            num_tossups=_scale(t.num_tossups, factor), num_bonuses=_scale(t.num_bonuses, factor),
            num_questions=_scale(t.num_questions, factor),
            max_tossups=_scale(t.max_tossups, factor), max_bonuses=_scale(t.max_bonuses, factor),
            max_questions=_scale(t.max_questions, factor))
        added += 1
    return added


def tag_rows_by_path(qset):
    """{category path ('' for whole-set): [(tag, progress)]}."""
    out = {}
    for tag in CategoryTag.objects.filter(question_set=qset):
        out.setdefault(tag.category_path or '', []).append((tag, tag.progress()))
    return out


def copy_from_set(new_qset, src, scale_tags=True, copy_extras=False):
    """Start `new_qset` from `src`: a copy of its categories, its lines
    (worked out from its totals if it has not switched), its tags with their
    quotas scaled to the new set's size, and optionally its tiebreakers and
    extras. The two sets are independent afterwards."""
    specs = specs_from_set(src)
    copy_distribution_for(new_qset, src.distribution)
    new_qset.per_packet_distribution = True
    new_qset.top_links_auto = src.top_links_auto if src.per_packet_distribution else True
    if copy_extras:
        new_qset.tiebreak_tossups_per_packet = src.tiebreak_tossups_per_packet
        new_qset.tiebreak_bonuses_per_packet = src.tiebreak_bonuses_per_packet
        new_qset.extra_tossups = src.extra_tossups
        new_qset.extra_bonuses = src.extra_bonuses
    else:
        for spec in specs:
            spec['extra_tu'] = spec['extra_bs'] = None
            spec['tb_tu'] = spec['tb_bs'] = None
    new_qset.save()
    apply_lines(new_qset, specs)
    return copy_tags(new_qset, src, scale=scale_tags)


# --------------------------------------------------------------------------
# Pasting the announcement
# --------------------------------------------------------------------------

LINE_RE = re.compile(
    r'^(?P<indent>\s*)(?P<bullet>[-*•–—]\s*)?'
    r'(?P<tu>\d+(?:[.,]\d+)?|½)\s*(?:/\s*(?P<bs>\d+(?:[.,]\d+)?|½))?\s+'
    r'(?P<name>.+?)\s*$')

# A person's name, as subject editors are given in brackets: two to four
# capitalised words ("Emily Bussa", "Michael Zhou"), optionally followed by
# their school in parentheses.
PERSON_RE = re.compile(r"^[A-Z][\w.'’-]*(?:\s+[A-Z][\w.'’-]*){1,3}(?:\s*\(.*\))?$")


def _trailing_bracket(text):
    """(before, inside, opener) for a trailing [...] or (...) group, nested
    brackets allowed ("[Michael Zhou (formerly GT)]"), or None."""
    text = text.rstrip()
    if not text or text[-1] not in ')]':
        return None
    close = text[-1]
    opener = '(' if close == ')' else '['
    depth = 0
    for i in range(len(text) - 1, -1, -1):
        ch = text[i]
        if ch in ')]':
            depth += 1
        elif ch in '([':
            depth -= 1
            if depth == 0:
                return (text[:i].rstrip(), text[i + 1:-1].strip(), ch) if ch == opener else None
    return None


def split_name(raw):
    """(name, editor, note) from what follows a line's count.

    Square brackets always hold the subject editor. Parentheses hold one only
    when they read as a person's name ("(Emily Bussa)"); otherwise they
    describe the category ("Americas (Canada, Mexico, ...)") and are kept
    out of its name as a note."""
    name, editor, note = raw.strip(), '', ''
    for _ in range(2):          # an editor and a description may both trail
        found = _trailing_bracket(name)
        if not found:
            break
        before, inside, opener = found
        if not before:
            break
        if opener == '[' or (not editor and PERSON_RE.match(inside)):
            if editor:
                break
            editor = inside
        else:
            note = inside
        name = before
    return name.strip(' :'), editor, note


def catalog(qset=None):
    """Known categories to match pasted names against: {top: set(first
    subcategory segments)}. The set's own, then everything else."""
    out = {}
    entries = DistributionEntry.objects.values_list('category', 'subcategory').distinct()
    if qset is not None:
        own = list(qset.distribution.distributionentry_set.values_list('category', 'subcategory'))
        entries = own + [e for e in entries if e not in own]
    for cat, sub in entries:
        if not cat or not cat.strip():
            continue
        subs = out.setdefault(cat.strip(), set())
        first = (sub or '').split(' - ')[0].strip()
        if first:
            subs.add(first)
    return out


def _match(name, choices, cutoff=0.8):
    """('match'|'close'|None, choice)."""
    n = _norm(name)
    if not n:
        return None, None
    by_norm = {}
    for c in choices:
        by_norm.setdefault(_norm(c), c)
    if n in by_norm:
        return 'match', by_norm[n]
    # A name that contains a known one whole ("Classical Music" -> "Music",
    # "Math and Computer Science" -> "Math") or is contained by one.
    # A known name inside the pasted one counts; the pasted name inside a
    # known one only when that is barely longer, or "Other Academic" lands on
    # some "Geography, Current Events, Other Academic".
    words = set(n.split())
    contained = [c for k, c in by_norm.items()
                 if k and (set(k.split()) <= words
                           or (words <= set(k.split()) and len(set(k.split())) - len(words) <= 1))]
    if len(contained) == 1:
        return 'close', contained[0]
    close = difflib.get_close_matches(n, list(by_norm), n=1, cutoff=cutoff)
    if close:
        return 'close', by_norm[close[0]]
    return None, None


def _strip_heading(child, heading):
    """"American Literature" under Literature -> "American"."""
    hw = set(_norm(heading).split())
    kept = [w for w in re.split(r'\s+', child.strip()) if _norm(w) not in hw]
    stripped = ' '.join(kept).strip(' -/')
    return stripped or child.strip()


def parse_announcement(text, known, packet_size=None):
    """Read a pasted set announcement into blocks.

    Each block is a category ("4/4 Literature") and the indented or bulleted
    lines under it, its sub-categories. Counts may be fractions and a name in
    brackets is that line's subject editor.
    """
    tops = list(known)
    raw_lines = []
    unread = []
    para = 0
    for raw in (text or '').splitlines():
        if not raw.strip():
            para += 1               # a blank line ends a block
            continue
        m = LINE_RE.match(raw.replace('\t', '    '))
        if not m:
            unread.append(raw.strip())
            continue
        try:
            tu = to_decimal(m.group('tu'))
            bs = to_decimal(m.group('bs')) if m.group('bs') else tu
        except ValueError:
            unread.append(raw.strip())
            continue
        name, editor, note = split_name(m.group('name'))
        raw_lines.append({'tu': tu, 'bs': bs, 'name': name, 'editor': editor, 'note': note,
                          'nested': bool(m.group('indent')) or bool(m.group('bullet')),
                          'colon': m.group('name').rstrip().endswith(':'),
                          'para': para, 'text': raw.strip()})

    # Structure comes only from what the text shows, never from names:
    #  - an indented or bulleted line belongs to the line above it;
    #  - a category ending in a colon ("4 History:") takes the lines after it,
    #    up to the next blank line or colon heading;
    #  - the first line of a block (lines between blank lines) takes the rest
    #    of the block when there are at least two of them and they add up to
    #    exactly its count -- how set announcements are written ("4/4
    #    History", its four 1/1 lines, a blank line, "4/4 Science"...).
    # Anything else is a category of its own. A flat list whose lines don't
    # add up to the first one ("4 United States", "1 Canada"...) stays flat.
    blocks = []
    orphans = []
    # The block rule needs blocks: a paste with no blank lines between its
    # counted lines is one list, and its structure has to be shown by
    # indenting or a colon.
    blocked = len({ln['para'] for ln in raw_lines}) > 1
    i = 0
    while i < len(raw_lines):
        ln = raw_lines[i]
        if ln['nested']:
            if blocks:
                blocks[-1]['children'].append(ln)
            else:
                orphans.append(ln['text'])      # indented with nothing above it
                blocks.append(dict(ln, children=[]))
            i += 1
            continue
        head = dict(ln, children=[])
        blocks.append(head)
        i += 1
        rest = []
        j = i
        while (j < len(raw_lines) and raw_lines[j]['para'] == ln['para']
               and not raw_lines[j]['nested'] and not raw_lines[j]['colon']):
            rest.append(raw_lines[j])
            j += 1
        if ln['colon'] and rest:
            take = rest
        elif blocked and len(rest) >= 2 and (sum((r['tu'] for r in rest), ZERO),
                                 sum((r['bs'] for r in rest), ZERO)) == (ln['tu'], ln['bs']):
            take = rest
        else:
            take = []
        head['children'] = take
        i += len(take)

    out = []
    for b in blocks:
        kind, top = _match(b['name'], tops)
        block = {'pasted': b['name'], 'tu': b['tu'], 'bs': b['bs'], 'editor': b['editor'],
                 'description': b.get('note', ''), 'children': [], 'notes': []}
        others = [t for t in tops if t != top]
        child_tops = [_match(c['name'], others) for c in b['children']]
        if kind is None:
            # An unknown heading over known categories ("Thought": Philosophy,
            # Social Science) is a pool.
            pool = sum(1 for k, _ in child_tops if k) * 2 > len(b['children'])
        else:
            # A known heading is a pool when most of its lines are other
            # categories by name, and more of them are than are its own
            # subcategories ("Other": Geography, Current Events, Other
            # Academic).
            subs = known.get(top, ())
            own = sum(1 for c in b['children'] if _match(_strip_heading(c['name'], b['name']), subs)[0])
            named = sum(1 for k, _ in child_tops if k == 'match')
            pool = named > own and named * 2 > len(b['children'])
        if b['children'] and pool:
            block.update({'kind': 'pool', 'name': b['name'], 'status': 'pool',
                          'becomes': 'Combined: the categories indented under “{0}” share '
                                     'its count'.format(b['name'])})
            members = []
            for c, (ck, ctop) in zip(b['children'], child_tops):
                path = ctop or c['name']
                block['children'].append({'pasted': c['name'], 'tu': c['tu'], 'bs': c['bs'],
                                          'editor': c['editor'], 'path': path,
                                          'status': ck or 'new',
                                          'becomes': path if ck else '{0} (a new category)'.format(path)})
                members.append(path)
            block['categories'] = members
        else:
            name = top if kind else b['name']
            block.update({'kind': 'line', 'name': name, 'categories': [name],
                          'status': kind or 'new',
                          'becomes': name if kind else '{0} (a new category)'.format(name)})
            subs = known.get(top, set()) if kind else set()
            for c in b['children']:
                bare = _strip_heading(c['name'], b['name'])
                ck, sub = _match(bare, subs) if subs else (None, None)
                if ck is None and subs:
                    ck, sub = _match(c['name'], subs)
                sub = sub or bare
                path = '{0} - {1}'.format(name, sub)
                block['children'].append({'pasted': c['name'], 'tu': c['tu'], 'bs': c['bs'],
                                          'editor': c['editor'], 'path': path,
                                          'status': ck or 'new',
                                          'becomes': path if ck else '{0} (a new subcategory)'.format(path)})
            paths = [ch['path'] for ch in block['children']]
            for p in set(paths):
                if paths.count(p) > 1:
                    block['notes'].append('Two lines land on {0}; their counts are added together. '
                                          'Rename one to keep them apart.'.format(p))
        if b['children']:
            ctu = sum((c['tu'] for c in b['children']), ZERO)
            cbs = sum((c['bs'] for c in b['children']), ZERO)
            if ctu != b['tu'] or cbs != b['bs']:
                block['notes'].append('Its lines add up to {0}, not {1}.'.format(
                    pair(ctu, cbs), pair(b['tu'], b['bs'])))
        out.append(block)

    total_tu = sum((b['tu'] for b in out), ZERO)
    total_bs = sum((b['bs'] for b in out), ZERO)
    counts = {'match': 0, 'close': 0, 'new': 0, 'pool': 0}
    for b in out:
        counts[b['status']] = counts.get(b['status'], 0) + 1
        for c in b['children']:
            counts[c['status']] = counts.get(c['status'], 0) + 1
    result = {'blocks': out, 'unread': unread, 'total_tu': total_tu, 'total_bs': total_bs,
              'counts': counts, 'mismatch': None,
              # No sub-categories found anywhere: every line is its own
              # category, right only if the announcement has no breakdowns.
              'flat': len(raw_lines) > 1 and not any(b['children'] for b in out),
              'orphans': orphans}
    if packet_size is not None and (total_tu, total_bs) != (Decimal(packet_size[0]), Decimal(packet_size[1])):
        result['mismatch'] = {'pasted': pair(total_tu, total_bs),
                              'set': pair(packet_size[0], packet_size[1]),
                              # Only a whole number of questions can be a
                              # packet size; 20.5 can't.
                              'whole': total_tu == int(total_tu) and total_bs == int(total_bs)}
    return result


def specs_from_parse(parsed):
    specs = []
    for b in parsed['blocks']:
        items = {}
        order = []
        for c in b['children']:
            if c['path'] not in items:
                items[c['path']] = {'path': c['path'], 'tu': ZERO, 'bs': ZERO}
                order.append(c['path'])
            items[c['path']]['tu'] += c['tu']
            items[c['path']]['bs'] += c['bs']
        specs.append({'name': b['name'], 'categories': b['categories'],
                      'is_pool': b['kind'] == 'pool', 'tu': b['tu'], 'bs': b['bs'],
                      'items': [items[p] for p in order]})
    return specs


def editors_from_parse(qset, parsed):
    """Tag the named subject editors who are on the set with their
    categories. Returns the names that matched nobody on the set."""
    members = {}
    for w in (list(qset.editor.all()) + list(qset.writer.all()) + qset.all_owners()):
        u = w.user
        for key in (u.get_full_name(), u.username):
            if key and key.strip():
                members.setdefault(_norm(key), w)
    missing = []
    for b in parsed['blocks']:
        targets = [(b.get('editor'), b['name'] if b['kind'] == 'line' else '')]
        targets += [(c['editor'], c['path']) for c in b['children']]
        for name, path in targets:
            if not name or not path:
                continue
            # "Michael Zhou (formerly GT)": the school isn't part of the name.
            writer = members.get(_norm(re.sub(r'\s*\(.*\)\s*$', '', name)))
            if writer is None:
                if name not in missing:
                    missing.append(name)
                continue
            EditorTag.objects.get_or_create(question_set=qset, editor=writer, category=path,
                                            defaults={'label': ''})
    return missing


# --------------------------------------------------------------------------
# The tree under a line: nested rows, links between siblings, and the
# "no sub-category more than N a packet" rule
# --------------------------------------------------------------------------

def _lower_parts(path):
    return tuple(p.lower() for p in path_parts(path))


def item_parents(paths):
    """For each row path, the index of the row it sits under -- the longest
    other row whose path starts its own -- or None for a row straight under
    the line."""
    keys = [_lower_parts(p) for p in paths]
    out = []
    for i, key in enumerate(keys):
        best = None
        for j, other in enumerate(keys):
            if j != i and other and len(other) < len(key) and key[:len(other)] == other:
                if best is None or len(other) > len(keys[best]):
                    best = j
        out.append(best)
    return out


def _dec(value):
    return Decimal(str(value)) if value not in (None, '') else ZERO


def _frac(value):
    value = _dec(value)
    return value - value.to_integral_value(rounding=ROUND_FLOOR)


def _whole(counts):
    return not _frac(sum((_dec(tu) for tu, _ in counts), ZERO)) and \
        not _frac(sum((_dec(bs) for _, bs in counts), ZERO))


def auto_links(counts):
    """Links for one set of siblings, from their (tu, bs) per packet: [link
    number or None]. Siblings whose counts aren't whole numbers are linked
    in the smallest sets that add up to whole numbers (0.5 Opera with 0.5
    Ballet; 0.25 with 0.75), so it is plain which of them share a slot. Any
    left over that make no whole number together share what is left of
    their parent's fraction, and are linked to each other."""
    out = [None] * len(counts)
    left = [i for i, (tu, bs) in enumerate(counts) if _frac(tu) or _frac(bs)]
    n = 0
    while len(left) >= 2:
        found = None
        if len(left) <= 16:
            for size in range(2, len(left) + 1):
                found = next((c for c in combinations(left, size)
                              if _whole([counts[i] for i in c])), None)
                if found:
                    break
        else:
            for k in range(2, len(left) + 1):
                if _whole([counts[i] for i in left[:k]]):
                    found = tuple(left[:k])
                    break
        if not found:
            break
        n += 1
        for i in found:
            out[i] = n
        left = [i for i in left if i not in found]
    if len(left) >= 2:
        n += 1
        for i in left:
            out[i] = n
    return out


def _get(item, key):
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, {'tu': 'tossups', 'bs': 'bonuses'}.get(key, key))


def siblings(items):
    """{parent index or None: [child indexes]} for a line's rows."""
    out = {}
    for i, parent in enumerate(item_parents([_get(it, 'path') for it in items])):
        out.setdefault(parent, []).append(i)
    return out


def resolve_links(items, auto=True):
    """The link number of every row: worked out from the counts when `auto`,
    else as each row has it. A link only one sibling carries links nothing
    and is dropped; links are renumbered 1, 2, ... in order within each set
    of siblings."""
    out = [None] * len(items)
    for parent, kids in siblings(items).items():
        if auto:
            links = auto_links([(_get(items[k], 'tu'), _get(items[k], 'bs')) for k in kids])
        else:
            links = [_get(items[k], 'link') or None for k in kids]
            used = Counter(l for l in links if l)
            links = [l if l and used[l] > 1 else None for l in links]
        renumber = {}
        for k, link in zip(kids, links):
            if link:
                out[k] = renumber.setdefault(link, len(renumber) + 1)
    return out


def line_links(line, items=None):
    """The links of a saved line's rows: worked out from their counts when
    the line's links are automatic (so a count changed anywhere moves them),
    else as saved."""
    items = list(line.items.all()) if items is None else items
    return resolve_links(items, True) if line.links_auto else [it.link for it in items]


def top_links(qset, lines=None):
    """The link number of each of the set's top-level lines: worked out from
    their counts when the set's links are automatic, else as saved. A link
    only one line carries links nothing."""
    lines = list(qset.distribution_lines.all()) if lines is None else lines
    if qset.top_links_auto:
        return auto_links([(l.tossups, l.bonuses) for l in lines])
    used = Counter(l.link for l in lines if l.link)
    return [l.link if l.link and used[l.link] > 1 else None for l in lines]


def top_link_sets(qset, lines=None):
    """[(link, [lines])] for every link between top-level lines."""
    lines = list(qset.distribution_lines.all()) if lines is None else lines
    out = {}
    for line, link in zip(lines, top_links(qset, lines)):
        if link:
            out.setdefault(link, []).append(line)
    return [(link, members) for link, members in sorted(out.items()) if len(members) > 1]


def top_link_bounds(members):
    """As link_bounds, for linked top-level lines."""
    return link_bounds([{'tu': l.tossups, 'bs': l.bonuses} for l in members], range(len(members)))


def link_sets(items, links=None):
    """[(parent index or None, link, [member indexes])] for every link."""
    if links is None:
        links = [_get(it, 'link') for it in items]
    out = {}
    parents = item_parents([_get(it, 'path') for it in items])
    for i, (parent, link) in enumerate(zip(parents, links)):
        if link:
            out.setdefault((parent, link), []).append(i)
    return [(parent, link, members) for (parent, link), members in out.items() if len(members) > 1]


def link_sums(items, members):
    return tuple(sum((_dec(_get(items[i], k)) for i in members), ZERO) for k in ('tu', 'bs'))


def link_bounds(items, members):
    """Per-packet bounds a link holds its members to together: ((min, max)
    tossups, (min, max) bonuses, (min, max) questions). A link adding up to
    a whole number is exactly that; one that doesn't is the numbers either
    side."""
    tu, bs = link_sums(items, members)

    def bounds(x):
        return int(x.to_integral_value(rounding=ROUND_FLOOR)), int(math.ceil(float(x) - 1e-9))
    return bounds(tu), bounds(bs), bounds(tu + bs)


def link_label(items, members):
    return ' + '.join(path_parts(_get(items[i], 'path'))[-1] for i in members)


def _most(item):
    """The most questions a row can give a packet: 0.2 / 0.2 is one."""
    return int(math.ceil(float(_dec(item.get('tu')) + _dec(item.get('bs'))) - 1e-9))


def check_tree(spec):
    """Why the rows of the line `spec` describes can't be saved: [sentence].
    The rows straight under a row can't ask for more than it holds, and no
    row can ask for more than its parent's "no sub-category more than N"
    allows. (The rows straight under the line are checked by the page.)"""
    problems = []
    items = spec.get('items', [])
    for parent, kids in siblings(items).items():
        holder = spec if parent is None else items[parent]
        label = spec.get('name') or 'this category' if parent is None else path_parts(holder['path'])[-1]
        if parent is not None and holder.get('tu') is not None and holder.get('bs') is not None:
            tu = sum((items[k]['tu'] for k in kids if items[k].get('tu') is not None), ZERO)
            bs = sum((items[k]['bs'] for k in kids if items[k].get('bs') is not None), ZERO)
            if tu > holder['tu'] or bs > holder['bs']:
                problems.append('the rows under {0} add up to {1} a packet, more than its own {2}. Raise '
                                '{0}\'s count or lower the rows'.format(label, pair(tu, bs),
                                                                       pair(holder['tu'], holder['bs'])))
        limit = holder.get('child_max')
        if limit:
            for k in kids:
                if _most(items[k]) > limit:
                    problems.append('{0} asks for {1} a packet, but no sub-category of {2} may give a packet '
                                    'more than {3} question{4}'.format(
                                        path_parts(items[k]['path'])[-1],
                                        pair(items[k].get('tu') or ZERO, items[k].get('bs') or ZERO),
                                        label, limit, '' if limit == 1 else 's'))
    return problems


def check_differ(spec):
    """Why "a packet's tossup and bonus never come from the same
    sub-category" can't work on the line `spec` describes, or None."""
    if not spec.get('is_pool') and not spec.get('items'):
        return 'it needs sub-categories for tossups and bonuses to be kept apart'
    for it in spec.get('items', []):
        if (it.get('tu') or 0) >= 1 and (it.get('bs') or 0) >= 1:
            return ('{0} asks every packet for a tossup and a bonus, which can\'t both come from it if they '
                    'never share a sub-category'.format(path_parts(it['path'])[-1]))
    return None


def _in(parts, member_paths):
    """Whether a question at `parts` falls under any of `member_paths`."""
    return any(parts[:len(m)] == m for m in member_paths if m)


def _packet_contents(qset, packets):
    contents = {p.id: {'tu': [], 'bs': []} for p in packets}
    for model, key in ((Tossup, 'tu'), (Bonus, 'bs')):
        for packet_id, cat, sub in (model.objects.filter(question_set=qset, packet__in=packets)
                                    .values_list('packet_id', 'category__category', 'category__subcategory')):
            parts = tuple(x.strip().lower() for x in [cat or ''] + (sub or '').split(' - ') if x.strip())
            contents[packet_id][key].append(parts)
    return contents


def rule_problems(qset):
    """Where the packets as they stand break a link, a "no sub-category more
    than N" rule or the "never the same sub-category" rule: [sentence].
    Reads the packets directly, so questions moved by hand on the grid are
    checked too. A link is broken by a packet holding more than its share;
    one holding less is short of questions, which the packetizer reports."""
    if not qset.per_packet_distribution:
        return []
    top_sets = top_link_sets(qset)
    lines = []
    for line in qset.distribution_lines.prefetch_related('items'):
        items = list(line.items.all())
        links = line_links(line, items)
        if line.groups_differ or line.child_max or link_sets(items, links) or any(it.child_max for it in items):
            lines.append((line, items, links))
    if not lines and not top_sets:
        return []
    from qems2.qsub.utils import EXTRAS_PACKET_NAME
    packets = [p for p in qset.packet_set.all() if p.packet_name != EXTRAS_PACKET_NAME]
    contents = _packet_contents(qset, packets)

    out = []

    def report(what, bad):
        if bad:
            out.append('{0} \u2014 {1} packet{2} ({3}{4})'.format(
                what, len(bad), '' if len(bad) == 1 else 's', ', '.join(bad[:5]), '\u2026' if len(bad) > 5 else ''))

    for link, members in top_sets:
        paths = [(c.lower(),) for l in members for c in l.category_list()]
        (_, tu_hi), (_, bs_hi), (_, q_hi) = top_link_bounds(members)
        bad = []
        for p in packets:
            tu = sum(1 for x in contents[p.id]['tu'] if _in(x, paths))
            bs = sum(1 for x in contents[p.id]['bs'] if _in(x, paths))
            if tu > tu_hi or bs > bs_hi or tu + bs > q_hi:
                bad.append(p.packet_name)
        report('More than {0} share'.format(' + '.join(l.name for l in members)), bad)

    for line, items, links in lines:
        tops = tuple(c.lower() for c in line.category_list())
        for parent, link, members in link_sets(items, links):
            paths = [_lower_parts(items[i].path) for i in members]
            (_, tu_hi), (_, bs_hi), (_, q_hi) = link_bounds(items, members)
            bad = []
            for p in packets:
                tu = sum(1 for x in contents[p.id]['tu'] if _in(x, paths))
                bs = sum(1 for x in contents[p.id]['bs'] if _in(x, paths))
                if tu > tu_hi or bs > bs_hi or tu + bs > q_hi:
                    bad.append(p.packet_name)
            report('{0}: more than {1} share'.format(line.name, link_label(items, members)), bad)

        caps = []
        if line.child_max:
            caps.append((line.name, None if line.is_pool else tops[:1], line.child_max))
        caps.extend((path_parts(it.path)[-1], _lower_parts(it.path), it.child_max) for it in items if it.child_max)
        for label, scope, limit in caps:
            bad = []
            for p in packets:
                seen = Counter()
                for x in contents[p.id]['tu'] + contents[p.id]['bs']:
                    if scope is None:
                        if x[:1] and x[0] in tops:
                            seen[x[:1]] += 1
                    elif len(x) > len(scope) and x[:len(scope)] == scope:
                        seen[x[:len(scope) + 1]] += 1
                if seen and max(seen.values()) > limit:
                    bad.append(p.packet_name)
            report('{0}: more than {1} question{2} from one sub-category'.format(
                label, limit, '' if limit == 1 else 's'), bad)

        if line.groups_differ:
            if line.is_pool:
                def member(parts):
                    return parts[:1] if parts and parts[0] in tops else None
            else:
                def member(parts):
                    return parts[:2] if len(parts) > 1 and parts[:1] == tops[:1] else None
            bad = []
            for p in packets:
                shared = {member(x) for x in contents[p.id]['tu']} & {member(x) for x in contents[p.id]['bs']}
                shared.discard(None)
                if shared:
                    bad.append(p.packet_name)
            report('{0}: a tossup and a bonus from the same sub-category'.format(line.name), bad)
    return out


# --------------------------------------------------------------------------
# Re-pasting over an existing distribution
# --------------------------------------------------------------------------

def _pasted_paths(parsed):
    """(tops, leaves): the top-level names a paste covers, and the paths it
    covers whole -- each sub-category row, each pool member, and each
    category pasted with no breakdown."""
    tops, leaves = set(), set()
    for b in parsed['blocks']:
        for cat in b['categories']:
            tops.add(cat.lower())
        if b['children']:
            for c in b['children']:
                leaves.add(tuple(p.lower() for p in path_parts(c['path'])))
        else:
            for cat in b['categories']:
                leaves.add((cat.lower(),))
    return tops, leaves


def _replaced_entries(qset, parsed):
    """[(entry, questions filed under it)] for every category of the set the
    paste leaves out."""
    tops, leaves = _pasted_paths(parsed)
    out = []
    for e in set_entries(qset):
        parts = tuple(p.lower() for p in entry_parts(e))
        if any(parts[:len(leaf)] == leaf for leaf in leaves):
            continue
        out.append((e, Tossup.objects.filter(category=e).count() + Bonus.objects.filter(category=e).count()))
    return out


def paste_impact(qset, parsed):
    """Everything accepting `parsed` would throw away, for the warning the
    review shows. None when the set has nothing to lose yet."""
    lines = list(qset.distribution_lines.prefetch_related('items'))
    replaced = _replaced_entries(qset, parsed)
    if not lines and not replaced:
        return None
    kept = sorted(((entry_path(e), n) for e, n in replaced if n), key=lambda x: x[0].lower())
    removed = sorted(entry_path(e) for e, n in replaced if not n)
    tops, _ = _pasted_paths(parsed)
    return {
        'categories': len(lines),
        'rows': sum(l.items.count() for l in lines),
        'rules': sum((1 if l.groups_differ else 0) + (1 if l.child_max else 0) +
                     sum(1 for it in l.items.all() if it.child_max) +
                     (0 if l.links_auto else len(link_sets(list(l.items.all())))) for l in lines),
        'hand_set': sum(1 for l in lines if any(
            v is not None for v in (l.extra_tossups, l.extra_bonuses, l.tiebreak_tossups, l.tiebreak_bonuses))),
        'removed': removed,
        'kept': kept,
        'orphaned_tags': CategoryTag.objects.filter(question_set=qset).exclude(category_path='').exclude(
            category_path__in=[p for p in category_paths(qset)
                               if path_parts(p)[0].lower() in tops]).count(),
    }


def replace_from_paste(qset, parsed):
    """Overwrite the set's distribution with `parsed`: its categories become
    the pasted ones, and every category the paste leaves out is deleted --
    except one with questions filed under it, which can't go without them and
    stays, unbudgeted. Returns how many categories were deleted."""
    apply_lines(qset, specs_from_parse(parsed))
    qset.refresh_from_db()
    if qset.distribution.owner_set_id != qset.id:
        return 0            # never delete from a distribution another set shares
    deleted = 0
    with transaction.atomic():
        for entry, n in _replaced_entries(qset, parsed):
            # Counted again here, across every set: a question moved to
            # another set can still be filed under this set's category.
            if n or entry.tossup_set.exists() or entry.bonus_set.exists():
                continue
            entry.delete()
            deleted += 1
    derive(qset)
    return deleted
