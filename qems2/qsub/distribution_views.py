"""Pages for the per-packet distribution: the Distribution page itself,
pasting an announcement into it, bringing tags over from another set, and
switching an existing set onto it (and back). The logic lives in
distribution_lines; this is the request handling."""

from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db import transaction
from django.http import HttpResponseRedirect, HttpResponseNotAllowed
from django.shortcuts import render, get_object_or_404

from qems2.qsub import distribution_lines as dl
from qems2.qsub.models import QuestionSet, CategoryTag


def _roles(qset, writer):
    owner = qset.is_owner(writer)
    editor = owner or qset.editor.filter(id=writer.id).exists()
    member = editor or qset.writer.filter(id=writer.id).exists()
    return owner, editor, member


def _deny(request, text):
    return render(request, 'failure.html', {'message': text, 'message_class': 'alert-box alert'})


def _int(raw, default=0, low=0, high=10000):
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def _pair_input(raw, default):
    """"20 / 20", "20/20" or "20" -> (20, 20)."""
    raw = (raw or '').strip()
    if not raw:
        return default
    parts = [p for p in raw.replace(' ', '').split('/') if p != '']
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        raise ValueError('"{0}" should be two whole numbers, tossups / bonuses'.format(raw))
    if not nums or any(n < 0 or n > 1000 for n in nums):
        raise ValueError('"{0}" should be two whole numbers, tossups / bonuses'.format(raw))
    return (nums[0], nums[1] if len(nums) > 1 else nums[0])


def _pair_fields(post, name, default):
    """A tossups / bonuses pair from two boxes (`name_tu`, `name_bs`), or
    from one "20 / 20" box called `name`."""
    if (name + '_tu') in post or (name + '_bs') in post:
        tu = (post.get(name + '_tu') or '').strip()
        bs = (post.get(name + '_bs') or '').strip()
        return _pair_input('{0}/{1}'.format(tu or default[0], bs or default[1]), default)
    return _pair_input(post.get(name), default)


def _lines_from_db(qset):
    out = []
    for i, line in enumerate(qset.distribution_lines.prefetch_related('items')):
        out.append({
            'idx': i, 'id': line.id, 'name': line.name,
            'categories': ', '.join(line.category_list()), 'category_list': line.category_list(),
            'is_pool': line.is_pool, 'tu': dl.fmt(line.tossups), 'bs': dl.fmt(line.bonuses),
            'extra_tu': '' if line.extra_tossups is None else line.extra_tossups,
            'extra_bs': '' if line.extra_bonuses is None else line.extra_bonuses,
            'tb_tu': '' if line.tiebreak_tossups is None else line.tiebreak_tossups,
            'tb_bs': '' if line.tiebreak_bonuses is None else line.tiebreak_bonuses,
            'extra_tu_set': line.extra_tossups is not None, 'extra_bs_set': line.extra_bonuses is not None,
            'tb_tu_set': line.tiebreak_tossups is not None, 'tb_bs_set': line.tiebreak_bonuses is not None,
            'items': _rows_for_display(line),
            'groups_differ': line.groups_differ,
            'child_max': line.child_max or '', 'links_auto': line.links_auto,
            'obj': line,
        })
    for row, link in zip(out, dl.top_links(qset, [r['obj'] for r in out])):
        row['link'] = link or ''
    return out


def _rows_for_display(line):
    """What a category's Sub-categories tree shows: its rows, plus a blank
    row for any top-level category it covers that no row names (a pool made
    before rows existed, or a category whose name differs from what it
    covers), so nothing it counts is hidden."""
    items = list(line.items.all())
    rows = [{'path': it.path, 'tu': dl.fmt(it.tossups), 'bs': dl.fmt(it.bonuses),
             'link': link or '', 'child_max': it.child_max or ''}
            for it, link in zip(items, dl.line_links(line, items))]
    named = {dl.path_parts(r['path'])[0].lower() for r in rows if dl.path_parts(r['path'])}
    for cat in line.category_list():
        if cat.lower() not in named and not (not rows and cat.lower() == line.name.lower()):
            rows.append({'path': cat, 'tu': '', 'bs': '', 'link': '', 'child_max': ''})
    return _as_tree(rows, _home(line))


