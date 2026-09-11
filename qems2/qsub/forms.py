from .models import *
from .utils import *
from django import forms
from django.forms import ValidationError
from django.utils.translation import gettext, gettext_lazy as _
from django.db.models import Q

class RegistrationFormWithName(forms.Form):
    first_name = forms.CharField(max_length=200)
    last_name = forms.CharField(max_length=200)

    def signup(self, request, user):
        user.first_name = self.cleaned_data['first_name']
        user.last_name = self.cleaned_data['last_name']
        user.save()

class WriterQuestionSetSettingsForm(forms.ModelForm):
    
    class Meta:
        model = WriterQuestionSetSettings
        exclude = ['question_set', 'writer']

    def __init__(self, *args, **kwargs):
        super(WriterQuestionSetSettingsForm, self).__init__(*args, **kwargs)
        self.fields['email_on_all_new_comments'] = forms.BooleanField(required=False)
        self.fields['email_on_all_new_questions'] = forms.BooleanField(required=False)
    
class PerCategoryWriterSettingsForm(forms.Form):
    email_on_new_comments = forms.BooleanField(required=False)
    email_on_new_questions = forms.BooleanField(required=False)
    activity_on_question_changes = forms.BooleanField(required=False)
    distribution_entry_string = forms.CharField(max_length=200)
    entry_id = forms.IntegerField(widget=forms.TextInput(attrs={'style': 'display: none'}))

    def __init__(self, *args, **kwargs):
        super(PerCategoryWriterSettingsForm, self).__init__(*args, **kwargs)
        self.fields['distribution_entry_string'].widget.attrs.update({'readonly': 'readonly'})
        
class WriterChangeForm(forms.Form):

    def __init__(self, *args, **kwargs):

        super(WriterChangeForm, self).__init__(*args, **kwargs)

        self.fields['username'] = forms.CharField(max_length=200)
        self.fields['first_name'] = forms.CharField(max_length=200)
        self.fields['last_name'] = forms.CharField(max_length=200)
        self.fields['email'] = forms.EmailField(max_length=200)
        self.fields['send_mail_on_comments'] = forms.BooleanField(required=False)
        self.fields['search_in_new_tab'] = forms.BooleanField(required=False)

        self.fields['username'].widget.attrs.update({'readonly': 'readonly'})

