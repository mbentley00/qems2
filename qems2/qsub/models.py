from __future__ import unicode_literals

from django.db import models
from django.contrib.auth.models import User
from django.db.models.signals import post_save
from allauth.account.signals import password_changed
from django.dispatch import receiver
from django.contrib import messages

from datetime import datetime
from django.utils import timezone
import json

from collections import OrderedDict
from .utils import *

# Create your models here.

CATEGORIES = (('S-P', 'Science - physics'),
              ('S-C', 'Science - chemistry'),
              ('S-B', 'Science - biology'),
              ('S-O', 'Science - other'),
              ('L-AM', 'Literature - American'),
              ('L-EU', 'Literature - European'),
              ('L-BR', 'Literature - British'),
              ('L-W', 'Literature - World'),
              ('H-AM', 'History - American'),
              ('H-EU', 'History - European'),
              ('H-W', 'History - World'),
              ('R', 'Religion'),
              ('M', 'Myth'),
              ('P', 'Philosophy'),
              ('FA', 'Fine arts'),
              ('SS', 'Social science'),
              ('G', 'Geography'),
              ('O', 'Other'),
              ('PC', 'Pop culture'))

RELIGION_SUBTYPES = (('R-J', 'Religion - Judaism'),
                     ('R-C', 'Religion - Christianity'),
                     ('R-B', 'Religion - Buddhism'),
                     ('R-H', 'Religion - Hinduism'),
                     ('R-I', 'Religion - Islam'),
                     ('R-O', 'Religion - Other'),)

MYTH_SUBTYPES = (('M-GR', 'Myth - Greek'),
                 ('M-R', 'Myth - Roman'),
                 ('M-N', 'Myth - Norse'),
                 ('M-BR', 'Myth - British Isles'),
                 ('M-EE', 'Myth - Eastern Europe'),
                 ('M-IN', 'Myth - India'),
                 ('M-CH', 'Myth - China'),
                 ('M-JP', 'Myth - Japan'),
                 ('M-O', 'Myth - Other'),)

PHILOSOPHY_SUBTYPES = (('P-AN', 'Philosophy - Analytic'),
                       ('P-CO', 'Philosophy - Continental'),
                       ('P-EN', 'Philosophy - Enlightenment'),
                       ('P-CL', 'Philosophy - Classical'),
                       ('P-O', 'Philosophy - Other'),)

FINE_ARTS_SUBTYPES = (('FA-M', 'Fine arts - Music'),
                      ('FA-SC', 'Fine arts - Sculpture'),
                      ('FA-OP', 'Fine arts - Opera'),
                      ('FA-F', 'Fine arts - Film'),
                      ('FA-P', 'Fine arts - Painting'),
                      ('FA-AR', 'Fine arts - Architecture'),
                      ('FA-O', 'Fine arts - Other'),)

SOCIAL_SCIENCE_SUBTYPES = (('SS-SOC', 'Social science - Sociology'),
                           ('SS-EC', 'Social science - Economics'),
                           ('SS-PS', 'Social science - Psychology'),
                           ('SS-O', 'Social science - Other'),)

LIT_SUBTYPES = (('L-PL', 'Literature - play'),
                ('L-PO', 'Literature - poem'),
                ('L-NO', 'Literature - novel'),
                ('L-CR', 'Literature - criticism'),
                ('L-O', 'Literature - other'),)

SCIENCE_SUBTYPES = (('S-P-QM', 'Science - physics - quantum mechanics'),
                    ('S-P-SM', 'Science - physics - statistical mechanics'),
                    ('S-P-M', 'Science - physics - classical mechanics'),
                    ('S-P-R', 'Science - physics - relativity'),
                    ('S-P-MP', 'Science - physics - mathematical physics'),
                    ('S-P-EM', 'Science - physics - electrodynamics'),
                    ('S-P-SS', 'Science - physics - solid state'),
                    ('S-P-MSC', 'Science - physics - miscellaneous'),
                    ('S-C-O', 'Science - chemistry - organic'),
                    ('S-C-P', 'Science - chemistry - physical'),
                    ('S-C-B', 'Science - chemistry - biochem'),
                    ('S-C-MSC', 'Science - chemistry - miscellaneous'),
                    ('S-B-C', 'Science - biology - biochem'),
                    ('S-B-G', 'Science - biology - genetics'),
                    ('S-B-E', 'Science - biology - evolutionary bio'),
                    ('S-B-MSC', 'Science - biology - miscellaneous'),
                    ('S-O-A', 'Science - other - astronomy'),
                    ('S-O-M', 'Science - other - mathematics'),
                    ('S-O-CS', 'Science - other - computer science'),
                    ('S-O-ENG', 'Science - other - engineering'),
                    ('S-O-ES', 'Science - other - earth science'),)

ACF_DISTRO = OrderedDict([('S', (5, 5)),
                          ('L', (5, 5)),
                          ('H', (5, 5)),
                          ('R', (1, 1)),
                          ('M', (1, 1)),
                          ('P', (1, 1)),
                          ('FA', (3, 3)),
                          ('SS', (1, 1)),
                          ('G', (1, 1)),
                          ('PC', (1, 1))])

class Writer (models.Model):

    user = models.OneToOneField(User, on_delete=models.CASCADE)

    question_set_writer = models.ManyToManyField('QuestionSet', related_name='writer')
    question_set_editor = models.ManyToManyField('QuestionSet', related_name='editor')

    administrator = models.BooleanField(default=False)

    send_mail_on_comments = models.BooleanField(default=False)

    def get_real_name(self):
        return '{0!s} {1!s} '.format(self.user.first_name, self.user.last_name)
        
    def get_last_name(self):
        return self.user.last_name

    def __str__(self):
        return '{0!s} {1!s} ({2!s})'.format(self.user.first_name, self.user.last_name, self.user.username)

class QuestionSet (models.Model):
    name = models.CharField(max_length=200)
    date = models.DateField()
    host = models.CharField(max_length=200)
    address = models.TextField(max_length=200)
    owner = models.ForeignKey('Writer', on_delete=models.CASCADE, related_name='owner')
    co_owners = models.ManyToManyField('Writer', related_name='co_owned_sets', blank=True)
    # When public, the set is listed for all logged-in users, who can request to
    # join it (which emails the owner). It does not grant any access by itself.
    public = models.BooleanField(default=False)
    # Comma-separated style-check rule codes turned off for this set (e.g. a team
    # that allows contractions). Empty = run every rule the guide enables.
    disabled_style_rules = models.TextField(blank=True, default='')
    num_packets = models.PositiveIntegerField()
    distribution = models.ForeignKey('Distribution', on_delete=models.CASCADE) # TODO: This needs to be deleted eventually
    #teams = models.ForeignKey('Team')
    #tiebreak_dist = models.ForeignKey('TieBreakDistribution')
    max_acf_tossup_length = models.PositiveIntegerField(default=725)
    max_acf_bonus_length = models.PositiveIntegerField(default=650)
    max_vhsl_bonus_length = models.PositiveIntegerField(default=100)
    char_count_ignores_pronunciation_guides = models.BooleanField(default=True)

    # When true, this is a tossup-only tournament: bonuses are not expected and
    # bonus requirements/UI are suppressed.
    tossups_only = models.BooleanField(default=False)

    # Regular (non-tiebreaker) questions per packet, used by auto-packetization
    tossups_per_packet = models.PositiveIntegerField(default=20)
    bonuses_per_packet = models.PositiveIntegerField(default=20)

    class Admin: pass

    def is_owner(self, writer):
        """True if the writer is the primary owner or a co-owner of this set."""
        if writer is None:
            return False
        return writer == self.owner or self.co_owners.filter(id=writer.id).exists()

    def all_owners(self):
        """The primary owner followed by any co-owners."""
        return [self.owner] + list(self.co_owners.all())

    def disabled_style_rule_set(self):
        """Style-check rule codes turned off for this set."""
        return set(c for c in (self.disabled_style_rules or '').split(',') if c)

    def __str__(self):
        return '{0!s}'.format(self.name)

class Role(models.Model):

    writer = models.ForeignKey(Writer, on_delete=models.CASCADE)
    question_set = models.ForeignKey(QuestionSet, on_delete=models.CASCADE)
    category = models.CharField(max_length=500)
    can_view_others = models.BooleanField(default=False)
    can_edit_others = models.BooleanField(default=False)

class Packet (models.Model):
    packet_name = models.CharField(max_length=200)
    date_submitted = models.DateField(auto_now_add=True)
    # authors = models.ManyToManyField(Player)
    question_set = models.ForeignKey(QuestionSet, on_delete=models.CASCADE)
    #team = models.ForeignKey(Team)
    # Explicit display order set by the user (drag-to-reorder on the packet
    # grid). When null, packets fall back to a natural sort by name.
    sort_order = models.PositiveIntegerField(null=True, blank=True)

    created_by = models.ForeignKey(Writer, on_delete=models.CASCADE, related_name='packet_creator')

    def __str__(self):
        return '{0!s}'.format(self.packet_name)