def _as_tree(rows, home):
    """`rows` in tree order -- every row followed by the rows under it --
    each with its `depth` (1 straight under the line) and the `label` it
    shows: its path less its parent's ("Opera" under "Auditory"), and a row
    straight under an ordinary category less the category ("Physics", not
    "Science - Physics"; the page posts the home back, so the bare name finds
    its way to the same place)."""
    parents = dl.item_parents([r['path'] for r in rows])
    kids = {}
    for i, parent in enumerate(parents):
        kids.setdefault(parent, []).append(i)
    out = []

    def walk(parent, depth):
        for i in kids.get(parent, []):
            r = rows[i]
            parts = dl.path_parts(r['path'])
            if parent is not None:
                r['label'] = ' - '.join(parts[len(dl.path_parts(rows[parent]['path'])):])
            elif home and len(parts) > 1 and parts[0].lower() == home.lower():
                r['label'] = ' - '.join(parts[1:])
            else:
                r['label'] = r['path']
            r['depth'] = depth
            r['has_kids'] = bool(kids.get(i))
            out.append(r)
            walk(i, depth + 1)
    walk(None, 1)
    return out


def _home(line):
    """The one top-level category an ordinary category is about, or ''."""
    cats = line.category_list()
    return cats[0] if len(cats) == 1 and not line.is_pool else ''