class QuestionSetForm(forms.ModelForm):

    distribution = forms.ModelChoiceField(Distribution.objects.all(), empty_label=None)

    class Meta:
        model = QuestionSet
        # max_vhsl_bonus_length excluded: VHSL support has been removed from the UI.
        # tossups_per_packet / bonuses_per_packet are managed on the packetization
        # page, not this form; leaving them in made the form (which never renders
        # them) fail validation on every submit, so edits silently didn't save.
        # approval_status/approval_date are the administrator's, not the
        # owner's: they're set when the set is created and changed only from
        # the review page. Left in, the form would render neither but demand
        # both, so every submit would fail validation.
        exclude = ['owner', 'address', 'host', 'max_vhsl_bonus_length',
                   'tossups_per_packet', 'bonuses_per_packet',
                   'approval_status', 'approval_date']

    def clean_question_table_columns(self):
        """Only keys the model knows, in its own order. The value is a list the
        page writes from checkboxes, so it is normalised here rather than
        trusted -- and an empty choice means the default set, not no columns."""
        raw = (self.cleaned_data.get('question_table_columns') or '')
        wanted = {c.strip() for c in raw.split(',') if c.strip()}
        known = [k for k, _label, _on in QuestionSet.QUESTION_TABLE_COLUMNS]
        keep = [k for k in known if k in wanted]
        return ','.join(keep)

    def _owner_only_field(self, name):
        """Keep the stored value for a field the page only shows the owner.

        An absent input is indistinguishable from an emptied one, so the
        owner's form carries a marker field saying the input was on the page.
        Without it an editor saving the settings would wipe what it holds."""
        return getattr(self.instance, name, '')

    def clean_packet_credits(self):
        if not self.data.get('credits_shown'):
            return self._owner_only_field('packet_credits')
        return self.cleaned_data.get('packet_credits', '')

    def clean_first_packet_credits(self):
        if not self.data.get('credits_shown'):
            return self._owner_only_field('first_packet_credits')
        return self.cleaned_data.get('first_packet_credits', '')

    def clean_archived(self):
        """Keep the stored value when the checkbox was not on the page.

        Archiving is the owner's, so the settings page renders the checkbox
        only for them -- and an absent checkbox is indistinguishable from an
        unticked one, which would quietly unarchive the set the next time an
        editor saved the settings. The owner's form carries a marker field so
        the two can be told apart."""
        if not self.data.get('archived_shown'):
            return getattr(self.instance, 'archived', False)
        return self.cleaned_data.get('archived', False)

    def clean_favicon_color(self):
        """Only a colour the page offers. The value ends up in markup, and a
        list of nine is easier to be sure of than any amount of escaping."""
        value = (self.cleaned_data.get('favicon_color') or '').strip().lower()
        allowed = {c for c, _label in QuestionSet.FAVICON_COLORS}
        return value if value in allowed else ''

    def __init__(self, read_only=False, writer=None, *args, **kwargs):
        super(QuestionSetForm, self).__init__(*args, **kwargs)

        # Only offer the distributions this writer may use: the public ones and
        # their own. (A set's current distribution always qualifies — belonging
        # to the set is what makes it theirs — so editing a set never loses it.)
        if writer is not None:
            self.fields['distribution'].queryset = Distribution.selectable_by(
                writer, keep=getattr(self.instance, 'distribution_id', None))

        self.fields['date'].widget.attrs.update({'placeholder': 'mm/dd/yyyy'})

        # The colour is picked from swatches on the page, so the field only has
        # to carry the value they set.
        if 'favicon_color' in self.fields:
            self.fields['favicon_color'].required = False
            self.fields['favicon_color'].widget = forms.HiddenInput()
        # Chosen from checkboxes on the page, same as the colour.
        if 'question_table_columns' in self.fields:
            self.fields['question_table_columns'].required = False
            self.fields['question_table_columns'].widget = forms.HiddenInput()

        for field in self.fields:
            if read_only:
                self.fields[field].widget.attrs['readonly'] = True

class AddUserForm(forms.ModelForm):

    add_user = forms.BooleanField(required=False)

class RoleAssignmentForm(forms.ModelForm):

    class Meta:
        model = Role
        exclude = ['writer', 'question_set']

    def __init__(self, categories=None, *args, **kwargs):
        super(RoleAssignmentForm, self).__init__(*args, **kwargs)
        self.fields['category'] = forms.MultipleChoiceField(widget=forms.SelectMultiple(attrs={'size': len(CATEGORIES)}), choices=CATEGORIES)
        if categories:
            self.initial['category'] = categories

    #editor = forms.IntegerField(widget=forms.HiddenInput, required=True)
    #tournament = forms.IntegerField(widget=forms.HiddenInput, required=True)
    #categories = forms.MultipleChoiceField(widget=forms.SelectMultiple, choices=CATEGORIES)
    #can_view_others = forms.BooleanField(required=False)
    #can_edit_others = forms.BooleanField(required=False)

