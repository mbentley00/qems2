from django.conf import settings
from django.db.models import Q


def debug_flag(request):
    """Expose DEBUG to templates (e.g. to show a distinct local-only favicon)."""
    return {'DEBUG': settings.DEBUG}


def news(request):
    """The site-news bar and the sidebar's unread count, both lazy: a template
    calls them only when it shows them, so partial templates rendered for
    AJAX never count as the bar having been seen, and pages that don't show
    the shell never query for news."""
    user = getattr(request, 'user', None)
    if not (user and user.is_authenticated) or request.path.startswith('/news/'):
        return {}
    from . import news as site_news
    memo = {}

    def news_banner():
        if 'banner' not in memo:
            try:
                memo['banner'] = site_news.banner_for(user.writer)
            except Exception:
                memo['banner'] = None
        return memo['banner']

    def news_unread():
        if 'unread' not in memo:
            try:
                memo['unread'] = site_news.unread(user.writer).count()
            except Exception:
                memo['unread'] = 0
        return memo['unread']

    return {'news_banner': news_banner, 'news_unread': news_unread}


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
    ctx = {'nav_display_name': name, 'nav_initials': initials, 'nav_username': u.username,
           # The bug/idea form is only offered when its mail has somewhere to
           # go; a form that quietly drops what people write is worse than none.
           'nav_support_on': bool((getattr(settings, 'SUPPORT_EMAIL', '') or '').strip())}

    sets = (QuestionSet.objects
            .filter(Q(owner=writer) | Q(co_owners=writer) | Q(editor=writer) | Q(writer=writer)
                    | Q(proofreader=writer))
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

    # Where the top-bar search sends you. A new tab keeps the page you searched
    # from, which is what you want from a question you are editing -- but not
    # from the results themselves, where each refinement would leave another
    # tab behind. So the preference stands everywhere except the search pages.
    ctx['nav_on_search_page'] = view_name in ('search', 'quick_search')
    ctx['nav_search_new_tab'] = (bool(writer.search_in_new_tab)
                                 and not ctx['nav_on_search_page'])

    active = None
    if view_name not in SET_AGNOSTIC_VIEWS:
        active_id = _active_set_id(request)
        if active_id:
            active = sets.filter(id=active_id).first() or QuestionSet.objects.filter(id=active_id).first()
        # Some pages carry the set in the query string rather than the path --
        # the search form posts ?qset=<id>, and quick search the same. Without
        # this the sidebar fell back to whatever the session last remembered,
        # so a search scoped to one set sat under a sidebar naming another.
        # Only the viewer's own sets: this one comes from a parameter anyone
        # can type.
        if active is None:
            raw = (request.GET.get('qset') or '').strip()
            if raw.isdigit():
                active = sets.filter(id=int(raw)).first()
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
        # A proofreader reads, comments and suggests; they don't write, so the
        # sidebar offers them no "new question" buttons.
        is_proofreader = not is_editor and active.is_proofreader(writer)
        ctx['nav_is_proofreader'] = is_proofreader
        ctx['nav_role'] = 'Editor' if is_editor else 'Proofreader' if is_proofreader else 'Writer'
        ctx['nav_first_packet'] = (active.packet_set.order_by('id')
                                   .values_list('id', flat=True).first())
        try:
            from .views import _new_activity_count
            ctx['nav_new_activity'] = _new_activity_count(writer, active)
        except Exception:
            ctx['nav_new_activity'] = 0
    return ctx