class PacketGridLog(models.Model):
    """A change made on the packet grid (move/swap/reorder), recorded with the
    prior state of each affected question (as JSON) so it can be undone."""
    question_set = models.ForeignKey(QuestionSet, on_delete=models.CASCADE)
    changer = models.ForeignKey(Writer, on_delete=models.SET_NULL, null=True,
                                related_name='packet_grid_changes')
    change_date = models.DateTimeField(auto_now_add=True)
    description = models.TextField()
    # JSON list of prior states: [{"qtype","id","packet_id","number"}, ...]
    undo_data = models.TextField(default='[]')
    undone = models.BooleanField(default=False)

    def __str__(self):
        return '{0!s}: {1!s}'.format(self.change_date, self.description)

class PacketSlotVacancy(models.Model):
    """The category of the question most recently removed from a packet slot, so
    the now-empty grid cell can hint what used to fill it. One row per
    (packet, number, type); overwritten on each removal and ignored while the
    slot is filled."""
    question_set = models.ForeignKey(QuestionSet, on_delete=models.CASCADE)
    packet = models.ForeignKey(Packet, on_delete=models.CASCADE)
    question_number = models.PositiveIntegerField()
    question_type = models.CharField(max_length=10)  # 'tossup' or 'bonus'
    category = models.TextField(blank=True, default='')

    class Meta:
        unique_together = ('packet', 'question_number', 'question_type')

class StyleIssueDismissal(models.Model):
    """Records that a style-check issue was dismissed for a question, so it is
    hidden on future runs. Identified by (question, rule code, token) where the
    token distinguishes issues of the same rule (e.g. the term for a missing
    pronunciation guide, or the field label for a mechanical issue)."""
    question_set = models.ForeignKey(QuestionSet, on_delete=models.CASCADE)
    question_type = models.CharField(max_length=10)  # 'tossup' | 'bonus'
    question_id = models.PositiveIntegerField()
    code = models.CharField(max_length=40)
    token = models.CharField(max_length=255, blank=True, default='')
    dismissed_by = models.ForeignKey(Writer, on_delete=models.SET_NULL, null=True)
    dismissed_date = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('question_type', 'question_id', 'code', 'token')

    def __str__(self):
        return 'dismissed {0} {1}/{2} [{3}]'.format(
            self.question_type, self.question_id, self.code, self.token)


class StyleRuleDismissal(models.Model):
    """A set-wide style dismissal: hides every example of one style suggestion
    (rule code + token, e.g. a pronunciation guide for a given term) across the
    whole question set, including questions written later."""
    question_set = models.ForeignKey(QuestionSet, on_delete=models.CASCADE)
    code = models.CharField(max_length=40)
    token = models.CharField(max_length=255, blank=True, default='')
    dismissed_by = models.ForeignKey(Writer, on_delete=models.SET_NULL, null=True)
    dismissed_date = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('question_set', 'code', 'token')

    def __str__(self):
        return 'set-wide dismissed {0}/{1} [{2}]'.format(
            self.question_set_id, self.code, self.token)


class RoleGroup(models.Model):
    """A named group of writers (e.g. 'PACE Editor'). A group can be attached to
    a question set with a role; every member then holds that role on the set.
    Membership is live: members added later gain the role on all sets the group
    is attached to, and members removed lose it."""
    name = models.CharField(max_length=120, unique=True)
    members = models.ManyToManyField('Writer', related_name='role_groups', blank=True)
    created_by = models.ForeignKey('Writer', on_delete=models.SET_NULL, null=True,
                                   related_name='created_role_groups')
    created_date = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

    def can_manage(self, writer):
        return (writer is not None and
                (self.created_by_id == writer.id or
                 (writer.user and writer.user.is_superuser)))


class RoleGroupJoinRequest(models.Model):
    """A pending request from a writer to join a role group. Created when someone
    clicks "Request to join"; removed when a manager approves (the writer becomes
    a member) or declines, or when the writer is added by other means."""
    role_group = models.ForeignKey(RoleGroup, on_delete=models.CASCADE,
                                   related_name='join_requests')
    requester = models.ForeignKey('Writer', on_delete=models.CASCADE,
                                  related_name='role_group_join_requests')
    created_date = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('role_group', 'requester')
        ordering = ['created_date']

    def __str__(self):
        return 'join request: writer {0} -> group {1}'.format(
            self.requester_id, self.role_group_id)


class SetRoleGroupAssignment(models.Model):
    """Attaches a RoleGroup to a QuestionSet with a role (editor or writer)."""
    ROLE_CHOICES = (('editor', 'Editor'), ('writer', 'Writer'))
    question_set = models.ForeignKey(QuestionSet, on_delete=models.CASCADE,
                                     related_name='role_group_assignments')
    role_group = models.ForeignKey(RoleGroup, on_delete=models.CASCADE,
                                   related_name='set_assignments')
    role = models.CharField(max_length=10, choices=ROLE_CHOICES)

    class Meta:
        unique_together = ('question_set', 'role_group')

    def __str__(self):
        return '{0} -> set {1} as {2}'.format(self.role_group_id, self.question_set_id, self.role)


class GroupRoleGrant(models.Model):
    """Provenance marker: a (writer, set, role) membership that exists because of
    a role group, so reconciliation can revoke it without touching members who
    were assigned directly."""
    question_set = models.ForeignKey(QuestionSet, on_delete=models.CASCADE)
    writer = models.ForeignKey('Writer', on_delete=models.CASCADE)
    role = models.CharField(max_length=10)

    class Meta:
        unique_together = ('question_set', 'writer', 'role')

    def __str__(self):
        return 'group-granted {0} to writer {1} on set {2}'.format(
            self.role, self.writer_id, self.question_set_id)


class SuggestedEdit(models.Model):
    """A proposed change to one field of a question (track-changes style). Any
    set member can suggest, even on questions they didn't write; the question's
    author or an editor accepts or rejects each one."""
    STATUS_CHOICES = (('pending', 'Pending'), ('accepted', 'Accepted'),
                      ('rejected', 'Rejected'), ('superseded', 'Superseded'))
    question_set = models.ForeignKey(QuestionSet, on_delete=models.CASCADE)
    question_type = models.CharField(max_length=10)  # 'tossup' | 'bonus'
    question_id = models.PositiveIntegerField()
    field = models.CharField(max_length=40)          # e.g. 'tossup_text'
    field_label = models.CharField(max_length=60)
    old_value = models.TextField(blank=True, default='')
    new_value = models.TextField(blank=True, default='')
    note = models.CharField(max_length=255, blank=True, default='')
    suggested_by = models.ForeignKey(Writer, on_delete=models.SET_NULL, null=True,
                                     related_name='suggested_edits')
    created_date = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=10, default='pending', choices=STATUS_CHOICES)
    resolved_by = models.ForeignKey(Writer, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='resolved_suggestions')
    resolved_date = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['created_date']

    def __str__(self):
        return 'suggested {0} on {1} {2} ({3})'.format(
            self.field, self.question_type, self.question_id, self.status)


class AIGrammarFinding(models.Model):
    """A grammar/spelling suggestion produced by the AI grammar check for one
    question in a set. Persisted so the results survive a page reload; a whole
    set's findings are replaced on each rerun, and a single finding disappears
    when dismissed."""
    question_set = models.ForeignKey(QuestionSet, on_delete=models.CASCADE,
                                     related_name='ai_grammar_findings')
    question_type = models.CharField(max_length=10)  # 'tossup' | 'bonus'
    question_id = models.PositiveIntegerField()
    severity = models.CharField(max_length=10, default='warning')  # 'error' | 'warning'
    excerpt = models.TextField(blank=True, default='')
    suggestion = models.TextField(blank=True, default='')
    explanation = models.TextField(blank=True, default='')
    created_by = models.ForeignKey(Writer, on_delete=models.SET_NULL, null=True)
    created_date = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']

    def __str__(self):
        return 'ai grammar finding on {0} {1}'.format(
            self.question_type, self.question_id)


class DistributionPerPacket(models.Model):

    #packet = models.ManyToManyField(Packet)

    question_set = models.ManyToManyField(QuestionSet)
    category = models.CharField(max_length=10, choices=CATEGORIES)
    subcategory = models.CharField(max_length=10)
    num_tossups = models.PositiveIntegerField()
    num_bonuses = models.PositiveIntegerField()
    
    def __str__(self):
        return str("Distribution total for " + str(self.question_set))     

class Distribution(models.Model):

    name = models.CharField(max_length=100)
    acf_tossup_per_period_count = models.PositiveIntegerField(default=20)
    acf_bonus_per_period_count = models.PositiveIntegerField(default=20)
    vhsl_bonus_per_period_count = models.PositiveIntegerField(default=0)
    # Who created this distribution. Lets the creator view/edit it before it's
    # attached to any of their question sets (a brand-new distribution belongs
    # to no set yet, so set-membership alone wouldn't grant access).
    created_by = models.ForeignKey('Writer', null=True, blank=True,
                                   on_delete=models.SET_NULL,
                                   related_name='created_distributions')

    def __str__(self):
        return '{0!s}'.format(self.name)