class TossupForm(forms.ModelForm):

    tossup_text = forms.CharField(widget=forms.Textarea(attrs={'rows': 10, 'class': 'expanding'}))
    tossup_answer = forms.CharField(widget=forms.Textarea(attrs={'rows': 1, 'class': 'expanding'}))

    category = forms.ModelChoiceField(queryset=DistributionEntry.objects.none())

    class Meta:
        model = Tossup
        # The bookkeeping fields (history, editor/proofreader stamps, search
        # text, dates) are managed by save_question(), never by this form —
        # excluding them keeps a bound instance from having them wiped to None.
        # all_power is rendered as a plain checkbox in the template and read
        # straight from POST (a nullable BooleanField would otherwise render as
        # a three-way select).
        # tossup_answer_structure is written from the structured answer editor's
        # own fields (see views._with_structured_answers), not as a JSON box.
        exclude = ['question_set', 'subtype', 'time_period', 'location', 'question_number',
                   'search_question_content', 'search_question_answers', 'question_history',
                   'editor', 'edited_date', 'proofreader', 'proofread_date',
                   'created_date', 'last_changed_date', 'all_power',
                   'tossup_answer_structure']

    def __init__(self, *args, **kwargs):
        qset_id = kwargs.pop('qset_id', None)
        packet_id = kwargs.pop('packet_id', None)
        period_id = kwargs.pop('period_id', None)
        role = kwargs.pop('role', None)
        writer = kwargs.pop('writer', None)

        super(TossupForm, self).__init__(*args, **kwargs)

        self.fields['question_type'] = forms.ModelChoiceField(queryset=QuestionType.objects.all(), required=False)
        self.fields['question_type'].widget.attrs['style'] = 'display:none'

        #self.fields['locked'].required = False

        if qset_id:
            try:
                qset = QuestionSet.objects.get(id=qset_id)
                writer_filter = Q(question_set_writer=qset) | Q(question_set_editor=qset)
                # Include the current author even if they're not in this set
                # (e.g. question was moved from another set)
                if self.instance and self.instance.pk and self.instance.author_id:
                    writer_filter = writer_filter | Q(pk=self.instance.author_id)
                all_writers = Writer.objects.filter(writer_filter).distinct()
                all_writers = all_writers.order_by('user__last_name', 'user__first_name', 'user__username')
                if writer:
                    user = User.objects.get(username=writer)
                    my_writer = all_writers.get(user=user)
                    self.fields['author'] = forms.ModelChoiceField(queryset=all_writers, initial=my_writer.pk, required=True, empty_label=None)
                else:
                    self.fields['author'] = forms.ModelChoiceField(queryset=all_writers, required=True, empty_label=None)

                dist = qset.distribution
                dist_entries = dist.distributionentry_set.all().order_by('category', 'subcategory')
                # categories = [(d.id, '{0!s} - {1!s}'.format(d.category, d.subcategory)) for d in dist_entries]
                if packet_id is not None:
                    pack_label = None
                    packets = qset.packet_set.filter(id=packet_id)
                else:
                    pack_label = 'None'
                    packets = qset.packet_set.all()

                periods = Period.objects.filter(period_wide_entry__question_set=qset)
                if period_id is not None:
                    period_label = None
                else:
                    period_label = 'None'

                self.fields['category'] = forms.ModelChoiceField(queryset=dist_entries, empty_label=None)
                self.fields['packet'] = forms.ModelChoiceField(queryset=packets, required=False, empty_label=pack_label)
                self.fields['period'] = forms.ModelChoiceField(queryset=periods, required=False, empty_label=period_label)

                # New questions in a packetized set default to the Extras packet
                if packet_id is None and not (self.instance and self.instance.pk):
                    extras = packets.filter(packet_name=EXTRAS_PACKET_NAME).first()
                    if extras:
                        self.fields['packet'].initial = extras.pk

            except QuestionSet.DoesNotExist:
                print('Non-existent question set!')
                self.fields['category'] = forms.ModelChoiceField(queryset=DistributionEntry.objects.none(), empty_label=None)

        if role and role == 'writer':
            # if this tossup is being submitted by a writer we don't need to show them the edited/locked checkboxes
            self.fields['locked'].widget.attrs['readonly'] = 'readonly'
            self.fields['locked'].widget.attrs['style'] = 'display:none'
            self.fields['locked'].label = ''
            self.fields['edited'].widget.attrs['readonly'] = 'readonly'
            self.fields['edited'].widget.attrs['style'] = 'display:none'
            self.fields['edited'].label = ''

