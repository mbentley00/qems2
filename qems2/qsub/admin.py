from django.contrib import admin
from .models import *


class WriterAdmin(admin.ModelAdmin):
    """The list is the point: "can create early" is togglable straight from it,
    so lifting the new-account wait for someone is two clicks and a Save."""
    list_display = ('username', 'get_real_name', 'email', 'date_joined',
                    'can_create_early', 'administrator')
    list_editable = ('can_create_early',)
    list_filter = ('can_create_early', 'administrator')
    search_fields = ('user__username', 'user__first_name', 'user__last_name', 'user__email')

    @admin.display(description='Username', ordering='user__username')
    def username(self, writer):
        return writer.user.username

    @admin.display(description='Email', ordering='user__email')
    def email(self, writer):
        return writer.user.email

    @admin.display(description='Signed up', ordering='user__date_joined')
    def date_joined(self, writer):
        return writer.user.date_joined

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('user')


class QuestionSetAdmin(admin.ModelAdmin):
    list_display = ('name', 'date', 'owner', 'public', 'approval_status')
    list_filter = ('approval_status', 'public')
    search_fields = ('name', 'host')


admin.site.register(QuestionSet, QuestionSetAdmin)
admin.site.register(Packet)
admin.site.register(Tossup)
admin.site.register(Bonus)
admin.site.register(Writer, WriterAdmin)
admin.site.register(Distribution)
admin.site.register(DistributionEntry)
admin.site.register(DistributionPerPacket)
admin.site.register(SetWideDistributionEntry)
admin.site.register(QuestionType)
admin.site.register(TieBreakDistributionEntry)
#admin.site.register(Comments)