# This class represents a category (i.e. History - European - British)
# It contains no distribution or set specific information
class CategoryEntry(models.Model):
    category_name = models.CharField(max_length=200)
    sub_category_name = models.CharField(max_length=200, null=True)
    sub_sub_category_name = models.CharField(max_length=200, null=True)
    category_type = models.CharField(max_length=200) # i.e. "Category", "SubCategory" or "SubSubCategory"
    
    def __str__(self):
        if (self.sub_sub_category_name is not None):
            return '{0!s} - {1!s} - {2!s}'.format(self.category_name, self.sub_category_name, self.sub_sub_category_name)
        elif (self.sub_category_name is not None):
            return '{0!s} - {1!s}'.format(self.category_name, self.sub_category_name)
        else:
            return '{0!s}'.format(self.category_name)

# This class links a Category Entry to a specific distribution.
# It contains data on how many questions per period this category entry
# should have.
class CategoryEntryForDistribution (models.Model):
    distribution = models.ForeignKey(Distribution, on_delete=models.CASCADE)
    category_entry = models.ForeignKey(CategoryEntry, on_delete=models.CASCADE)
    
    # Min/max questions of this type for one period
    # i.e. 2.2, which means between 2 and 3 weighted towards 2
    acf_tossup_fraction = models.DecimalField(null=True, max_digits=5, decimal_places=1)
    acf_bonus_fraction = models.DecimalField(null=True, max_digits=5, decimal_places=1)
    vhsl_bonus_fraction = models.DecimalField(null=True, max_digits=5, decimal_places=1)

    # Min/max questions of all types in one period for this category      
    min_total_questions_in_period = models.PositiveIntegerField(null=True)
    max_total_questions_in_period = models.PositiveIntegerField(null=True)
        
    def get_acf_tossup_integer(self):
        return int(self.acf_tossup_fraction)
        
    def get_acf_tossup_remainder(self):
        return round(self.acf_tossup_fraction - self.get_acf_tossup_integer(), 3)
    
    # Returns the maximum number of acf tossups based on the fraction.  For instance,
    # if you have a fraction of 4, it's 4.  If it's 4.2, it's 5 (since some packets can
    # legally have 5 tossups).
    def get_acf_tossup_upper_bound(self):
        if (self.get_acf_tossup_remainder() > 0):
            return self.get_acf_tossup_integer() + 1
        else:
            return self.get_acf_tossup_integer()

    def get_acf_bonus_integer(self):
        return int(self.acf_bonus_fraction)
        
    def get_acf_bonus_remainder(self):
        return round(self.acf_bonus_fraction - self.get_acf_bonus_integer(), 3)

    def get_acf_bonus_upper_bound(self):
        if (self.get_acf_bonus_remainder() > 0):
            return self.get_acf_bonus_integer() + 1
        else:
            return self.get_acf_bonus_integer()

    def get_vhsl_bonus_integer(self):
        return int(self.vhsl_bonus_fraction)
        
    def get_vhsl_bonus_remainder(self):
        return round(self.vhsl_bonus_fraction - self.get_vhsl_bonus_integer(), 3)

    def get_vhsl_bonus_upper_bound(self):
        if (self.get_vhsl_bonus_remainder() > 0):
            return self.get_vhsl_bonus_integer() + 1
        else:
            return self.get_vhsl_bonus_integer()

    def __str__(self):
        return str(self.category_entry)

# This class corresponds to all periods of this type in the set.  For instance,
# you'd have 10 ACFTossupBonusPeriods corresponding to this one PeriodWideEntry,
# and 10 ACFTossupBonusTiebreakerPeriods corresponding to a different PeriodWideEntry
class PeriodWideEntry (models.Model):
    period_type = models.CharField(max_length=200) # i.e. "ACF Regular Period"
    question_set = models.ForeignKey(QuestionSet, on_delete=models.CASCADE)
    distribution = models.ForeignKey(Distribution, on_delete=models.CASCADE)

    # Current number of questions across all categories
    acf_tossup_cur = models.PositiveIntegerField(default=0) 
    acf_bonus_cur = models.PositiveIntegerField(default=0) 
    vhsl_bonus_cur = models.PositiveIntegerField(default=0)
    
    # Total needed number of questions across all categories
    acf_tossup_total = models.PositiveIntegerField(null=True) 
    acf_bonus_total = models.PositiveIntegerField(null=True) 
    vhsl_bonus_total = models.PositiveIntegerField(null=True)
    
    def reset_current_values(self):
        self.acf_tossup_cur = 0  
        self.acf_bonus_cur = 0
        self.vhsl_bonus_cur = 0
        self.save()
        
    def reset_total_values(self):
        self.acf_tossup_total = 0
        self.acf_bonus_total = 0
        self.vhsl_bonus_total = 0
        self.save()

    def __str__(self):
        return str(self.period_type) + ' for ' + str(self.question_set)

# A period is a part of a packet.  For instance, it might be the regular tossup/bonus
# part of an mACF set.  It could also be the VHSL bonus round or a tiebreaker period.
class Period (models.Model):
    name = models.CharField(max_length=200) # i.e. "VHSL Tossup Period 1"
    packet = models.ForeignKey(Packet, on_delete=models.CASCADE)
    period_wide_entry = models.ForeignKey(PeriodWideEntry, on_delete=models.CASCADE)
    
    acf_tossup_cur = models.PositiveIntegerField(default=0) 
    acf_bonus_cur = models.PositiveIntegerField(default=0) 
    vhsl_bonus_cur = models.PositiveIntegerField(default=0)
 
    def reset_current_values(self):
        self.acf_tossup_cur = 0  
        self.acf_bonus_cur = 0
        self.vhsl_bonus_cur = 0
        self.save()
        
    def __str__(self):
        return str(self.name) 

# This class tracks the requirements for a particular category across all periods of this type
# in the set.  For instance, this might track how many History questions have currently been written
# and are still needed for the tiebreaker rounds in an ACF tournament.
class PeriodWideCategoryEntry(models.Model):
    period_wide_entry = models.ForeignKey(PeriodWideEntry, on_delete=models.CASCADE)
    category_entry_for_distribution = models.ForeignKey(CategoryEntryForDistribution, on_delete=models.CASCADE, null=True)
        
    # Current number of tossups/bonuses across all periods (with this distribution) for this category
    acf_tossup_cur_across_periods = models.PositiveIntegerField(default=0) 
    acf_bonus_cur_across_periods = models.PositiveIntegerField(default=0) 
    vhsl_bonus_cur_across_periods = models.PositiveIntegerField(default=0)    
    
    # Total expected number of tossups/bonuses across all periods (with this distribution) for this category
    acf_tossup_total_across_periods = models.PositiveIntegerField(null=True) 
    acf_bonus_total_across_periods = models.PositiveIntegerField(null=True) 
    vhsl_bonus_total_across_periods = models.PositiveIntegerField(null=True)
            
    def reset_current_values(self):
        self.acf_tossup_cur_across_periods = 0
        self.acf_bonus_cur_across_periods = 0
        self.vhsl_bonus_cur_across_periods = 0
        self.save()
        
    def reset_total_values(self):
        self.acf_tossup_total_across_periods = 0
        self.acf_bonus_total_across_periods = 0
        self.vhsl_bonus_total_across_periods = 0
        self.save()
        
    def get_category_type(self):
        return self.category_entry_for_distribution.category_entry.category_type

    def __str__(self):
        return 'Period-Wide {0!s}'.format(str(self.category_entry_for_distribution))

# This class tracks the requirements for a particular category in one period.
# For instance, it could track how many literature questions are needed in
# the VHSL bonus round (i.e. second period) of Round 5 of a tournament.
class OnePeriodCategoryEntry(models.Model):
    period = models.ForeignKey(Period, on_delete=models.CASCADE)
    period_wide_category_entry = models.ForeignKey(PeriodWideCategoryEntry, on_delete=models.CASCADE)
    
    # Current number of tossups/bonuses in this period for this category
    acf_tossup_cur_in_period = models.PositiveIntegerField(default=0) 
    acf_bonus_cur_in_period = models.PositiveIntegerField(default=0) 
    vhsl_bonus_cur_in_period = models.PositiveIntegerField(default=0)

    def get_linked_category_entry_for_distribution(self):
        return self.period_wide_category_entry.category_entry_for_distribution
        
    def get_total_questions_all_types(self):
        return self.acf_tossup_cur_in_period + self.acf_bonus_cur_in_period + self.vhsl_bonus_cur_in_period
        
    def is_under_max_total_questions_limit(self):
        max_total_questions = self.get_linked_category_entry_for_distribution().max_total_questions_in_period
        return (self.get_total_questions_all_types() <= max_total_questions)
        
    def is_over_min_total_questions_limit(self):
        min_total_questions = self.get_linked_category_entry_for_distribution().min_total_questions_in_period
        return (self.get_total_questions_all_types() >= min_total_questions)
        
    # TODO: This method should probably be renamed "is_over_max_acf_tossup_limit"
    def is_over_min_acf_tossup_limit(self):
        return (self.acf_tossup_cur_in_period > self.get_linked_category_entry_for_distribution().get_acf_tossup_upper_bound())

    def is_over_min_acf_bonus_limit(self):
        return (self.acf_bonus_cur_in_period > self.get_linked_category_entry_for_distribution().get_acf_bonus_upper_bound())

    def is_over_min_vhsl_bonus_limit(self):
        return (self.vhsl_bonus_cur_in_period > self.get_linked_category_entry_for_distribution().get_vhsl_bonus_upper_bound())

    def reset_current_values(self):
        self.acf_tossup_cur_in_period = 0
        self.acf_bonus_cur_in_period = 0
        self.vhsl_bonus_cur_in_period = 0
        self.save()
    
    def __str__(self):
        return 'Period {0!s}'.format(str(self.get_linked_category_entry_for_distribution()))