def _lines_from_post(post, tops=(), paths=()):
    """(display dicts, specs, errors) from the Distribution page's form.

    A category's rows say what it covers. Each is a category ("Geography")
    or a sub-category ("Literature - American"), with an optional count per
    packet. The top-level categories it covers are the rows' own, and rows
    spanning more than one make it share its count between them (a pool).
    With no rows it covers the category of its own name. A bare name that
    isn't one of the set's categories ("American") is a sub-category of the
    one the row is about. `tops` is the set's top-level names.
    """
    known = {t.lower() for t in tops}
    known_paths = {p.lower() for p in paths}
    count = _int(post.get('line_count'), 0, 0, 500)
    display, specs, errors = [], [], []
    seen = {}
    for i in range(count):
        key = 'line-{0}-'.format(i)
        if post.get(key + 'delete'):
            continue
        name = (post.get(key + 'name') or '').strip()

        raw = []
        for j in range(_int(post.get(key + 'item_count'), 0, 0, 200)):
            ik = '{0}item-{1}-'.format(key, j)
            path = ' - '.join(dl.path_parts(post.get(ik + 'path') or ''))
            if path and not post.get(ik + 'delete'):
                raw.append({'path': path, 'tu': post.get(ik + 'tu', ''), 'bs': post.get(ik + 'bs', ''),
                            'link': post.get(ik + 'link', ''), 'child_max': post.get(ik + 'child_max', ''),
                            'order': _int(post.get(ik + 'order'), j, 0, 10000)})
        # Rows are saved in the order the page shows them (dragged or not).
        raw.sort(key=lambda r: r['order'])
        # An older form (and the tests) may still say what it covers outright.
        listed = [c.strip() for c in (post.get(key + 'categories') or '').replace(';', ',').split(',')
                  if c.strip()]
        if not name and not raw and not listed:
            continue
        home = (post.get(key + 'home') or '').strip() or (listed[0] if listed else name)
        for r in raw:
            parts = dl.path_parts(r['path'])
            if len(parts) != 1 or not home or parts[0].lower() == home.lower():
                continue
            under_home = '{0} - {1}'.format(home, parts[0])
            # Fine Arts' "Other" is Fine Arts - Other even though there is
            # also a top-level Other: a sub-category of the home comes first.
            if under_home.lower() in known_paths or parts[0].lower() not in known:
                r['path'] = under_home
        cats = list(listed)
        for r in raw:
            top = dl.path_parts(r['path'])[0]
            if top.lower() not in [c.lower() for c in cats]:
                cats.append(top)
        if not cats:
            cats = [name]
        if not name:
            name = cats[0] if len(cats) == 1 else ' / '.join(cats)
        is_pool = bool(post.get(key + 'pool')) or len(cats) > 1

        # Links come from the counts unless the page says they were set by
        # hand; a post without the switch (an older form) means automatic.
        links_auto = post.get(key + 'links_auto', '1') != '0'
        row = {'idx': len(display), 'name': name, 'categories': ', '.join(cats), 'category_list': cats,
               'is_pool': is_pool, 'tu': post.get(key + 'tu', ''), 'bs': post.get(key + 'bs', ''),
               'extra_tu': post.get(key + 'extra_tu', ''), 'extra_bs': post.get(key + 'extra_bs', ''),
               'tb_tu': post.get(key + 'tb_tu', ''), 'tb_bs': post.get(key + 'tb_bs', ''),
               'child_max': post.get(key + 'child_max', ''), 'links_auto': links_auto,
               'link': post.get(key + 'link', ''),
               'items': _as_tree([dict(r) for r in raw], home if len(cats) == 1 and not is_pool else '')}
        spec = {'name': name, 'categories': cats, 'is_pool': is_pool, 'items': [],
                'links_auto': links_auto, 'child_max': _int(row['child_max'], 0, 0, 50) or None,
                'link': _int(row['link'], 0, 0, 50) or None}
        try:
            spec['tu'] = dl.to_decimal(row['tu'])
            spec['bs'] = dl.to_decimal(row['bs'])
            # Every box shows a number, so the number alone can't say whether
            # it was typed: only a box flagged as set by hand (the page sets
            # the flag when someone types in it) is fixed; the rest stay the
            # automatic share.
            for field in ('extra_tu', 'extra_bs', 'tb_tu', 'tb_bs'):
                is_set = post.get(key + field + '_set') == '1' and str(row[field]).strip() != ''
                row[field + '_set'] = is_set
                spec[field] = _int(row[field]) if is_set else None
        except ValueError as ex:
            errors.append('{0}: {1}'.format(name, ex))
        for c in cats:
            if c.lower() in seen:
                errors.append('{0} is in two categories ({1} and {2}). Put it in one, or put both in one '
                              'category to share a count.'.format(c, seen[c.lower()], name))
            seen[c.lower()] = name
        for r in raw:
            try:
                spec['items'].append({'path': r['path'], 'tu': dl.to_decimal(r['tu'], allow_blank=True),
                                      'bs': dl.to_decimal(r['bs'], allow_blank=True),
                                      'link': _int(r['link'], 0, 0, 50) or None,
                                      'child_max': _int(r['child_max'], 0, 0, 50) or None})
            except ValueError as ex:
                errors.append('{0} \u2013 {1}: {2}'.format(name, r['path'], ex))
        # Rows may leave part of the count to the rest of the category, but
        # can't ask for more than the category itself holds -- nor can the
        # rows under a row ask for more than it holds.
        if 'tu' in spec and 'bs' in spec:
            parents = dl.item_parents([it['path'] for it in spec['items']])
            top_rows = [it for it, parent in zip(spec['items'], parents) if parent is None]
            row_tu = sum((it['tu'] for it in top_rows if it['tu'] is not None), dl.ZERO)
            row_bs = sum((it['bs'] for it in top_rows if it['bs'] is not None), dl.ZERO)
            if row_tu > spec['tu'] or row_bs > spec['bs']:
                errors.append('{0}: its rows add up to {1} a packet, more than its own {2}. Raise {0}\'s '
                              'count or lower the rows.'.format(name, dl.pair(row_tu, row_bs),
                                                                 dl.pair(spec['tu'], spec['bs'])))
            for problem in dl.check_tree(spec):
                errors.append('{0}: {1}.'.format(name, problem))
        row['groups_differ'] = spec['groups_differ'] = bool(post.get(key + 'groups_differ'))
        if spec['groups_differ'] and 'tu' in spec:
            problem = dl.check_differ(spec)
            if problem:
                errors.append('{0}: {1}.'.format(name, problem))
        row['order'] = _int(post.get(key + 'order'), i, 0, 10000)
        display.append(row)
        specs.append(spec)
    # Categories too, in the order the page shows them.
    order = sorted(range(len(display)), key=lambda k: display[k]['order'])
    return [display[k] for k in order], [specs[k] for k in order], errors


