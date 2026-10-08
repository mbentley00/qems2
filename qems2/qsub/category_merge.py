"""Merging a sub-category (and everything under it) into one of its parents.

"Literature - American - Drama" merged into "Literature - American": the
Drama questions are refiled under American, and Drama is gone. Merging
"Literature - American" into "Literature" takes American's own
sub-categories with it, so the whole branch becomes Literature.

Everything that names a category by its path follows the questions: the
category's tags (a tag already on the parent with the same name takes the
moved tag's questions), its comments, subject-editor tags, the set's old
totals and packetizer quotas, and the per-packet distribution's rows, whose
counts are added to the parent's. Questions keep their packets and numbers.

A set on a distribution other sets use first gets a copy of its own (as
switching to a per-packet distribution does), so a merge never reaches
another set.
"""

from django.db import transaction

from qems2.qsub import distribution_lines as dl
from qems2.qsub.models import (
    Bonus, CategoryComment, CategoryTag, DistributionEntry, EditorTag,
    PacketizationEntry, PacketSlotVacancy, SetWideDistributionEntry, TieBreakDistributionEntry, Tossup)


def _low(parts):
    return tuple(p.lower() for p in parts)


def _under(path, parts):
    """Whether the category path string `path` is `parts` or below it."""
    have = _low(dl.path_parts(path))
    return bool(have) and have[:len(parts)] == _low(parts)


def branch(qset, source):
    """The set's categories at `source` or below it."""
    return [e for e in dl.set_entries(qset) if _low(dl.entry_parts(e))[:len(source)] == _low(source)]


def merge_targets(qset, source):
    """The parents `source` can be merged into, nearest first, spelled as the
    set spells them."""
    spelled = {_low(dl.path_parts(p)): dl.path_parts(p) for p in dl.category_paths(qset)}
    return [spelled.get(_low(source[:k]), tuple(source[:k])) for k in range(len(source) - 1, 0, -1)]


def plan(qset, source):
    """What merging `source` would take with it, for the confirmation page."""
    entries = branch(qset, source)
    ids = [e.id for e in entries]
    tags = [t for t in CategoryTag.objects.filter(question_set=qset) if t.category_path and _under(t.category_path, source)]
    return {
        'tossups': Tossup.objects.filter(question_set=qset, category_id__in=ids).count(),
        'bonuses': Bonus.objects.filter(question_set=qset, category_id__in=ids).count(),
        'below': sorted({dl.entry_path(e) for e in entries if len(dl.entry_parts(e)) > len(source)}, key=str.lower),
        'tags': tags,
        'comments': sum(1 for c in CategoryComment.objects.filter(question_set=qset)
                        if _under(c.category_path, source)),
        'shared': not dl.owns_distribution(qset) and qset.distribution.questionset_set.exclude(id=qset.id).exists(),
    }


def _add(a, b):
    """Two optional counts added; blank stays blank only when both are."""
    if a is None and b is None:
        return None
    return (a or 0) + (b or 0)