class TieBreakDistribution(models.Model):

    name = models.CharField(max_length=100)

    def __str__(self):
        return '{0!s}'.format(self.name)

# TODO: This should be deleted eventually
class DistributionEntry(models.Model):

    distribution = models.ForeignKey(Distribution, on_delete=models.CASCADE)
    category = models.TextField()
    subcategory = models.TextField()
    min_tossups = models.PositiveIntegerField(null=True)
    min_bonuses = models.PositiveIntegerField(null=True)
    max_tossups = models.PositiveIntegerField(null=True)
    max_bonuses = models.PositiveIntegerField(null=True)

    #fin_tossups = models.CharField(max_length=500, null=True)
    #fin_bonuses = models.CharField(max_length=500, null=True)

    def __str__(self):
        return '{0!s} - {1!s}'.format(self.category, self.subcategory)

# TODO: This should be deleted eventually
class TieBreakDistributionEntry(models.Model):

    question_set = models.ForeignKey(QuestionSet, on_delete=models.CASCADE)
    dist_entry = models.ForeignKey(DistributionEntry, on_delete=models.CASCADE)
    num_tossups = models.PositiveIntegerField(null=True)
    num_bonuses = models.PositiveIntegerField(null=True)

    def __str__(self):
        return '{0!s} - {1!s}'.format(self.dist_entry.category, self.dist_entry.subcategory)

# TODO: This should be deleted eventually
class SetWideDistributionEntry(models.Model):

    question_set = models.ForeignKey(QuestionSet, on_delete=models.CASCADE)
    dist_entry = models.ForeignKey(DistributionEntry, on_delete=models.CASCADE)
    num_tossups = models.PositiveIntegerField()
    num_bonuses = models.PositiveIntegerField()

    def __str__(self):
        return '{0!s} - {1!s}'.format(self.dist_entry.category, self.dist_entry.subcategory)

# Per-packet quota for a category path (e.g. "History" or "History - American")
# used by auto-packetization.  Values are per-packet and may be fractional:
# min/max of 1.5 tossups and 1.5 bonuses means each packet gets 3 questions
# from the category, sometimes 2 tossups + 1 bonus, sometimes the reverse.
class PacketizationEntry(models.Model):

    question_set = models.ForeignKey(QuestionSet, on_delete=models.CASCADE)
    path = models.CharField(max_length=500)
    depth = models.PositiveIntegerField(default=0)
    min_tossups = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)
    max_tossups = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)
    min_bonuses = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)
    max_bonuses = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)

    def __str__(self):
        return '{0!s}: {1!s}'.format(self.question_set, self.path)

# An editor-defined tag under a category path of a set, e.g. "Asian
# Literature" under "Literature - World", with optional required tossup and
# bonus counts that writers fulfill by tagging their questions.
class CategoryTag(models.Model):

    question_set = models.ForeignKey(QuestionSet, on_delete=models.CASCADE)
    category_path = models.CharField(max_length=500)
    name = models.CharField(max_length=200)
    num_tossups = models.PositiveIntegerField(default=0)
    num_bonuses = models.PositiveIntegerField(default=0)
    tossups = models.ManyToManyField('Tossup', blank=True, related_name='category_tags')
    bonuses = models.ManyToManyField('Bonus', blank=True, related_name='category_tags')

    def __str__(self):
        return '{0!s}: {1!s} ({2!s})'.format(self.question_set, self.name, self.category_path)

class QuestionType(models.Model):

    question_type = models.CharField(max_length=500)

    def __str__(self):
        return '{0!s}'.format(self.question_type)

# Tossups and tossup history will both reference this, it's how you link
class QuestionHistory(models.Model):
    pass

class Tossup (models.Model):
    packet = models.ForeignKey(Packet, on_delete=models.CASCADE, null=True)
    question_set = models.ForeignKey(QuestionSet, on_delete=models.CASCADE)
    tossup_text = models.TextField()
    tossup_answer = models.TextField()
    period = models.ForeignKey(Period, on_delete=models.CASCADE, null=True)

    category = models.ForeignKey(DistributionEntry, on_delete=models.CASCADE, null=True) # TODO: Delete this later
    subtype = models.CharField(max_length=500)

    #category_entry = models.ForeignKey(CategoryEntry, null=True)

    time_period = models.CharField(max_length=500)
    location = models.CharField(max_length=500)
    question_type = models.ForeignKey(QuestionType, on_delete=models.CASCADE, null=True)
    author = models.ForeignKey(Writer, on_delete=models.CASCADE)

    locked = models.BooleanField(default=False)
    edited = models.BooleanField(default=False)
    proofread = models.BooleanField(default=False)
    read_carefully = models.BooleanField(default=False)

    #order = models.PositiveIntegerField(null=True)
    question_number = models.PositiveIntegerField(null=True)
    
    search_question_content = models.TextField(default='')
    search_question_answers = models.TextField(default='')

    question_history = models.ForeignKey(QuestionHistory, on_delete=models.CASCADE, null=True)

    created_date = models.DateTimeField()
    last_changed_date = models.DateTimeField()
    edited_date = models.DateTimeField(null=True)
    editor = models.ForeignKey(Writer, on_delete=models.CASCADE, null=True, related_name='tossup_editor')
    proofread_date = models.DateTimeField(null=True)
    proofreader = models.ForeignKey(Writer, on_delete=models.CASCADE, null=True, related_name='tossup_proofreader')

    # Calculates character count, ignoring special characters
    def character_count(self):
        char_count_ignores_pronunciation_guides = True
        if (self.get_question_set() is not None):
            char_count_ignores_pronunciation_guides = self.question_set.char_count_ignores_pronunciation_guides        
        
        return get_character_count(self.tossup_text, char_count_ignores_pronunciation_guides)

    def character_count_exclusions(self):
        """Text excluded from this tossup's character count (moderator
        instructions, directives, pronunciation guides)."""
        ignore = True
        if self.get_question_set() is not None:
            ignore = self.question_set.char_count_ignores_pronunciation_guides
        return get_char_count_exclusions(self.tossup_text, ignore)

    def save(self, *args, **kwargs):
        self.setup_search_fields()
        super(Tossup, self).save(*args, **kwargs)

    def __str__(self):
        return '{0!s}...'.format(strip_markup(self.tossup_answer)[0:40]) #.decode('utf-8')

    def to_json(self):

        if self.packet is None:
            packet_id = None
        else:
            packet_id = self.packet.id
        if self.category is None:
            category_id = None
            category_name = ''
        else:
            category_id = self.category.id
            category_name = str(DistributionEntry.objects.get(id=self.category.id))
        return {'id': self.id,
                'packet': packet_id,
                'tossup_text': self.tossup_text.strip(),
                'tossup_answer': self.tossup_answer.strip(),
                'category': category_id,
                'category_name': category_name.strip(),
                'author': self.author.id,
                'question_number': self.question_number}

    def to_latex(self):

        html_to_latex_dict = {'u': 'uline', 'b': 'bf', 'strong': 'bf', 'i': 'it'}

        tossup_text = html_to_latex(self.tossup_text, html_to_latex_dict)
        tossup_answer = html_to_latex(self.tossup_answer, html_to_latex_dict)

        return r'\tossup{{{0}}}{{{1}}}'.format(tossup_text, tossup_answer) + '\n'

    def to_plain_text(self, include_category=False, include_character_count=False):
        output = self.tossup_text + "\nANSWER: " + self.tossup_answer + "\n"
        if (include_category and self.category is not None):
            output = output + str(self.category) + "\n"
        if (include_character_count):
            output = output + str(self.character_count()) + "\n"
        
        return output

    def to_html(self, include_category=False, include_character_count=False):

        output = ''
        output = output + "<p>" + get_formatted_question_html(self.tossup_text, False, True, False, True) + "<br />"
        output = output + "ANSWER: " + get_formatted_question_html(self.tossup_answer, True, True, False, False) + "</p>"
        if (include_category and self.category is not None):
            output = output + "<p><strong>Category:</strong> " + str(self.category) + "</p>"
        else:
            output = output

        if (include_character_count):
            char_count = self.character_count()
            css_class = ''
            if (self.get_question_set() is not None):
                if (self.character_count() > self.question_set.max_acf_tossup_length):
                    css_class = "class='over-char-limit'"
                output = output + "<p><strong " + css_class + ">Character Count:</strong> " + str(self.character_count()) + "/" + str(self.question_set.max_acf_tossup_length) + "</p>"
            else:
                output = output + "<p><strong>Character Count:</strong> " + str(char_count) + "</p>"

        return output

    def is_valid(self):

        if self.tossup_text == '':
            raise InvalidTossup('question', self.tossup_text, self.question_number,
                                reason='The question text is empty.')

        if self.tossup_answer == '':
            raise InvalidTossup('answer', self.tossup_answer, self.question_number,
                                reason='The answer line is empty.')

        text_reason = special_character_imbalance_reason(self.tossup_text)
        if text_reason is not None:
            raise InvalidTossup('question', self.tossup_text, self.question_number,
                                reason=text_reason)

        answer_reason = special_character_imbalance_reason(self.tossup_answer)
        if answer_reason is not None:
            raise InvalidTossup('answer', self.tossup_answer, self.question_number,
                                reason=answer_reason)

        if (not does_answerline_have_underlines(self.tossup_answer)):
            raise InvalidTossup('answer', self.tossup_answer, self.question_number,
                                reason='The answer line has no underlined portion. Mark '
                                       'the required part(s) of the answer with underscores, '
                                       'e.g. "_hole_s".')

        return True

    def setup_search_fields(self, remove_unicode=True):
        if (remove_unicode):
            self.search_question_content = strip_special_chars(strip_unicode(self.tossup_text))
            self.search_question_answers = strip_special_chars(strip_unicode(self.tossup_answer))
        else:            
            self.search_question_content = strip_special_chars(self.tossup_text)
            self.search_question_answers = strip_special_chars(self.tossup_answer)

    def get_question_set(self):
        try:
            return self.question_set
        except:
            return None

    def get_question_history(self):
        tossups = []
        bonuses = []

        if (self.question_history is not None):
            tossups = TossupHistory.objects.filter(question_history=self.question_history)
            bonuses = BonusHistory.objects.filter(question_history=self.question_history)

        return tossups, bonuses

    def latest_history(self):
        """The most recent TossupHistory row (the version current right now), or
        None if this tossup has no history yet."""
        if self.question_history_id is None:
            return None
        return (TossupHistory.objects.filter(question_history_id=self.question_history_id)
                .order_by('-id').first())

    def save_question(self, edit_type, changer):

        if (self.question_history is None):
            qh = QuestionHistory()
            qh.save()
            self.question_history = qh
            self.created_date = timezone.now()

        self.last_changed_date = timezone.now()
        if (edit_type == QUESTION_EDIT):
            self.editor = changer
            self.edited_date = timezone.now()

        if (edit_type == QUESTION_PROOFREAD):
            self.proofreader = changer
            self.proofread_date = timezone.now()

        self.tossup_answer = strip_answer_from_answer_line(self.tossup_answer)
        tossup_history = TossupHistory()
        tossup_history.tossup_text = self.tossup_text
        tossup_history.tossup_answer = self.tossup_answer
        tossup_history.question_type = self.question_type
        tossup_history.question_history = self.question_history
        tossup_history.changer = changer
        tossup_history.change_date = timezone.now()
        tossup_history.save()
        self.setup_search_fields()

        self.save()

    def get_tossup_type(self):
        return get_tossup_type_from_question_type(self.question_type)