class BonusForm(forms.ModelForm):

    leadin = forms.CharField(widget=forms.Textarea(attrs={'class': 'expanding', 'rows': 2}), required=False)
    part1_text = forms.CharField(widget=forms.Textarea(attrs={'class': 'expanding', 'rows': 2}))
    part1_answer = forms.CharField(widget=forms.Textarea(attrs={'class': 'expanding', 'rows': 1}))
    part2_text = forms.CharField(widget=forms.Textarea(attrs={'class': 'expanding', 'rows': 2}), required=False)
    part2_answer = forms.CharField(widget=forms.Textarea(attrs={'class': 'expanding', 'rows': 1}), required=False)
    part3_text = forms.CharField(widget=forms.Textarea(attrs={'class': 'expanding', 'rows': 2}), required=False)
    part3_answer = forms.CharField(widget=forms.Textarea(attrs={'class': 'expanding', 'rows': 1}), required=False)
    class Meta:
        model = Bonus
        # See TossupForm.Meta: bookkeeping fields belong to save_question(),
        # not the form, so a bound instance must not overwrite them.
        # The part*_answer_structure fields are written from the structured
        # answer editor's own fields (see views._with_structured_answers).
        exclude = ['question_set', 'subtype', 'time_period', 'location', 'question_number',
                   'search_question_content', 'search_question_answers', 'question_history',
                   'editor', 'edited_date', 'proofreader', 'proofread_date',
                   'created_date', 'last_changed_date',
                   'part1_answer_structure', 'part2_answer_structure', 'part3_answer_structure']

    def __init__(self, *args, **kwargs):
        qset_id = kwargs.pop('qset_id', None)
        packet_id = kwargs.pop('packet_id', None)
        period_id = kwargs.pop('period_id', None)
        role = kwargs.pop('role', None)
        writer = kwargs.pop('writer', None)
        question_type = kwargs.pop('question_type', None)

        super(BonusForm, self).__init__(*args, **kwargs)

        self.fields['question_type'] = forms.ModelChoiceField(queryset=QuestionType.objects.all(), required=False)
        self.fields['question_type'].widget.attrs['style'] = 'display:none'

        # Short labels for the compact per-part "Dif" dropdowns (E / M / H).
        short_diff = [('', '–'), ('e', 'E'), ('m', 'M'), ('h', 'H')]
        for i in (1, 2, 3):
            fld = self.fields.get('part{0}_difficulty'.format(i))
            if fld is not None:
                fld.choices = short_diff
                if hasattr(fld.widget, 'choices'):
                    fld.widget.choices = short_diff

        if qset_id:
            try:
                qset = QuestionSet.objects.get(id=qset_id)
                writer_filter = Q(question_set_writer=qset) | Q(question_set_editor=qset)
                # Include the current author even if they're not in this set
                # (e.g. question was moved from another set)
                if self.instance and self.instance.pk and self.instance.author_id:
                    writer_filter = writer_filter | Q(pk=self.instance.author_id)
                all_writers = Writer.objects.filter(writer_filter).distinct()
                all_writers = all_writers.order_by('user__last_name', 'user__first_name', 'user__username')
                if writer:
                    user = User.objects.get(username=writer)
                    my_writer = all_writers.get(user=user)
                    self.fields['author'] = forms.ModelChoiceField(queryset=all_writers, initial=my_writer.pk, required=True, empty_label=None)
                else:
                    self.fields['author'] = forms.ModelChoiceField(queryset=all_writers, required=True, empty_label=None)

                dist = qset.distribution
                dist_entries = dist.distributionentry_set.all().order_by('category', 'subcategory')
                if packet_id is not None:
                    pack_label = None
                    packets = qset.packet_set.filter(id=packet_id)
                else:
                    pack_label = 'None'
                    packets = qset.packet_set.all()

                periods = Period.objects.filter(period_wide_entry__question_set=qset)
                if period_id is not None:
                    period_label = None
                else:
                    period_label = 'None'

                self.fields['category'] = forms.ModelChoiceField(queryset=dist_entries, empty_label=None)
                self.fields['packet'] = forms.ModelChoiceField(queryset=packets, required=False, empty_label=pack_label)
                self.fields['period'] = forms.ModelChoiceField(queryset=periods, required=False, empty_label=period_label)

                # New questions in a packetized set default to the Extras packet
                if packet_id is None and not (self.instance and self.instance.pk):
                    extras = packets.filter(packet_name=EXTRAS_PACKET_NAME).first()
                    if extras:
                        self.fields['packet'].initial = extras.pk

            except QuestionSet.DoesNotExist:
                print('Non-existent question set!')
                self.fields['category'] = forms.ModelChoiceField(queryset=DistributionEntry.objects.none(), empty_label=None)

        if question_type and question_type == VHSL_BONUS:
            self.fields['leadin'].widget.attrs['style'] = 'display:none'
            self.fields['part2_text'].widget.attrs['style'] = 'display:none'
            self.fields['part2_answer'].widget.attrs['style'] = 'display:none'
            self.fields['part3_text'].widget.attrs['style'] = 'display:none'
            self.fields['part3_answer'].widget.attrs['style'] = 'display:none'

        if role and role == 'writer':
            # if this bonus is being submitted by a writer we don't need to show them the edited/locked checkboxes
            self.fields['locked'].widget.attrs['readonly'] = 'readonly'
            self.fields['locked'].widget.attrs['style'] = 'display:none'
            self.fields['locked'].label = ''
            self.fields['edited'].widget.attrs['readonly'] = 'readonly'
            self.fields['edited'].widget.attrs['style'] = 'display:none'
            self.fields['edited'].label = ''

