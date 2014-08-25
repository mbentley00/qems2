from django.conf.urls import patterns, include, url
from django.contrib.auth.views import logout, login
from django.views.generic import ListView
from qsub.views import *
from qsub.models import *

import django

# Uncomment the next two lines to enable the admin:
from django.contrib import admin
admin.autodiscover()

urlpatterns = patterns('',
    # Examples:
    # url(r'^$', 'QuEST.views.home', name='home'),
    # url(r'^QuEST/', include('QuEST.foo.urls')),

    # Uncomment the admin/doc line below to enable admin documentation:
    # url(r'^admin/doc/', include('django.contrib.admindocs.urls')),

    # Uncomment the next line to enable the admin:
    url(r'^admin/', include(admin.site.urls)),
    (r'^main/$', main),
    (r'^$', main),
    (r'^register/$', register),
    (r'^accounts/login/$', django.contrib.auth.views.login),
    (r'^accounts/logout/$', logout),
    (r'^question_sets/$', question_sets),
    (r'^create_question_set/$', create_question_set),
    (r'^edit_question_set/(?P<qset_id>[0-9]+)/$', edit_question_set),
    (r'^distributions/$', distributions),
    (r'^add_editor/(?P<qset_id>[0-9]+)/$', add_editor),
    (r'^add_writer/(?P<qset_id>[0-9]+)/$', add_writer),
    (r'^edit_distribution/(?P<dist_id>[0-9]+)/$', edit_distribution),
    (r'^edit_distribution/$', edit_distribution),
    (r'^add_tossups/(?P<qset_id>[0-9]+)/$', add_tossups),
    (r'^edit_tossup/(?P<tossup_id>[0-9]+)/$', edit_tossup),
    (r'^delete_tossup/(?P<tossup_id>[0-9]+)/$', delete_tossup),
    (r'^add_bonuses/(?P<qset_id>[0-9]+)/$', add_bonuses),
    (r'^edit_bonus/(?P<bonus_id>[0-9]+)/$', edit_bonus),
    (r'^delete_bonus/(?P<bonus_id>[0-9]+)/$', delete_bonus),
    (r'^add_packets/(?P<qset_id>[0-9]+)/$', add_packets),
    (r'^edit_packet/(?P<packet_id>[0-9]+)/$', edit_packet),
    (r'^delete_packet/(?P<packet_id>[0-9]+)/$', delete_packet),
)