class Bonus(models.Model):
    packet = models.ForeignKey(Packet, on_delete=models.CASCADE, null=True)
    question_set = models.ForeignKey(QuestionSet, on_delete=models.CASCADE)
    period = models.ForeignKey(Period, on_delete=models.CASCADE, null=True)

    # Leadins and part 2 and 3 aren't required in VHSL, so allow nulls
    # The is_valid method will make sure that ACF bonuses have these values
    leadin = models.CharField(max_length=500, null=True)
    part1_text = models.TextField()
    part1_answer = models.TextField()
    part2_text = models.TextField(null=True)
    part2_answer = models.TextField(null=True)
    part3_text = models.TextField(null=True)
    part3_answer = models.TextField(null=True)

    DIFFICULTY_CHOICES = [('', '-'), ('e', 'Easy'), ('m', 'Medium'), ('h', 'Hard')]
    part1_difficulty = models.CharField(max_length=1, blank=True, default='', choices=DIFFICULTY_CHOICES)
    part2_difficulty = models.CharField(max_length=1, blank=True, default='', choices=DIFFICULTY_CHOICES)
    part3_difficulty = models.CharField(max_length=1, blank=True, default='', choices=DIFFICULTY_CHOICES)

    category = models.ForeignKey(DistributionEntry, on_delete=models.CASCADE, null=True) # TODO: Delete this later
    subtype = models.CharField(max_length=500)
    time_period = models.CharField(max_length=500)
    location = models.CharField(max_length=500)
    question_type = models.ForeignKey(QuestionType, on_delete=models.CASCADE, null=True)

    #category_entry = models.ForeignKey(CategoryEntry, null=True)

    question_history = models.ForeignKey(QuestionHistory, on_delete=models.CASCADE, null=True)

    author = models.ForeignKey(Writer, on_delete=models.CASCADE)

    locked = models.BooleanField(default=False)
    edited = models.BooleanField(default=False)
    proofread = models.BooleanField(default=False)
    read_carefully = models.BooleanField(default=False)

    #order = models.PositiveIntegerField(null=True)
    question_number = models.PositiveIntegerField(null=True)

    search_question_content = models.TextField(default='')
    search_question_answers = models.TextField(default='')

    created_date = models.DateTimeField()
    last_changed_date = models.DateTimeField()
    edited_date = models.DateTimeField(null=True)
    editor = models.ForeignKey(Writer, on_delete=models.CASCADE, null=True, related_name='bonus_editor')
    proofread_date = models.DateTimeField(null=True)
    proofreader = models.ForeignKey(Writer, on_delete=models.CASCADE, null=True, related_name='bonus_proofreader')

    # Calculates character count, ignoring special characters
    def character_count(self):
        char_count_ignores_pronunciation_guides = True
        if (self.get_question_set() is not None):
            char_count_ignores_pronunciation_guides = self.question_set.char_count_ignores_pronunciation_guides  
        
        leadin_count = get_character_count(self.leadin, char_count_ignores_pronunciation_guides)
        part1_count = get_character_count(self.part1_text, char_count_ignores_pronunciation_guides)
        part2_count = get_character_count(self.part2_text, char_count_ignores_pronunciation_guides)
        part3_count = get_character_count(self.part3_text, char_count_ignores_pronunciation_guides)
        return leadin_count + part1_count + part2_count + part3_count

    def character_count_exclusions(self):
        """Text excluded from this bonus's character count across leadin/parts."""
        ignore = True
        if self.get_question_set() is not None:
            ignore = self.question_set.char_count_ignores_pronunciation_guides
        out = []
        for field in (self.leadin, self.part1_text, self.part2_text, self.part3_text):
            out.extend(get_char_count_exclusions(field, ignore))
        seen = set()
        deduped = []
        for s in out:
            if s.lower() not in seen:
                seen.add(s.lower())
                deduped.append(s)
        return deduped

    def save(self, *args, **kwargs):
        self.setup_search_fields()
        super(Bonus, self).save(*args, **kwargs)

    def __str__(self):
        if (self.get_bonus_type() == ACF_STYLE_BONUS):
            return '{0!s}...'.format(strip_markup(get_answer_no_formatting(self.part1_answer))[0:40])
        else:
            return '{0!s}...'.format(strip_markup(get_answer_no_formatting(self.part1_answer))[0:40])

    def to_json(self):

        if self.packet is None:
            packet_id = None
        else:
            packet_id = self.packet.id
        if self.category is None:
            category_id = None
            category_name = ''
        else:
            category_id = self.category.id
            category_name = str(DistributionEntry.objects.get(id=self.category.id))

        return {'id': self.id,
                'packet': packet_id,
                'leadin': self.leadin.strip(),
                'part1_text': self.part1_text,
                'part1_answer': self.part1_answer,
                'part2_text': self.part2_text,
                'part2_answer': self.part2_answer,
                'part3_text': self.part3_text,
                'part3_answer': self.part3_answer,
                'category': category_id,
                'category_name': category_name.strip(),
                'author': self.author.id,
                'question_number': self.question_number}

    def to_latex(self):

        html_to_latex_dict = {'u': 'uline', 'b': 'bf', 'strong': 'bf', 'i': 'it'}

        leadin = html_to_latex(self.leadin, html_to_latex_dict)
        leadin = r'\begin{{bonus}}{{{0}}}'.format(leadin) + '\n'

        parts = [self.part1_text, self.part2_text, self.part3_text]
        answers = [self.part1_answer, self.part2_answer, self.part3_answer]

        parts_latex = ''

        for part, answer in zip(parts, answers):
            answer = html_to_latex(answer, html_to_latex_dict)
            part = html_to_latex(part, html_to_latex_dict)
            parts_latex += r'\bonuspart{{{0}}}{{{1}}}{{{2}}}'.format(10, part, answer) + '\n'

        return leadin + parts_latex + r'\end{bonus}' + '\n'

    def leadin_to_html(self):
        output = ''
        if (self.get_bonus_type() == ACF_STYLE_BONUS):
            return get_formatted_question_html(self.leadin, False, True, False, False)
        elif (self.get_bonus_type() == VHSL_BONUS):
            return get_formatted_question_html(self.part1_text, False, True, False, False)
        return output

    def to_plain_text(self, include_category=False, include_character_count=False):
        output = ''

        if (self.get_bonus_type() == ACF_STYLE_BONUS):
            output = output + self.leadin + "\n"
            output = output + "[10" + self.part1_difficulty + "] " + self.part1_text + "\n"
            output = output + "ANSWER: " + self.part1_answer + "\n"
            output = output + "[10" + self.part2_difficulty + "] " + self.part2_text + "\n"
            output = output + "ANSWER: " + self.part2_answer + "\n"
            output = output + "[10" + self.part3_difficulty + "] " + self.part3_text + "\n"
            output = output + "ANSWER: " + self.part3_answer + "\n"
            if (include_category and self.category is not None):
                output = output + str(self.category) + "\n"
            if (include_character_count):
                output = output + str(self.character_count()) + "\n"
        elif (self.get_bonus_type() == VHSL_BONUS):
            output = output + "[10] " + self.part1_text + "\n"
            output = output + "ANSWER: " + self.part1_answer + "\n"
            if (include_category and self.category is not None):
                output = output + str(self.category) + "\n"
            if (include_character_count):
                output = output + str(self.character_count()) + "\n"
        
        return output

    def to_html(self, include_category=False, include_character_count=False):
        output = ''

        if (self.get_bonus_type() == ACF_STYLE_BONUS):
            output = output + "<p>" + get_formatted_question_html(self.leadin, False, True, False, False) + "<br />"
            output = output + "[10" + self.part1_difficulty + "] " + get_formatted_question_html(self.part1_text, False, True, False, False) + "<br />"
            output = output + "ANSWER: " + get_formatted_question_html(self.part1_answer, True, True, False, False) + "<br />"
            output = output + "[10" + self.part2_difficulty + "] " + get_formatted_question_html(self.part2_text, False, True, False, False) + "<br />"
            output = output + "ANSWER: " + get_formatted_question_html(self.part2_answer, True, True, False, False) + "<br />"
            output = output + "[10" + self.part3_difficulty + "] " + get_formatted_question_html(self.part3_text, False, True, False, False) + "<br />"
            output = output + "ANSWER: " + get_formatted_question_html(self.part3_answer, True, True, False, False) + "</p>"

            if (include_category and self.category is not None):
                output = output + "<p><strong>Category:</strong> " + str(self.category) + "</p>"

            if (include_character_count):
                char_count = self.character_count()
                css_class = ''
                if (self.get_question_set() is not None):
                    if (self.character_count() > self.question_set.max_acf_bonus_length):
                        css_class = "class='over-char-limit'"
                    output = output + "<p><strong " + css_class + ">Character Count:</strong> " + str(char_count) + "/" + str(self.question_set.max_acf_bonus_length) + "</p>"
                else:
                    output = output + "<p><strong>Character Count:</strong> " + str(char_count) + "</p>"

        elif (self.get_bonus_type() == VHSL_BONUS):
            output = output + "<p>" + get_formatted_question_html(self.part1_text, False, True, False, False) + "<br />"
            output = output + "ANSWER: " + get_formatted_question_html(self.part1_answer, True, True, False, False) + "</p>"

            if (include_category and self.category is not None):
                output = output + "<p><strong>Category:</strong> " + str(self.category) + "</p>"

            if (include_character_count):
                char_count = self.character_count()
                css_class = ''
                if (self.get_question_set() is not None):
                    if (self.character_count() > self.question_set.max_vhsl_bonus_length):
                        css_class = "class='over-char-limit'"
                    output = output + "<p><strong " + css_class + ">Character Count:</strong> " + str(char_count)  + "/" + str(self.question_set.max_vhsl_bonus_length) + "</p>"
                else:
                    output = output + "<p><strong>Character Count:</strong> " + str(char_count) + "</p>"

        return output

    def is_valid(self):

        if (self.get_bonus_type() == ACF_STYLE_BONUS):
            print("valid acf")

            if self.leadin == '':
                raise InvalidBonus('leadin', self.leadin, self.question_number,
                                   reason='The leadin is empty.')

            leadin_reason = special_character_imbalance_reason(self.leadin)
            if leadin_reason is not None:
                raise InvalidBonus('leadin', self.leadin, self.question_number,
                                   reason=leadin_reason)

            answers = [self.part1_answer, self.part2_answer, self.part3_answer]
            for answer in answers:
                answer_reason = special_character_imbalance_reason(answer)
                if answer_reason is not None:
                    raise InvalidBonus('answers', answer, self.question_number,
                                       reason=answer_reason)
                if (not does_answerline_have_underlines(answer)):
                    raise InvalidBonus('answers', answer, self.question_number,
                                       reason='This answer has no underlined portion. Mark '
                                              'the required part(s) with underscores, e.g. "_France_".')

            parts = [self.part1_text, self.part2_text, self.part3_text]
            for part in parts:
                if part == '':
                    raise InvalidBonus('parts', part, self.question_number,
                                       reason='A bonus part is empty.')
                part_reason = special_character_imbalance_reason(part)
                if part_reason is not None:
                    raise InvalidBonus('parts', part, self.question_number,
                                       reason=part_reason)

            return True

        elif (self.get_bonus_type() == VHSL_BONUS):
            print("valid vhsl")

            if (self.leadin is not None and self.leadin != ''):
                raise InvalidBonus('leadin', self.leadin + " (this field should be blank for VHSL bonuses.)", self.question_number)
            blank_parts = [self.part2_text, self.part2_answer, self.part3_text, self.part3_answer]
            for blank_part in blank_parts:
                if (blank_part is not None and blank_part != ''):
                    raise InvalidBonus('2nd or 3rd part of bonus (this field should be blank for VHSL bonuses.)', blank_part, self.question_number)

            answers = [self.part1_answer]
            for answer in answers:
                answer_reason = special_character_imbalance_reason(answer)
                if answer_reason is not None:
                    raise InvalidBonus('answer', answer, self.question_number,
                                       reason=answer_reason)
                if (not does_answerline_have_underlines(answer)):
                    raise InvalidBonus('answer', answer, self.question_number,
                                       reason='This answer has no underlined portion. Mark '
                                              'the required part(s) with underscores, e.g. "_France_".')

            parts = [self.part1_text]
            for part in parts:
                if part == '':
                    raise InvalidBonus('part', part, self.question_number,
                                       reason='The bonus part is empty.')
                part_reason = special_character_imbalance_reason(part)
                if part_reason is not None:
                    raise InvalidBonus('part', part, self.question_number,
                                       reason=part_reason)

            return True

        else:
            raise InvalidBonus('question_type', self.question_type, self.question_number)

    def setup_search_fields(self, remove_unicode=True):
        if (remove_unicode):
            self.search_question_content = strip_special_chars(strip_unicode(self.leadin)) + " " + strip_special_chars(strip_unicode(self.part1_text)) + " " + strip_special_chars(strip_unicode(self.part2_text)) + " " + strip_special_chars(strip_unicode(self.part3_text))
            self.search_question_answers = strip_special_chars(strip_unicode(self.part1_answer)) + " " + strip_special_chars(strip_unicode(self.part2_answer)) + " " + strip_special_chars(strip_unicode(self.part3_answer))
        else:
            self.search_question_content = strip_special_chars(self.leadin) + " " + strip_special_chars(self.part1_text) + " " + strip_special_chars(self.part2_text) + " " + strip_special_chars(self.part3_text)
            self.search_question_answers = strip_special_chars(self.part1_answer) + " " + strip_special_chars(self.part2_answer) + " " + strip_special_chars(self.part3_answer)

    def get_question_set(self):
        try:
            return self.question_set
        except:
            return None

    def get_bonus_type(self):
        return get_bonus_type_from_question_type(self.question_type)

    def get_question_history(self):
        tossups = []
        bonuses = []

        if (self.question_history is not None):
            tossups = TossupHistory.objects.filter(question_history=self.question_history)
            bonuses = BonusHistory.objects.filter(question_history=self.question_history)
            print("is not null")

        return tossups, bonuses

    def latest_history(self):
        """The most recent BonusHistory row (the version current right now), or
        None if this bonus has no history yet."""
        if self.question_history_id is None:
            return None
        return (BonusHistory.objects.filter(question_history_id=self.question_history_id)
                .order_by('-id').first())

    def save_question(self, edit_type, changer):
        if (self.question_history is None):
            qh = QuestionHistory()
            qh.save()
            self.question_history = qh
            self.created_date = timezone.now()

        self.last_changed_date = timezone.now()
        if (edit_type == QUESTION_EDIT):
            self.editor = changer
            self.edited_date = timezone.now()

        if (edit_type == QUESTION_PROOFREAD):
            self.proofreader = changer
            self.proofread_date = timezone.now()            

        if (self.get_bonus_type() == VHSL_BONUS):
            self.leadin = ''
            self.part2_text  = ''
            self.part2_answer = ''
            self.part3_text = ''
            self.part3_answer = ''

        self.part1_answer = strip_answer_from_answer_line(self.part1_answer)
        self.part2_answer = strip_answer_from_answer_line(self.part2_answer)
        self.part3_answer = strip_answer_from_answer_line(self.part3_answer)

        bonus_history = BonusHistory()
        bonus_history.leadin = self.leadin
        bonus_history.part1_text = self.part1_text
        bonus_history.part1_answer = self.part1_answer
        bonus_history.part2_text = self.part2_text
        bonus_history.part2_answer = self.part2_answer
        bonus_history.part3_text = self.part3_text
        bonus_history.part3_answer = self.part3_answer
        bonus_history.part1_difficulty = self.part1_difficulty
        bonus_history.part2_difficulty = self.part2_difficulty
        bonus_history.part3_difficulty = self.part3_difficulty
        bonus_history.question_type = self.question_type
        bonus_history.question_history = self.question_history
        bonus_history.changer = changer
        bonus_history.change_date = timezone.now()
        bonus_history.save()
        self.setup_search_fields()
        self.save()

