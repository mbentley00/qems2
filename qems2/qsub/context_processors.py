from django.conf import settings
from django.db.models import Q


def debug_flag(request):
    """Expose DEBUG to templates (e.g. to show a distinct local-only favicon)."""
    return {'DEBUG': settings.DEBUG}


def _active_set_id(request):
    """Best-effort active question-set id for the current request, inferred from
    the resolved URL kwargs (set-scoped or object-scoped pages)."""
    rm = getattr(request, 'resolver_match', None)
    if not rm:
        return None
    kw = rm.kwargs or {}
    for key in ('qset_id', 'passed_qset_id', 'q_set_id', 'question_set_id'):
        if kw.get(key):
            try:
                return int(kw[key])
            except (TypeError, ValueError):
                return None
    # Object-scoped pages: resolve the owning set with one lightweight query.
    from .models import Packet, Tossup, Bonus
    try:
        if kw.get('packet_id'):
            return Packet.objects.values_list('question_set_id', flat=True).get(id=kw['packet_id'])
        if kw.get('tossup_id'):
            return Tossup.objects.values_list('question_set_id', flat=True).get(id=kw['tossup_id'])
        if kw.get('bonus_id'):
            return Bonus.objects.values_list('question_set_id', flat=True).get(id=kw['bonus_id'])
    except Exception:
        pass
    return None


def nav(request):
    """Sidebar/top-bar navigation context for the redesigned app shell: the
    user's sets, the active set (with per-set counts and the viewer's role), and
    display identity. Returns {} for anonymous users so the splash/login pages
    fall back to their own chrome."""
    if not getattr(request, 'user', None) or not request.user.is_authenticated:
        return {}
    try:
        writer = request.user.writer
    except Exception:
        return {}

    from .models import QuestionSet

    u = request.user
    name = (u.get_full_name() or u.username).strip()
    initials = ''.join(p[0] for p in name.split()[:2]).upper() or u.username[:1].upper()
    ctx = {'nav_display_name': name, 'nav_initials': initials, 'nav_username': u.username}

    sets = (QuestionSet.objects
            .filter(Q(owner=writer) | Q(co_owners=writer) | Q(editor=writer) | Q(writer=writer))
            .distinct().order_by('-date', 'name'))
    ctx['nav_sets'] = sets

    # Set-agnostic pages (the home/all-sets list, distribution editors) aren't
    # scoped to any one set, so don't show the set-specific sidebar there — fall
    # back to the minimal chrome instead of pinning a "remembered" active set.
    rm = getattr(request, 'resolver_match', None)
    view_name = rm.func.__name__ if rm and getattr(rm, 'func', None) else ''
    SET_AGNOSTIC_VIEWS = {
        'main', 'question_sets', 'distributions', 'edit_distribution',
        'edit_tiebreak',
    }

    active = None
    if view_name not in SET_AGNOSTIC_VIEWS:
        active_id = _active_set_id(request)
        if active_id:
            active = sets.filter(id=active_id).first() or QuestionSet.objects.filter(id=active_id).first()
        if active is None and request.session.get('nav_active_set'):
            active = sets.filter(id=request.session['nav_active_set']).first()
        if active is None:
            active = sets.first()
        if active is not None and request.session.get('nav_active_set') != active.id:
            request.session['nav_active_set'] = active.id
    ctx['nav_active'] = active

    if active is not None:
        is_editor = active.is_owner(writer) or writer in active.editor.all()
        ctx['nav_is_editor'] = is_editor
        ctx['nav_role'] = 'Editor' if is_editor else 'Writer'
        ctx['nav_first_packet'] = (active.packet_set.order_by('id')
                                   .values_list('id', flat=True).first())
        try:
            from .views import _new_activity_count
            ctx['nav_new_activity'] = _new_activity_count(writer, active)
        except Exception:
            ctx['nav_new_activity'] = 0
    return ctx