def _page_context(qset, writer, lines, posted=False):
    owner, editor, member = _roles(qset, writer)
    paths = dl.category_paths(qset)
    ctx = {'qset': qset, 'user': writer, 'lines': lines, 'can_edit': editor, 'is_owner': owner,
           'category_paths': paths, 'top_names': dl.top_level_names(qset),
           'link_letters': list(enumerate('ABCDEFGH', 1)), 'posted': posted}
    for l in lines:
        l['home'] = l['category_list'][0] if len(l['category_list']) == 1 and not l['is_pool'] else ''
    if not posted:
        objs = [l['obj'] for l in lines]
        totals = dl.line_totals(qset, objs)
        written = dl.written_by_line(qset, objs)
        tags = dl.tag_rows_by_path(qset)
        for l in lines:
            t = totals[l['id']]
            l['totals'] = t
            l['written'] = written.get(l['id'], (0, 0))
            l['covers'] = ', '.join(dl.path_parts(it['path'])[-1] for it in l['items']) or \
                ', '.join(l['category_list'])
            groups = {}
            for path, rows in tags.items():
                if not path:
                    continue
                top = dl.path_parts(path)[0].lower()
                if top in [c.lower() for c in l['category_list']]:
                    for tag, prog in rows:
                        groups.setdefault(tag.group_name or 'Ungrouped', []).append(
                            {'tag': tag, 'p': prog, 'path': path})
            l['tag_groups'] = sorted(groups.items(), key=lambda kv: (kv[0] == 'Ungrouped', kv[0].lower()))
            l['tag_count'] = sum(len(v) for v in groups.values())
        ctx['set_totals'] = totals['set']
        if qset.tiebreak_tossups_per_packet or qset.tiebreak_bonuses_per_packet:
            ctx['tiebreak_per_set'] = '= {0} / {1} across {2} packets'.format(
                totals['set']['tu']['tiebreak_asked'], totals['set']['bs']['tiebreak_asked'], qset.num_packets)
        ctx['whole_set_tags'] = [{'tag': t, 'p': p} for t, p in tags.get('', [])]
        ctx['unbudgeted'] = dl.unbudgeted(qset)
        ctx['size_ok'] = (totals['set']['tu']['per_packet'] == qset.tossups_per_packet and
                          (qset.tossups_only or totals['set']['bs']['per_packet'] == qset.bonuses_per_packet))
    ctx['tag_sources'] = [s for s in _member_sets(writer) if s.id != qset.id
                          and CategoryTag.objects.filter(question_set=s).exists()]
    return ctx


def _member_sets(writer):
    sets = (QuestionSet.objects.filter(owner=writer) | writer.co_owned_sets.all()
            | writer.question_set_editor.all() | writer.question_set_writer.all())
    return list(sets.distinct().order_by('name'))


@login_required
def set_distribution(request, qset_id):
    qset = get_object_or_404(QuestionSet, id=qset_id)
    writer = request.user.writer
    owner, editor, member = _roles(qset, writer)
    if not member:
        return _deny(request, 'You are not a member of this set.')
    if not qset.per_packet_distribution:
        return HttpResponseRedirect('/switch_distribution/{0}/'.format(qset.id))

    if request.method == 'POST':
        if not editor:
            return _deny(request, 'Only the set\'s editors can change its distribution.')
        display, specs, errors = _lines_from_post(request.POST, dl.top_level_names(qset), dl.category_paths(qset))
        try:
            n = _int(request.POST.get('num_packets'), qset.num_packets, 1, 500)
            size = _pair_fields(request.POST, 'size', (qset.tossups_per_packet, qset.bonuses_per_packet))
            tb = _pair_fields(request.POST, 'tiebreak', (qset.tiebreak_tossups_per_packet,
                                                         qset.tiebreak_bonuses_per_packet))
            extras = _pair_fields(request.POST, 'extras', (qset.extra_tossups, qset.extra_bonuses))
            if size[0] < 1 or (size[1] < 1 and not qset.tossups_only):
                raise ValueError('A packet needs at least one tossup and one bonus.')
        except ValueError as ex:
            errors.append(str(ex))
        if errors:
            for e in errors:
                messages.error(request, e)
            ctx = _page_context(qset, writer, display, posted=True)
            ctx['top_links_auto'] = request.POST.get('top_links_auto', '1') != '0'
            ctx.update({'form': {k: request.POST.get(k, '') for k in (
                'size_tu', 'size_bs', 'tiebreak_tu', 'tiebreak_bs', 'extras_tu', 'extras_bs')},
                'form_packets': request.POST.get('num_packets', '')})
            return render(request, 'set_distribution.html', ctx)
        with transaction.atomic():
            qset.num_packets = n
            qset.tossups_per_packet, qset.bonuses_per_packet = size
            qset.tiebreak_tossups_per_packet, qset.tiebreak_bonuses_per_packet = tb
            qset.extra_tossups, qset.extra_bonuses = extras
            # A post without the switch (an older form) leaves it as it was.
            if 'top_links_auto' in request.POST:
                qset.top_links_auto = request.POST.get('top_links_auto') != '0'
            qset.save(update_fields=['num_packets', 'tossups_per_packet', 'bonuses_per_packet',
                                     'tiebreak_tossups_per_packet', 'tiebreak_bonuses_per_packet',
                                     'extra_tossups', 'extra_bonuses', 'top_links_auto'])
            dl.apply_lines(qset, specs)
        cache.clear()
        messages.success(request, 'Distribution saved.')
        return HttpResponseRedirect('/set_distribution/{0}/'.format(qset.id))

    ctx = _page_context(qset, writer, _lines_from_db(qset))
    ctx['top_links_auto'] = qset.top_links_auto
    ctx.update({'form': {'size_tu': qset.tossups_per_packet, 'size_bs': qset.bonuses_per_packet,
                         'tiebreak_tu': qset.tiebreak_tossups_per_packet,
                         'tiebreak_bs': qset.tiebreak_bonuses_per_packet,
                         'extras_tu': qset.extra_tossups, 'extras_bs': qset.extra_bonuses},
                'form_packets': qset.num_packets})
    return render(request, 'set_distribution.html', ctx)