class TossupHistory(models.Model):
    tossup_text = models.TextField()
    tossup_answer = models.TextField()
    changer = models.ForeignKey(Writer, on_delete=models.CASCADE)
    change_date = models.DateTimeField()
    question_history = models.ForeignKey(QuestionHistory, on_delete=models.CASCADE)
    question_type = models.ForeignKey(QuestionType, on_delete=models.CASCADE, null=True)

    def __str__(self):
        return '{0!s}...'.format(strip_markup(self.tossup_answer)[0:40]) #.decode('utf-8')

    def to_html(self):
        output = ''
        output = output + "<p>" + get_formatted_question_html(self.tossup_text, False, True, False, True) + "<br />"
        output = output + get_formatted_question_html(self.tossup_answer, True, True, False, False) + "<br />"
        output = output + "Changed by " + str(self.changer) + " on " + str(self.change_date) + "</p>"
        return output

class BonusHistory(models.Model):
    leadin = models.CharField(max_length=500, null=True)
    part1_text = models.TextField()
    part1_answer = models.TextField()
    part2_text = models.TextField(null=True)
    part2_answer = models.TextField(null=True)
    part3_text = models.TextField(null=True)
    part3_answer = models.TextField(null=True)
    part1_difficulty = models.CharField(max_length=1, blank=True, default='')
    part2_difficulty = models.CharField(max_length=1, blank=True, default='')
    part3_difficulty = models.CharField(max_length=1, blank=True, default='')
    changer = models.ForeignKey(Writer, on_delete=models.CASCADE)
    change_date = models.DateTimeField()
    question_history = models.ForeignKey(QuestionHistory, on_delete=models.CASCADE)
    question_type = models.ForeignKey(QuestionType, on_delete=models.CASCADE, null=True)

    def to_html(self):
        output = ''
        if (self.get_bonus_type() == ACF_STYLE_BONUS):
            output = output + "<p>" + get_formatted_question_html(self.leadin, False, True, False, False) + "<br />"
            output = output + "[10" + self.part1_difficulty + "] " + get_formatted_question_html(self.part1_text, False, True, False, False) + "<br />"
            output = output + "ANSWER: " + get_formatted_question_html(self.part1_answer, True, True, False, False) + "<br />"
            output = output + "[10" + self.part2_difficulty + "] " + get_formatted_question_html(self.part2_text, False, True, False, False) + "<br />"
            output = output + "ANSWER: " + get_formatted_question_html(self.part2_answer, True, True, False, False) + "<br />"
            output = output + "[10" + self.part3_difficulty + "] " + get_formatted_question_html(self.part3_text, False, True, False, False) + "<br />"
            output = output + "ANSWER: " + get_formatted_question_html(self.part3_answer, True, True, False, False) + "<br />"
        else:
            output = output + "<p>" + get_formatted_question_html(self.part1_text, False, True, False, False) + "<br />"
            output = output + "ANSWER: " + get_formatted_question_html(self.part1_answer, True, True, False, False) + "<br />"

        output = output + "Changed by <strong>" + str(self.changer) + "</strong> on <strong>" + str(self.change_date) + "</strong></p>"
        return output

    def __str__(self):
        if (self.get_bonus_type() == ACF_STYLE_BONUS):
            return '{0!s}...'.format(strip_markup(get_answer_no_formatting(self.leadin))[0:40])
        else:
            return '{0!s}...'.format(strip_markup(get_answer_no_formatting(self.part1_answer))[0:40])

    def get_bonus_type(self):
        return get_bonus_type_from_question_type(self.question_type)