class DistributionForm(forms.ModelForm):

    name = forms.CharField(max_length=100)

    acf_tossup_per_period_count = forms.CharField(widget=forms.HiddenInput, required=False)
    acf_bonus_per_period_count = forms.CharField(widget=forms.HiddenInput, required=False)
    vhsl_bonus_per_period_count = forms.CharField(widget=forms.HiddenInput, required=False)

    public = forms.BooleanField(
        required=False, label='Publicly viewable',
        help_text=('Anyone can find this distribution when creating a set, see its '
                   'categories, and make a copy. Editing stays with you and the '
                   'people on your sets.'))

    archived = forms.BooleanField(
        required=False, label='Archived',
        help_text=('Keeps it working on the sets that use it, and takes it out of '
                   'the list offered when someone creates a set.'))

    class Meta:
        model = Distribution
        fields = ['name', 'public', 'archived', 'acf_tossup_per_period_count',
                  'acf_bonus_per_period_count', 'vhsl_bonus_per_period_count']

    def clean_archived(self):
        """As QuestionSetForm.clean_archived: only whoever made the
        distribution gets the checkbox, so an absent one means 'not asked',
        not 'unticked'."""
        if not self.data.get('archived_shown'):
            return getattr(self.instance, 'archived', False)
        return self.cleaned_data.get('archived', False)
        
class TieBreakDistributionForm(forms.ModelForm):

    name = forms.CharField(max_length=100)

    class Meta:
        model = TieBreakDistribution
        fields = '__all__'

class DistributionEntryForm(forms.ModelForm):

    entry_id = forms.IntegerField(widget=forms.HiddenInput, required=False)
    category = forms.CharField(max_length=100, widget=forms.TextInput(attrs={}))
    subcategory = forms.CharField(max_length=100, widget=forms.TextInput(attrs={}), required=False)
    min_tossups = forms.FloatField(widget=forms.NumberInput(attrs={}), min_value=0)
    min_bonuses = forms.FloatField(widget=forms.NumberInput(attrs={}), min_value=0)
    max_tossups = forms.FloatField(widget=forms.NumberInput(attrs={}), min_value=0)
    max_bonuses = forms.FloatField(widget=forms.NumberInput(attrs={}), min_value=0)

    delete = forms.BooleanField(widget=forms.CheckboxInput, required=False)

    class Meta:
        model = DistributionEntry
        exclude = ['distribution']