@login_required
def set_distribution_paste(request, qset_id):
    qset = get_object_or_404(QuestionSet, id=qset_id)
    writer = request.user.writer
    owner, editor, member = _roles(qset, writer)
    if not editor:
        return _deny(request, 'Only the set\'s editors can change its distribution.')
    if not qset.per_packet_distribution:
        return HttpResponseRedirect('/switch_distribution/{0}/'.format(qset.id))

    text = request.POST.get('text', '') if request.method == 'POST' else ''
    ctx = {'qset': qset, 'user': writer, 'text': text,
           'has_lines': qset.distribution_lines.exists(),
           'new_set': request.GET.get('new') == '1' or request.POST.get('new') == '1'}
    if request.method == 'POST' and text.strip():
        parsed = dl.parse_announcement(text, dl.catalog(qset),
                                       packet_size=(qset.tossups_per_packet, qset.bonuses_per_packet))
        impact = dl.paste_impact(qset, parsed)
        confirmed = request.POST.get('confirm_replace') == '1'
        if request.POST.get('action') == 'accept' and parsed['blocks'] and impact and not confirmed:
            messages.error(request, 'Nothing was saved. Tick the box to confirm you want to replace the '
                                    'current distribution.')
        elif request.POST.get('action') == 'accept' and parsed['blocks']:
            # Never resize to a fraction: 20.5 used to save as 20.
            if request.POST.get('resize') == '1' and parsed['mismatch'] and parsed['mismatch']['whole']:
                qset.tossups_per_packet = int(parsed['total_tu'])
                qset.bonuses_per_packet = int(parsed['total_bs'])
                qset.save(update_fields=['tossups_per_packet', 'bonuses_per_packet'])
            deleted = dl.replace_from_paste(qset, parsed)
            missing = dl.editors_from_parse(qset, parsed)
            cache.clear()
            messages.success(request, 'Distribution {0} from the announcement: {1} categories{2}.'.format(
                'replaced' if impact else 'written', len(parsed['blocks']),
                ', {0} old categor{1} removed'.format(deleted, 'y' if deleted == 1 else 'ies') if deleted else ''))
            if missing:
                messages.warning(request, 'Not on this set, so not tagged as subject editors: {0}. '
                                          'Add them to the set, then tag them from Writers & Editors.'
                                 .format(', '.join(missing)))
            return HttpResponseRedirect('/set_distribution/{0}/'.format(qset.id))
        ctx['parsed'] = parsed
        ctx['impact'] = impact
        ctx['decisions'] = sum(1 for b in parsed['blocks'] if b['notes']) + (1 if parsed['mismatch'] else 0)
    return render(request, 'set_distribution_paste.html', ctx)


