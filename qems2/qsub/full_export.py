"""Full archive of a question set (site admins only).

The ordinary exports (Word, PDF, TSV, YAPP) are for reading and playing a set.
This one is for keeping it: a zip holding every database row that belongs to
the set — its settings, distribution, packets, every question with all of its
fields, the question history, tags, comments with their replies, resolutions,
mentions and anchors, playtest results, packetization rules and logs — plus a
list of the people involved and their roles in the set.

What belongs to a set is found by following the schema rather than from a
fixed list, so a model added later is picked up without touching this file:
start from the set, then take every row that points at something already
taken (a tossup's history, a comment's replies, a playtest's buzzes), until
nothing new turns up. Shared things a set only points at — writers, its
distribution and categories, question types, role groups — are added as
references, but nothing is pulled in *through* them, so the archive never
wanders into other sets.

Left out on purpose: secrets (set API keys, join-link tokens), who visited
when (per-person page-visit tracking), passwords, email and IP addresses.

There is no importer for this format yet; it is an archive. ``data/`` holds
Django's own serialization of each model, which ``loaddata``-style tooling
can read, and ``readable/`` holds each packet as YAPP2 JSON.
"""

import datetime
import io
import json
import re
import zipfile
from collections import defaultdict

from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.core import serializers
from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import Q
from django_comments.models import Comment

from . import yapp_export
from .models import (Bonus, CategoryTag, Distribution, DistributionEntry, Packet,
                     QuestionHistory, QuestionSet, QuestionType, RoleGroup, Tossup,
                     Writer)

FORMAT = 'qems-set-archive/1'

# Models never written: secrets, per-person visit tracking, and site-wide
# things that aren't part of any one set.
SKIP = {'SetApiKey', 'SetJoinLink', 'ActivitySeen', 'SetVisit', 'Announcement',
        'ReferenceDataOverride', 'SuggestionFeedback'}
LEFT_OUT = [
    'Set API keys and join-link tokens (secrets)',
    'Page-visit tracking (who looked at the set when)',
    'Passwords, email addresses and IP addresses',
]

# Shared rows a set points at. Archived when referenced; never followed.
REFERENCE = {Writer, Distribution, DistributionEntry, QuestionType, RoleGroup}

# Personal fields blanked on comments.
COMMENT_PRIVATE = ('user_email', 'ip_address')


def _relations(model):
    """(field, related model) for each concrete FK/one-to-one/M2M on a model."""
    out = []
    for f in model._meta.get_fields():
        if f.is_relation and f.concrete and f.related_model is not None:
            out.append((f, f.related_model))
    return out


def collect(qset):
    """{model: {pk: row}} of everything the archive holds for ``qset``."""
    rows = defaultdict(dict)
    rows[QuestionSet][qset.pk] = qset

    candidates = [m for m in apps.get_app_config('qsub').get_models()
                  if m.__name__ not in SKIP and m not in REFERENCE and m is not QuestionSet]
    candidates += [m for m in apps.get_app_config('django_comments').get_models()
                   if m is not Comment]
    commented = [(m, ContentType.objects.get_for_model(m)) for m in (Tossup, Bonus, Packet)]

    def add(model, objs):
        new = 0
        for o in objs:
            if o.pk not in rows[model]:
                rows[model][o.pk] = o
                new += 1
        return new

    changed = True
    while changed:
        changed = False
        for model in candidates:
            for f, rel in _relations(model):
                if rel in REFERENCE or not rows.get(rel):
                    continue
                ids = list(rows[rel])
                for i in range(0, len(ids), 500):
                    if add(model, model.objects.filter(**{f.name + '__in': ids[i:i + 500]}).distinct()):
                        changed = True
        # Pointed at *from* the questions, but each history belongs to one
        # question, so it is owned like a child.
        hist = {o.question_history_id for m in (Tossup, Bonus) for o in rows[m].values()
                if o.question_history_id}
        if add(QuestionHistory, QuestionHistory.objects.filter(pk__in=hist)):
            changed = True
        # Comments hang off questions and packets by content type + id.
        for m, ct in commented:
            ids = [str(pk) for pk in rows[m]]
            for i in range(0, len(ids), 500):
                if add(Comment, Comment.objects.filter(content_type=ct, object_pk__in=ids[i:i + 500])):
                    changed = True
        if QuestionHistory not in candidates:
            candidates.append(QuestionHistory)

    # References: whatever the archived rows point at.
    wanted = defaultdict(set)
    for model, objs in list(rows.items()):
        for f, rel in _relations(model):
            if rel not in REFERENCE:
                continue
            for o in objs.values():
                if f.many_to_many:
                    wanted[rel].update(getattr(o, f.name).values_list('pk', flat=True))
                else:
                    v = getattr(o, f.attname)
                    if v is not None:
                        wanted[rel].add(v)
    # The set's own distributions, every category in them, and the
    # distribution of any category a question uses.
    dist_ids = set(wanted[Distribution]) | set(
        Distribution.objects.filter(owner_set=qset).values_list('pk', flat=True))
    dist_ids |= set(DistributionEntry.objects.filter(pk__in=wanted[DistributionEntry])
                    .values_list('distribution_id', flat=True))
    dist_ids.discard(None)
    add(Distribution, Distribution.objects.filter(pk__in=dist_ids))
    add(DistributionEntry, DistributionEntry.objects.filter(
        Q(pk__in=wanted[DistributionEntry]) | Q(distribution_id__in=dist_ids)))
    add(QuestionType, QuestionType.objects.filter(pk__in=wanted[QuestionType]))
    add(RoleGroup, RoleGroup.objects.filter(pk__in=wanted[RoleGroup]))
    return rows, wanted[Writer]