class Tag(models.Model):

    pass

class WriterQuestionSetSettings(models.Model):
    writer = models.ForeignKey(Writer, on_delete=models.CASCADE)
    question_set = models.ForeignKey(QuestionSet, on_delete=models.CASCADE)
    email_on_all_new_comments = models.BooleanField(default=False)
    email_on_all_new_questions = models.BooleanField(default=False)
    
    # Creates new per category writer settings for this object
    def create_per_category_writer_settings(self):
        print("self id: " + str(self.id))
        for de in self.question_set.distribution.distributionentry_set.all():
            pcws = PerCategoryWriterSettings(writer_question_set_settings=self, distribution_entry=de)
            pcws.save()
            
class PerCategoryWriterSettings(models.Model):
    writer_question_set_settings = models.ForeignKey(WriterQuestionSetSettings, on_delete=models.CASCADE)
    distribution_entry = models.ForeignKey(DistributionEntry, on_delete=models.CASCADE)
    email_on_new_questions = models.BooleanField(default=False)
    email_on_new_comments = models.BooleanField(default=False)

def create_user_profile(sender, instance, created, **kwargs):
    if created:
        Writer.objects.create(user=instance)

@receiver(password_changed)
def password_change_callback(sender, request, user, **kwargs):
    messages.success(request, 'You have Successfully changed your Password!')

post_save.connect(create_user_profile, sender=User)


class CommentReply(models.Model):
    """Links a comment to its parent comment, enabling threaded discussions."""
    comment = models.OneToOneField(
        'django_comments.Comment',
        on_delete=models.CASCADE,
        related_name='reply_info'
    )
    parent = models.ForeignKey(
        'django_comments.Comment',
        on_delete=models.CASCADE,
        related_name='replies'
    )


class CommentMention(models.Model):
    """Records that a comment @mentioned a writer, for their activity feed."""
    comment = models.ForeignKey(
        'django_comments.Comment', on_delete=models.CASCADE, related_name='mentions')
    mentioned = models.ForeignKey(Writer, on_delete=models.CASCADE, related_name='mentions')
    created_date = models.DateTimeField(auto_now_add=True)
    seen = models.BooleanField(default=False)

    def __str__(self):
        return 'mention of {0!s}'.format(self.mentioned)


class ActivitySeen(models.Model):
    """When a writer last viewed their activity feed for a set, so unseen
    activity (mentions + changes to their questions) can be flagged."""
    writer = models.ForeignKey(Writer, on_delete=models.CASCADE, related_name='activity_seen')
    question_set = models.ForeignKey(QuestionSet, on_delete=models.CASCADE)
    last_seen = models.DateTimeField()

    class Meta:
        unique_together = ('writer', 'question_set')


class CommentResolution(models.Model):
    """Marks a comment as resolved (a discussion that's been dealt with).
    A comment is resolved when a row exists here with resolved=True."""
    comment = models.OneToOneField(
        'django_comments.Comment', on_delete=models.CASCADE, related_name='resolution')
    resolved = models.BooleanField(default=True)
    resolved_by = models.ForeignKey(Writer, on_delete=models.SET_NULL, null=True)
    resolved_date = models.DateTimeField(auto_now=True)


class CommentAnchor(models.Model):
    """Anchors a comment to a selected span of the rendered question text,
    like a Google Docs comment. Prefix/suffix store surrounding context so
    the span can be re-located (or detected as stale) after edits."""
    comment = models.OneToOneField(
        'django_comments.Comment',
        on_delete=models.CASCADE,
        related_name='anchor_info'
    )
    selected_text = models.TextField()
    prefix = models.CharField(max_length=100, blank=True, default='')
    suffix = models.CharField(max_length=100, blank=True, default='')


# Source of a playtest record: questions played in-app vs. imported from a
# Discord playtest server in a future change.
PLAYTEST_SOURCE_WEB = 'web'
PLAYTEST_SOURCE_DISCORD = 'discord'
PLAYTEST_SOURCES = ((PLAYTEST_SOURCE_WEB, 'Web'), (PLAYTEST_SOURCE_DISCORD, 'Discord'))