def merge(qset, source, target):
    """Merge the category `source` (path parts) and everything under it into
    its parent `target`. Returns a summary dict. Raises ValueError for a
    target that is not one of the source's parents, or a source the set does
    not have."""
    source, target = tuple(source), tuple(target)
    if len(source) < 2 or len(target) >= len(source) or _low(source[:len(target)]) != _low(target):
        raise ValueError('A category can only be merged into one of its parents.')
    from qems2.qsub.views import drop_inapplicable_tags

    with transaction.atomic():
        dl.give_set_own_distribution(qset)
        qset.refresh_from_db()
        entries = branch(qset, source)
        if not entries:
            raise ValueError('{0} is not one of this set\'s categories.'.format(' - '.join(source)))
        dest = next((e for e in dl.set_entries(qset) if _low(dl.entry_parts(e)) == _low(target)), None)
        if dest is None:
            dest = DistributionEntry.objects.create(
                distribution=qset.distribution, category=target[0], subcategory=' - '.join(target[1:]),
                min_tossups=0, min_bonuses=0, max_tossups=0, max_bonuses=0)
        to_path = dl.entry_path(dest)
        out = {'target': to_path, 'questions': 0, 'categories': 0, 'tags_moved': 0, 'tags_merged': 0}

        # Tags first, so the questions keep them once they sit in the parent.
        for tag in CategoryTag.objects.filter(question_set=qset).order_by('id'):
            if not (tag.category_path and _under(tag.category_path, source)):
                continue
            twin = (CategoryTag.objects.filter(question_set=qset, category_path=to_path,
                                               group_name=tag.group_name, name__iexact=tag.name)
                    .exclude(id=tag.id).first())
            if twin is not None:
                twin.tossups.add(*tag.tossups.all())
                twin.bonuses.add(*tag.bonuses.all())
                tag.delete()
                out['tags_merged'] += 1
            else:
                tag.category_path = to_path
                tag.save(update_fields=['category_path'])
                out['tags_moved'] += 1

        # The questions, and the set's own numbers for the merged categories.
        for e in entries:
            if e.id == dest.id:
                continue
            for model in (Tossup, Bonus):
                qs = model.objects.filter(question_set=qset, category=e)
                for q in qs:
                    drop_inapplicable_tags(q, dest)
                out['questions'] += qs.update(category=dest)
            for model in (SetWideDistributionEntry, TieBreakDistributionEntry):
                for row in model.objects.filter(question_set=qset, dist_entry=e):
                    keep, _ = model.objects.get_or_create(question_set=qset, dist_entry=dest,
                                                          defaults={'num_tossups': 0, 'num_bonuses': 0})
                    keep.num_tossups += row.num_tossups or 0
                    keep.num_bonuses += row.num_bonuses or 0
                    keep.save(update_fields=['num_tossups', 'num_bonuses'])
                    row.delete()
            # The set has the distribution to itself now; a category nothing
            # is filed under any more goes.
            if not (Tossup.objects.filter(category=e).exists() or Bonus.objects.filter(category=e).exists()):
                e.delete()
                out['categories'] += 1

        # Things that name a category by its path.
        for c in CategoryComment.objects.filter(question_set=qset):
            if _under(c.category_path, source):
                c.category_path = to_path
                c.save(update_fields=['category_path'])
        for t in EditorTag.objects.filter(question_set=qset).exclude(category=''):
            if _under(t.category, source):
                if EditorTag.objects.filter(question_set=qset, editor=t.editor, category=to_path).exists():
                    t.delete()
                else:
                    t.category = to_path
                    t.save(update_fields=['category'])
        for v in PacketSlotVacancy.objects.filter(question_set=qset).exclude(category=''):
            if _under(v.category, source):
                v.category = to_path
                v.save(update_fields=['category'])

        if qset.per_packet_distribution:
            _merge_line_rows(qset, source, target, to_path)
            dl.derive(qset)
        else:
            _merge_quotas(qset, source, to_path)
    return out


def _merge_line_rows(qset, source, target, to_path):
    """A per-packet distribution's rows for the merged branch: their counts
    go to the parent's row (made if the parent had none and is not the
    category itself), and the rows go."""
    for line in qset.distribution_lines.prefetch_related('items'):
        items = list(line.items.all())
        gone = [it for it in items if _under(it.path, source)]
        if not gone:
            continue
        tu = bs = None
        for it in gone:
            tu, bs = _add(tu, it.tossups), _add(bs, it.bonuses)
        keep = next((it for it in items if _low(dl.path_parts(it.path)) == _low(target)), None)
        home = [c.lower() for c in line.category_list()]
        if keep is not None:
            keep.tossups, keep.bonuses = _add(keep.tossups, tu), _add(keep.bonuses, bs)
            keep.save(update_fields=['tossups', 'bonuses'])
        elif not (len(target) == 1 and target[0].lower() in home and not line.is_pool):
            # The parent is a sub-category with no row of its own: the first
            # merged row becomes its row, carrying all their counts.
            first = gone.pop(0)
            first.path, first.tossups, first.bonuses, first.link, first.child_max = to_path, tu, bs, None, None
            first.save(update_fields=['path', 'tossups', 'bonuses', 'link', 'child_max'])
        for it in gone:
            it.delete()


def _merge_quotas(qset, source, to_path):
    """An older set's packetizer quotas for the merged branch, added to the
    parent's."""
    rows = [p for p in PacketizationEntry.objects.filter(question_set=qset) if _under(p.path, source)]
    if not rows:
        return
    keep = PacketizationEntry.objects.filter(question_set=qset, path=to_path).first()
    if keep is None:
        keep = rows.pop(0)
        keep.path, keep.depth = to_path, len(dl.path_parts(to_path)) - 1
    for p in rows:
        for f in ('min_tossups', 'max_tossups', 'min_bonuses', 'max_bonuses'):
            setattr(keep, f, _add(getattr(keep, f), getattr(p, f)))
        p.delete()
    keep.save()