def people(qset, rows, writer_ids):
    """Everyone the archive mentions, with their roles in this set. Writer rows
    themselves aren't archived: they list every set a person is in."""
    members = Writer.objects.filter(
        Q(pk__in=writer_ids) | Q(question_set_writer=qset) | Q(question_set_editor=qset)
        | Q(question_set_proofreader=qset) | Q(co_owned_sets=qset)
        | Q(user_id__in={c.user_id for c in rows.get(Comment, {}).values() if c.user_id})
    ).distinct().select_related('user')
    editors = set(qset.editor.values_list('pk', flat=True))
    writers = set(qset.writer.values_list('pk', flat=True))
    proofreaders = set(qset.proofreader.values_list('pk', flat=True))
    co_owners = set(qset.co_owners.values_list('pk', flat=True))
    out = []
    for w in members.order_by('pk'):
        roles = [name for name, ids in (('co-owner', co_owners), ('editor', editors),
                                        ('writer', writers), ('proofreader', proofreaders))
                 if w.pk in ids]
        if w.pk == qset.owner_id:
            roles.insert(0, 'owner')
        u = w.user
        out.append({'writer_id': w.pk, 'user_id': u.pk if u else None,
                    'username': u.username if u else '',
                    'first_name': u.first_name if u else '', 'last_name': u.last_name if u else '',
                    'roles_in_set': roles})
    return out


def _serialize(model, objs):
    data = serializers.serialize('python', sorted(objs, key=lambda o: o.pk))
    if model is Comment:
        for d in data:
            for field in COMMENT_PRIVATE:
                if field in d['fields']:
                    d['fields'][field] = None
    return data


def _readable_packets(qset):
    """Each packet (and anything unpacketized) as YAPP2, every tag included."""
    tags = defaultdict(list)
    for tag in CategoryTag.objects.filter(question_set=qset):
        item = {'group': tag.group_name or '', 'name': tag.name}
        for pk in tag.tossups.values_list('pk', flat=True):
            tags[('tossup', pk)].append(item)
        for pk in tag.bonuses.values_list('pk', flat=True):
            tags[('bonus', pk)].append(item)
    groups = []
    for p in Packet.objects.filter(question_set=qset).order_by('packet_name', 'pk'):
        groups.append((p.packet_name or 'Packet {0}'.format(p.pk), dict(packet=p)))
    groups.append(('Unpacketized', dict(packet__isnull=True)))
    out = []
    for name, where in groups:
        tus = list(Tossup.objects.filter(question_set=qset, **where)
                   .select_related('category', 'author').order_by('question_number', 'pk'))
        bos = list(Bonus.objects.filter(question_set=qset, **where)
                   .select_related('category', 'author').order_by('question_number', 'pk'))
        if tus or bos:
            out.append((name, yapp_export.packet_to_yapp(tus, bos, version=2, name=name,
                                                         tag_names=tags)))
    return out


README = """QEMS set archive ({format})

Set: {name} (id {id})
Exported {when} by {who}.

manifest.json   what is in here, with a row count per table
people.json     everyone the archive mentions and their roles in this set
                (writer_id / user_id are what the data files point at)
data/           every database row belonging to the set, one file per table,
                in Django's serialization format (model, pk, fields)
readable/       each packet as YAPP2 JSON (questions, answers, metadata, tags)

Question history: data/qsub.questionhistory.json links each question to its
revisions in data/qsub.tossuphistory.json and data/qsub.bonushistory.json.
Comments: data/django_comments.comment.json (object_pk is the tossup, bonus
or packet id; content_type says which), with replies, resolutions, mentions and
anchors in the qsub.comment* files.

Not included: {left_out}.
"""


def build_archive(qset, exported_by=''):
    """The archive as zip bytes, and the manifest."""
    rows, writer_ids = collect(qset)
    when = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    counts = {}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for model in sorted(rows, key=lambda m: m._meta.label_lower):
            objs = list(rows[model].values())
            if not objs:
                continue
            label = model._meta.label_lower
            counts[label] = len(objs)
            zf.writestr('data/{0}.json'.format(label),
                        json.dumps(_serialize(model, objs), cls=DjangoJSONEncoder,
                                   indent=1, ensure_ascii=False))
        ppl = people(qset, rows, writer_ids)
        zf.writestr('people.json', json.dumps(ppl, indent=1, ensure_ascii=False))
        used = set()
        for name, packet in _readable_packets(qset):
            base = re.sub(r'[\\/:*?"<>|]', '_', name).strip() or 'Packet'
            fname, n = base, 2
            while fname in used:
                fname, n = '{0} ({1})'.format(base, n), n + 1
            used.add(fname)
            zf.writestr('readable/{0}.json'.format(fname),
                        json.dumps(packet, indent=1, ensure_ascii=False))
        manifest = {
            'format': FORMAT,
            'exported_at': when.isoformat(),
            'exported_by': exported_by,
            'set': {'id': qset.pk, 'name': qset.name},
            'counts': counts,
            'people': len(ppl),
            'left_out': LEFT_OUT,
        }
        zf.writestr('manifest.json', json.dumps(manifest, indent=1, ensure_ascii=False))
        zf.writestr('README.txt', README.format(
            format=FORMAT, name=qset.name, id=qset.pk, when=when.isoformat(),
            who=exported_by or 'an administrator', left_out='; '.join(LEFT_OUT).lower()))
    return buf.getvalue(), manifest
