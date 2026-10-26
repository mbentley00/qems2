"""Download a set's full archive (see full_export). Site admins only for now."""

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404

from .full_export import build_archive
from .models import QuestionSet


@login_required
def full_export_set(request, qset_id):
    if not request.user.is_superuser:
        return HttpResponseForbidden('Only site administrators can download a full archive.')
    qset = get_object_or_404(QuestionSet, id=qset_id)
    data, _ = build_archive(qset, exported_by=request.user.username)
    name = ''.join(c if c.isalnum() or c in ' -_' else '_' for c in (qset.name or 'Set')).strip() or 'Set'
    response = HttpResponse(data, content_type='application/zip')
    response['Content-Disposition'] = 'attachment; filename="{0} - full archive.zip"'.format(name)
    return response
