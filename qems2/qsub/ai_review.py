"""Automatic AI review of newly submitted questions.

A set whose owner is a superuser can turn on QuestionSet.ai_auto_review. Each
submission view then calls queue_review() with the questions it just created;
once the request's transaction commits, a background thread sends them to
Claude (ai.review_questions, Sonnet) for a grammar, style and fact check and
stores what comes back as AIGrammarFinding rows with source='auto'. They show
in the AI review list on the set's Style Check page.

The submission never waits on the model: the writer's page returns at once and
the findings appear a minute or so later. A failed review is stored as a
finding of kind 'error', so the admin sees it rather than it vanishing (the
production app keeps no runtime logs).
"""

import html
import threading

from django.db import close_old_connections, connection, transaction

AUTO_KINDS = ('grammar', 'style', 'fact')


def _text(value):
    return html.unescape(value or '').strip()


def question_text(q, qtype):
    """One question as the reviewer reads it: prose plus answer lines, markup
    kept so the model can see what is underlined and italicised."""
    if qtype == 'tossup':
        parts = [_text(q.tossup_text), 'ANSWER: ' + _text(q.tossup_answer)]
    else:
        parts = [_text(q.leadin)]
        for n in (1, 2, 3):
            body = _text(getattr(q, 'part{0}_text'.format(n)))
            answer = _text(getattr(q, 'part{0}_answer'.format(n)))
            if body or answer:
                parts += ['[{0}] {1}'.format(n, body), 'ANSWER: ' + answer]
    return '\n'.join(p for p in parts if p.strip())


def queue_review(qset, tossups=(), bonuses=(), writer=None):
    """Review these new questions in the background if the set asks for it.

    Cheap no-op when the set has the review off or no API key is configured,
    so every submission path can call it unconditionally."""
    from . import ai
    if not getattr(qset, 'ai_auto_review', False) or not ai.ai_enabled():
        return
    refs = [('tossup', t.id) for t in tossups if t.id] + \
           [('bonus', b.id) for b in bonuses if b.id]
    if not refs:
        return
    writer_id = writer.id if writer is not None else None
    # After commit: the thread opens its own connection and must see the rows.
    transaction.on_commit(lambda: _start(qset.id, refs, writer_id))


def _start(qset_id, refs, writer_id):
    threading.Thread(target=_run, args=(qset_id, refs, writer_id), daemon=True,
                     name='ai-review-{0}'.format(qset_id)).start()


def _run(qset_id, refs, writer_id):
    close_old_connections()
    try:
        run_review(qset_id, refs, writer_id)
    except Exception as ex:  # never let a background thread die silently
        try:
            _store_error(qset_id, refs, writer_id, 'The AI review crashed: {0}'.format(ex))
        except Exception:
            pass
    finally:
        connection.close()


def run_review(qset_id, refs, writer_id=None):
    """Review the given (qtype, id) questions now and store the findings.
    Returns the number of findings stored."""
    from . import ai
    from .models import AIGrammarFinding, Bonus, QuestionSet, Tossup

    qset = QuestionSet.objects.filter(id=qset_id).first()
    if qset is None:
        return 0
    models = {'tossup': Tossup, 'bonus': Bonus}
    items, known = [], set()
    for qtype, qid in refs:
        q = models[qtype].objects.filter(id=qid, question_set=qset).first()
        if q is None:
            continue
        ref = '{0}-{1}'.format(qtype, qid)
        known.add(ref)
        items.append({'ref': ref, 'text': question_text(q, qtype)})
    if not items:
        return 0

    findings, error = ai.review_questions(items)
    stored = 0
    for f in findings:
        ref = f.get('ref', '')
        kind = f.get('kind', '')
        if ref not in known or kind not in AUTO_KINDS:
            continue  # model output is checked, not trusted
        qtype, _, qid = ref.partition('-')
        AIGrammarFinding.objects.create(
            question_set=qset, question_type=qtype, question_id=int(qid),
            kind=kind, source='auto',
            severity='error' if f.get('severity') == 'error' else 'warning',
            excerpt=f.get('excerpt', ''), suggestion=f.get('suggestion', ''),
            explanation=f.get('explanation', ''), created_by_id=writer_id)
        stored += 1
    if error:
        _store_error(qset_id, refs, writer_id, error)
    return stored


def _store_error(qset_id, refs, writer_id, message):
    from .models import AIGrammarFinding
    qtype, qid = refs[0]
    AIGrammarFinding.objects.create(
        question_set_id=qset_id, question_type=qtype, question_id=qid,
        kind='error', source='auto', severity='error',
        explanation='{0} ({1} question(s) in this submission.)'.format(message, len(refs)),
        created_by_id=writer_id)