# Display name the Discord playtest bot posts comments under (so its comments
# can be recognized and styled apart from human ones even without a stored ref).
DISCORD_BOT_NAME = 'Cliff'


class PlaytestSession(models.Model):
    """One play-through of a set's questions, grouping the buzzes and bonus
    results recorded during it. A session may belong to a logged-in Writer or,
    for imported Discord playtests, just carry a free-text player name."""
    question_set = models.ForeignKey(QuestionSet, on_delete=models.CASCADE,
                                     related_name='playtest_sessions')
    player = models.ForeignKey(Writer, on_delete=models.SET_NULL, null=True, blank=True,
                               related_name='playtest_sessions')
    # Used when the player is not a Writer (e.g. a Discord username on import).
    player_name = models.CharField(max_length=200, blank=True, default='')
    source = models.CharField(max_length=20, choices=PLAYTEST_SOURCES,
                              default=PLAYTEST_SOURCE_WEB)
    # Optional external id (e.g. a Discord message/game id) so a future importer
    # can avoid creating duplicate sessions.
    external_id = models.CharField(max_length=200, blank=True, default='')
    created_date = models.DateTimeField(auto_now_add=True)

    def get_player_name(self):
        if self.player is not None:
            name = self.player.get_real_name().strip()
            return name or self.player.user.username
        return self.player_name or 'Anonymous'

    def __str__(self):
        return 'Playtest by {0!s} on {1!s}'.format(self.get_player_name(), self.question_set)


class TossupBuzz(models.Model):
    """A buzz on a tossup at a particular point in the question, recording
    where the player buzzed and whether they got it right. Buzzes come from
    in-app play now and from a Discord playtest server in a future change."""
    tossup = models.ForeignKey(Tossup, on_delete=models.CASCADE, related_name='buzzes')
    session = models.ForeignKey(PlaytestSession, on_delete=models.CASCADE, null=True,
                                blank=True, related_name='buzzes')
    player = models.ForeignKey(Writer, on_delete=models.SET_NULL, null=True, blank=True,
                               related_name='tossup_buzzes')
    player_name = models.CharField(max_length=200, blank=True, default='')

    # 0-based index of the word the player had heard when they buzzed, and the
    # total number of words in the tossup, so a buzz fraction can be computed
    # even after the question text is edited.
    buzz_word_index = models.PositiveIntegerField(default=0)
    total_words = models.PositiveIntegerField(default=0)
    # Character offset into the (plain-text) tossup at the buzz point.
    char_position = models.PositiveIntegerField(default=0)

    correct = models.BooleanField(default=False)
    # Whether the buzz was inside the power mark (before "(*)").
    powered = models.BooleanField(default=False)
    # Score: 15 power, 10 get, -5 neg, 0 otherwise.
    value = models.IntegerField(default=0)
    answer_given = models.TextField(blank=True, default='')

    source = models.CharField(max_length=20, choices=PLAYTEST_SOURCES,
                              default=PLAYTEST_SOURCE_WEB)
    # The version of the tossup that was current when this buzz was recorded, so
    # results can link straight to the exact text the player saw even after the
    # question is later edited. Set on record (web and Discord import).
    tossup_history = models.ForeignKey('TossupHistory', on_delete=models.SET_NULL,
                                       null=True, blank=True, related_name='buzzes')
    # Stable id supplied by an external recorder (e.g. the Discord bot) so the
    # same buzz can be re-sent without being recorded twice. Blank for web play.
    external_id = models.CharField(max_length=200, blank=True, default='', db_index=True)
    # Defaults to now (web play) but is overridable so an importer can record
    # when the buzz actually happened (e.g. the Discord results message time).
    buzz_date = models.DateTimeField(default=timezone.now)

    def history_url(self):
        """Deep link to the version of the tossup this buzz was recorded on."""
        if self.tossup_history_id is None:
            return ''
        return '/tossup_history/{0}/?v={1}#version-{1}'.format(
            self.tossup_id, self.tossup_history_id)

    def buzz_fraction(self):
        """How far into the tossup the buzz happened, 0.0-1.0."""
        if not self.total_words:
            return 0.0
        return min(1.0, float(self.buzz_word_index) / float(self.total_words))

    def get_player_name(self):
        if self.player is not None:
            name = self.player.get_real_name().strip()
            return name or self.player.user.username
        return self.player_name or 'Anonymous'

    def __str__(self):
        return 'Buzz on {0!s} ({1!s})'.format(self.tossup, 'correct' if self.correct else 'incorrect')


class BonusResult(models.Model):
    """A play-through of one bonus, recording which parts the player got and
    the total points. Comes from in-app play now and a future Discord import."""
    bonus = models.ForeignKey(Bonus, on_delete=models.CASCADE, related_name='results')
    session = models.ForeignKey(PlaytestSession, on_delete=models.CASCADE, null=True,
                                blank=True, related_name='bonus_results')
    player = models.ForeignKey(Writer, on_delete=models.SET_NULL, null=True, blank=True,
                               related_name='bonus_results')
    player_name = models.CharField(max_length=200, blank=True, default='')

    part1_correct = models.BooleanField(default=False)
    part2_correct = models.BooleanField(default=False)
    part3_correct = models.BooleanField(default=False)
    # Total points (0-30 for a 3-part bonus).
    total = models.PositiveIntegerField(default=0)

    source = models.CharField(max_length=20, choices=PLAYTEST_SOURCES,
                              default=PLAYTEST_SOURCE_WEB)
    # The version of the bonus current when this result was recorded. See
    # TossupBuzz.tossup_history.
    bonus_history = models.ForeignKey('BonusHistory', on_delete=models.SET_NULL,
                                      null=True, blank=True, related_name='results')
    # See TossupBuzz.external_id.
    external_id = models.CharField(max_length=200, blank=True, default='', db_index=True)
    # See TossupBuzz.buzz_date — overridable so an import can set the real time.
    answered_date = models.DateTimeField(default=timezone.now)

    def history_url(self):
        """Deep link to the version of the bonus this result was recorded on."""
        if self.bonus_history_id is None:
            return ''
        return '/bonus_history/{0}/?v={1}#version-{1}'.format(
            self.bonus_id, self.bonus_history_id)

    def get_player_name(self):
        if self.player is not None:
            name = self.player.get_real_name().strip()
            return name or self.player.user.username
        return self.player_name or 'Anonymous'

    def __str__(self):
        return 'Bonus result on {0!s} ({1!s})'.format(self.bonus, self.total)


class SetApiKey(models.Model):
    """A secret token that grants an external service (e.g. the Discord playtest
    bot) permission to write buzzes/results/comments to one question set. One
    key per set; regenerating replaces the token and revokes the old one. Owners
    and co-owners manage it."""
    question_set = models.OneToOneField(QuestionSet, on_delete=models.CASCADE,
                                        related_name='api_key')
    key = models.CharField(max_length=64, unique=True, db_index=True)
    active = models.BooleanField(default=True)
    created_by = models.ForeignKey(Writer, on_delete=models.SET_NULL, null=True)
    created_date = models.DateTimeField(auto_now=True)

    @staticmethod
    def generate_token():
        import secrets
        return secrets.token_urlsafe(32)

    def __str__(self):
        return 'API key for {0!s}'.format(self.question_set)


class DiscordCommentRef(models.Model):
    """Links a comment created by the Discord bot to the external id it was sent
    with, so the same comment isn't posted twice on retries/re-syncs."""
    external_id = models.CharField(max_length=200, unique=True, db_index=True)
    comment = models.OneToOneField('django_comments.Comment', on_delete=models.CASCADE,
                                   related_name='discord_ref')
    question_set = models.ForeignKey(QuestionSet, on_delete=models.CASCADE)

    def __str__(self):
        return 'Discord comment {0!s}'.format(self.external_id)


class DiscordThread(models.Model):
    """A link to the Discord playtest thread for a question, recorded by the bot
    so editors can jump straight to the discussion. Attached to the question
    itself (not embedded in a comment). Exactly one of tossup/bonus is set."""
    question_set = models.ForeignKey(QuestionSet, on_delete=models.CASCADE,
                                     related_name='discord_threads')
    tossup = models.ForeignKey(Tossup, on_delete=models.CASCADE, null=True, blank=True,
                               related_name='discord_threads')
    bonus = models.ForeignKey(Bonus, on_delete=models.CASCADE, null=True, blank=True,
                              related_name='discord_threads')
    url = models.URLField(max_length=500)
    title = models.CharField(max_length=300, blank=True, default='')
    # Stable id from the bot so the same thread link isn't stored twice.
    external_id = models.CharField(max_length=200, blank=True, default='', db_index=True)
    created_date = models.DateTimeField(auto_now_add=True)

    def question(self):
        return self.tossup or self.bonus

    def __str__(self):
        return 'Discord thread {0!s}'.format(self.url)

