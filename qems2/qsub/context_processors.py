from django.conf import settings


def debug_flag(request):
    """Expose DEBUG to templates (e.g. to show a distinct local-only favicon)."""
    return {'DEBUG': settings.DEBUG}