class TieBreakDistributionEntryForm(forms.Form):

    entry_id = forms.IntegerField(widget=forms.HiddenInput, required=False)
    category = forms.CharField(max_length=100, widget=forms.TextInput(attrs={}), required=False)
    subcategory = forms.CharField(max_length=100, widget=forms.TextInput(attrs={}), required=False)
    num_tossups = forms.FloatField(widget=forms.NumberInput(attrs={}), min_value=0)
    num_bonuses = forms.FloatField(widget=forms.NumberInput(attrs={}), min_value=0)

    delete = forms.BooleanField(widget=forms.CheckboxInput, required=False)

    class Meta:
        model = DistributionEntry
        exclude = ['distribution']

class SetWideDistributionEntryForm(forms.Form):

    entry_id = forms.IntegerField(widget=forms.TextInput(attrs={'style': 'display: none'}))
    #dist_entry = forms.IntegerField(widget=forms.TextInput(attrs={'style': 'display: none'}))

    num_tossups = forms.IntegerField(widget=forms.NumberInput(attrs={}), min_value=0)
    num_bonuses = forms.IntegerField(widget=forms.NumberInput(attrs={}), min_value=0)

    category = forms.CharField(max_length=100, widget=forms.TextInput(attrs={}), required=False)
    subcategory = forms.CharField(max_length=100, widget=forms.TextInput(attrs={}), required=False)

    class Meta:
        model = SetWideDistributionEntry
        exclude = ['min_tossups', 'max_tossups', 'min_bonuses', 'max_bonuses', 'question_set']

class PacketForm(forms.Form):

    packet_name = forms.CharField(max_length=200)

class QuestionUploadForm(forms.Form):

    questions_file = forms.FileField()

class ImportSetForm(forms.Form):
    """Import a sheet as a new set, or into one that already exists.

    Same shape as ImportPacketsForm: a name or a target, never both. Adding to
    an existing set is what lets a large archive arrive in several uploads
    instead of one, without leaving a set behind per file.
    """

    set_name = forms.CharField(max_length=200, required=False, label='New set name')
    target_set = forms.ModelChoiceField(
        queryset=QuestionSet.objects.none(), required=False,
        label='\u2026or add these questions to an existing set')
    set_file = forms.FileField(label='TSV or CSV file (in export format)')

    def __init__(self, *args, **kwargs):
        """`writer` limits the target list to sets that writer works on. The
        ModelChoiceField validates the posted id against this queryset, so the
        list is the permission check as well as the menu -- importing thousands
        of questions into somebody else's tournament is not undoable by hand."""
        writer = kwargs.pop('writer', None)
        super(ImportSetForm, self).__init__(*args, **kwargs)
        if writer is not None:
            self.fields['target_set'].queryset = QuestionSet.objects.filter(
                Q(owner=writer) | Q(co_owners=writer) | Q(editor=writer)
            ).distinct().order_by('name')

    def clean(self):
        cleaned = super().clean()
        name = (cleaned.get('set_name') or '').strip()
        target = cleaned.get('target_set')
        if not name and not target:
            raise forms.ValidationError(
                'Enter a new set name, or choose an existing set to add to.')
        if name and target:
            raise forms.ValidationError(
                'Choose either a new set name or an existing set, not both.')
        return cleaned

class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True

class MultipleFileField(forms.FileField):
    """A FileField that accepts and validates several files at once (the stock
    FileField rejects multiple selection). Returns a list of files."""
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('widget', MultipleFileInput())
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single_clean = super().clean
        if isinstance(data, (list, tuple)):
            return [single_clean(d, initial) for d in data]
        return [single_clean(data, initial)]