@login_required
def set_distribution_tags(request, qset_id):
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    qset = get_object_or_404(QuestionSet, id=qset_id)
    writer = request.user.writer
    owner, editor, member = _roles(qset, writer)
    if not editor:
        return _deny(request, 'Only the set\'s editors can add category tags.')
    src = QuestionSet.objects.filter(id=_int(request.POST.get('source'), 0)).first()
    if src is None or not _roles(src, writer)[2]:
        messages.error(request, 'Pick a set you are on to bring tags from.')
    else:
        added = dl.copy_tags(qset, src, scale=request.POST.get('scale') == '1')
        cache.clear()
        messages.success(request, '{0} tag(s) brought over from {1}{2}.'.format(
            added, src.name, ', quotas scaled to {0} packets'.format(qset.num_packets)
            if request.POST.get('scale') == '1' else ''))
    return HttpResponseRedirect('/set_distribution/{0}/'.format(qset.id))


@login_required
def switch_distribution(request, qset_id):
    qset = get_object_or_404(QuestionSet, id=qset_id)
    writer = request.user.writer
    owner, editor, member = _roles(qset, writer)
    if not member:
        return _deny(request, 'You are not a member of this set.')
    if qset.per_packet_distribution:
        return HttpResponseRedirect('/set_distribution/{0}/'.format(qset.id))

    proposal = dl.propose_lines(qset)
    if request.method == 'POST':
        if not owner:
            return _deny(request, 'Only the set\'s owner can switch its distribution.')
        chosen, errors = {}, []
        for row in proposal['rows']:
            i = row['index']
            try:
                chosen[i] = {'include': bool(request.POST.get('row-{0}-include'.format(i))),
                             'tu': dl.to_decimal(request.POST.get('row-{0}-tu'.format(i), row['tu'])),
                             'bs': dl.to_decimal(request.POST.get('row-{0}-bs'.format(i), row['bs']))}
            except ValueError as ex:
                errors.append('{0}: {1}'.format(row['name'], ex))
        if errors:
            for e in errors:
                messages.error(request, e)
            return HttpResponseRedirect('/switch_distribution/{0}/'.format(qset.id))
        specs = dl.specs_from_proposal(proposal, chosen)
        if request.POST.get('tiebreak_as_extras') == '1':
            extras = (proposal['tiebreak_tu'], proposal['tiebreak_bs'])
            overrides = proposal['tiebreak']
        else:
            extras, overrides = (0, 0), None
        dl.switch_set(qset, specs, extras=extras, extra_overrides=overrides)
        cache.clear()
        messages.success(request, 'Switched. You can go back from the bottom of this page.')
        return HttpResponseRedirect('/set_distribution/{0}/'.format(qset.id))

    shared_with = qset.distribution.questionset_set.exclude(id=qset.id).count()
    for row in proposal['rows']:
        row['tu_s'], row['bs_s'] = dl.fmt(row['tu']), dl.fmt(row['bs'])
        row['proposed'] = '{0} / {1}{2}'.format(row['tu_s'], row['bs_s'], ' pool' if row['kind'] == 'pool' else '')
        row['items_s'] = ', '.join('{0} {1}'.format(dl.path_parts(it['path'])[-1], dl.pair(it['tu'], it['bs']))
                                   for it in row['items'])
    return render(request, 'switch_distribution.html', {
        'qset': qset, 'user': writer, 'proposal': proposal, 'is_owner': owner,
        'shared_with': shared_with, 'dist_public': qset.distribution.public,
        'tiebreak_items': sorted(proposal['tiebreak'].items()),
    })


@login_required
def revert_distribution(request, qset_id):
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    qset = get_object_or_404(QuestionSet, id=qset_id)
    if not qset.is_owner(request.user.writer):
        return _deny(request, 'Only the set\'s owner can do that.')
    if dl.restore_legacy(qset):
        cache.clear()
        messages.success(request, '{0} is back on its old set totals and packetizer quotas. Its '
                                  'per-packet lines are kept if you switch again.'.format(qset.name))
        return HttpResponseRedirect('/question_set_distribution/{0}'.format(qset.id))
    messages.error(request, 'There is nothing to go back to: this set started on a per-packet '
                            'distribution.')
    return HttpResponseRedirect('/set_distribution/{0}/'.format(qset.id))
