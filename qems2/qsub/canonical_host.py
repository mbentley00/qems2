"""Send old or alternate domains to the main one.

Sign-ins are tied to a domain, so serving the site from qems3.buzz and
qems3.com at once would leave people signed in on one and not the other.
Hosts listed in REDIRECT_HOSTS get a permanent redirect to the same path on
BASE_URL. Hosts not listed (the azurewebsites.net host that deploy checks
use, localhost) are served as normal.
"""
from django.conf import settings
from django.http import HttpResponsePermanentRedirect


class CanonicalHostMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.hosts = {h.strip().lower() for h in getattr(settings, 'REDIRECT_HOSTS', []) if h.strip()}
        self.base = settings.BASE_URL.rstrip('/')

    def __call__(self, request):
        if self.hosts:
            host = request.get_host().split(':')[0].lower()
            if host in self.hosts:
                return HttpResponsePermanentRedirect(self.base + request.get_full_path())
        return self.get_response(request)