class ImportPacketsForm(forms.Form):

    set_name = forms.CharField(max_length=200, required=False, label='New tournament name')
    target_set = forms.ModelChoiceField(
        queryset=QuestionSet.objects.none(), required=False,
        label='…or add packets to an existing set')
    packet_files = MultipleFileField(label='Packet files (.json, .docx, or .pdf)')

    def __init__(self, *args, **kwargs):
        """`writer` limits the target list to sets that writer actually works
        on. It used to offer every set on the site, which is somebody else's
        tournament to nearly everyone reading the page — and one mis-click from
        importing a packet into it. A ModelChoiceField validates the posted id
        against this queryset, so it's the check as well as the list."""
        writer = kwargs.pop('writer', None)
        super(ImportPacketsForm, self).__init__(*args, **kwargs)
        if writer is not None:
            self.fields['target_set'].queryset = QuestionSet.objects.filter(
                Q(owner=writer) | Q(co_owners=writer) | Q(editor=writer)
            ).distinct().order_by('name')

    def clean(self):
        cleaned = super().clean()
        name = (cleaned.get('set_name') or '').strip()
        target = cleaned.get('target_set')
        if not name and not target:
            raise forms.ValidationError('Enter a new tournament name, or choose an existing set to add to.')
        if name and target:
            raise forms.ValidationError('Choose either a new tournament name or an existing set, not both.')
        return cleaned

class CompareRepeatsForm(forms.Form):
    """Upload the previous set's packets to compare the current set against."""
    packet_files = MultipleFileField(
        label='Previous set packets (.json, .docx, or .pdf)')


class NewPacketsForm(forms.Form):

    packet_name = forms.CharField(max_length=200, required=False)

    name_base = forms.CharField(max_length=190, required=False)
    num_packets = forms.IntegerField(widget=forms.NumberInput(attrs={}), required=False, min_value=0)

class TypeQuestionsForm(forms.Form):

    questions = forms.CharField(label='', widget=forms.Textarea(attrs={'rows': 10}), required=False)

class MoveTossupForm(forms.Form):
    def __init__(self, *args, **kwargs):
        move_sets = kwargs.pop('move_sets', None)

        super(MoveTossupForm, self).__init__(*args, **kwargs)

        self.fields['move_sets'] = forms.ModelChoiceField(queryset=move_sets, required=True)

class MoveBonusForm(forms.Form):
    def __init__(self, *args, **kwargs):
        move_sets = kwargs.pop('move_sets', None)

        super(MoveBonusForm, self).__init__(*args, **kwargs)

        self.fields['move_sets'] = forms.ModelChoiceField(queryset=move_sets, required=True)


class IssueReportForm(forms.Form):
    """A bug report or a feature request, mailed to whoever runs the site.

    The contact fields start filled in from the account (see the view) but stay
    editable: the address someone wants a reply at is not always the one they
    signed up with.
    """

    KINDS = [('bug', 'Something is broken'),
             ('feature', 'I have an idea for a feature')]

    kind = forms.ChoiceField(choices=KINDS, initial='bug',
                             widget=forms.RadioSelect, label='What is this?')
    summary = forms.CharField(max_length=140, label='Summary',
                              widget=forms.TextInput(attrs={
                                  'placeholder': 'One line: what happened, or what you want'}))
    details = forms.CharField(label='Details', widget=forms.Textarea(attrs={
        'rows': 8,
        'placeholder': "For a bug: what you did, what you expected, and what "
                       "happened instead. For an idea: what you are trying to "
                       "do and why the app makes it hard. Describe the problem "
                       "without quoting the question."}))
    name = forms.CharField(max_length=100, required=False, label='Your name')
    email = forms.EmailField(label='Your email',
                             help_text='So you can be asked for more detail, or told when it is fixed.')

    # Deliberately nothing here that identifies a question. This mail leaves the
    # site for an ordinary inbox, and an unreleased set's questions must not go
    # with it -- not the text, and not a link that would lead to it either.

    def clean_summary(self):
        summary = (self.cleaned_data.get('summary') or '').strip()
        if len(summary) < 5:
            raise ValidationError('Please give a slightly longer summary.')
        return summary

    def clean_details(self):
        details = (self.cleaned_data.get('details') or '').strip()
        if len(details) < 15:
            raise ValidationError('Please say a little more — enough to act on.')
        return details
