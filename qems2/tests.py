from django.test import TestCase
from django.contrib.auth.models import AnonymousUser, User
from datetime import datetime
from django.utils import timezone

from qems2.qsub.packet_parser import is_answer, is_bpart, is_vhsl_bpart, is_category
from qems2.qsub.packet_parser import parse_packet_data, get_bonus_part_value, remove_category
from qems2.qsub.packet_parser import remove_answer_label
from qems2.qsub.utils import get_character_count, get_formatted_question_html, are_special_characters_balanced
from qems2.qsub.models import *
from qems2.qsub.model_utils import *
from qems2.qsub.packetizer import *
from qems2.qsub.packetizer import _ensure_packets
from decimal import Decimal

class PacketParserTests(TestCase):

    # TODO: Determine if we really need this block of code anymore
    #if django.VERSION[:2] == (1, 7):
    #    # Django 1.7 requires an explicit setup() when running tests in PTVS
    #    @classmethod
    #    def setUpClass(cls):
    #        django.setup()
    #elif django.VERSION[:2] >= (1, 8):
    #    # Django 1.8 requires a different setup. See https://github.com/Microsoft/PTVS-Samples/issues/1
    #    @classmethod
    #    def setUpClass(cls):
    #        super(DjangoTestCase, cls).setUpClass()
    #        django.setup()

    user = None
    writer = None
    dist = None
    qset = None
    pwe = None
    packet = None
    period = None
    ce = None
    cefd = None
    pwce = None
    opce = None
    acf_tossup = None
    acf_bonus = None
    vhsl_bonus = None
    
    acf_reg_dist = None
    acf_tb_dist = None
    acf_qset = None
    
    acf_reg_pwe = None
    acf_tb_pwe = None
    
    acf_packets = []
    acf_reg_periods = []
    acf_tb_periods = []
    
    acf_ces = []
    acf_reg_cefds = []
    acf_tb_cefds = []
    
    #########################################################################
    # Setup and helper methods
    #########################################################################
    
    def setUp(self):
        acfTossup = QuestionType.objects.get_or_create(question_type=ACF_STYLE_TOSSUP)
        acfBonus = QuestionType.objects.get_or_create(question_type=ACF_STYLE_BONUS)
        vhslBonus = QuestionType.objects.get_or_create(question_type=VHSL_BONUS)

    def create_user(self):
        self.user, created = User.objects.get_or_create(username="testuser")
        if (created):
            self.user.email='qems2test@gmail.com'
            self.user.password='top_secret'
            self.user.save()
            
        self.writer = Writer.objects.get(user=self.user.id)

    def create_generic_distribution(self, per_period_totals=20):
        self.dist = Distribution.objects.create(name="Test Distribution", acf_tossup_per_period_count=per_period_totals, acf_bonus_per_period_count=per_period_totals, vhsl_bonus_per_period_count=per_period_totals)
        self.dist.save()
        
    def create_generic_qset(self, num_packets=10):
        self.create_user()
        self.create_generic_distribution()
        self.qset = QuestionSet.objects.create(
            name="new_set",
            date=timezone.now(),
            host="test host",
            owner=self.writer,
            num_packets=num_packets,
            distribution=self.dist)
        self.qset.save()

    def create_generic_period(self, pwe_cur_value=5, pwe_total_value=10, cefd_fraction=5, cefd_min=15, cefd_max=15, period_cur_value=10, pwce_cur_value=10, pwce_total_value=10, opce_cur_value=10):
        self.create_generic_qset()
        
        self.pwe = PeriodWideEntry.objects.create(period_type=ACF_REGULAR_PERIOD, question_set=self.qset, distribution=self.dist)
        self.pwe.acf_tossup_cur=pwe_cur_value
        self.pwe.acf_bonus_cur=pwe_cur_value 
        self.pwe.vhsl_bonus_cur=pwe_cur_value
        self.pwe.acf_tossup_total=pwe_total_value
        self.pwe.acf_bonus_total=pwe_total_value
        self.pwe.vhsl_bonus_total=pwe_total_value
        self.pwe.save()
                    
        self.packet = Packet.objects.create(packet_name="Test Packet", question_set=self.qset, created_by=self.writer)
        self.packet.save()
        
        self.period = Period.objects.create(name="Test Period", packet=self.packet, period_wide_entry=self.pwe, acf_tossup_cur=period_cur_value, acf_bonus_cur=period_cur_value, vhsl_bonus_cur=period_cur_value)
        self.period.save()
        
        self.ce, created = CategoryEntry.objects.get_or_create(category_name="Test Category", category_type=CATEGORY)
        self.ce.save()
        
        self.cefd = CategoryEntryForDistribution.objects.create(
            distribution=self.dist,
            category_entry=self.ce, 
            acf_tossup_fraction=cefd_fraction,
            acf_bonus_fraction=cefd_fraction,
            vhsl_bonus_fraction=cefd_fraction,
            min_total_questions_in_period=cefd_min,
            max_total_questions_in_period=cefd_max)
        self.cefd.save()
        
        self.pwce = PeriodWideCategoryEntry.objects.create(
            period_wide_entry=self.pwe,
            category_entry_for_distribution=self.cefd,
            acf_tossup_cur_across_periods=pwce_cur_value,
            acf_bonus_cur_across_periods=pwce_cur_value,
            vhsl_bonus_cur_across_periods=pwce_cur_value,
            acf_tossup_total_across_periods=pwce_total_value,
            acf_bonus_total_across_periods=pwce_total_value,
            vhsl_bonus_total_across_periods=pwce_total_value)
        self.pwce.save()
        
        self.opce = OnePeriodCategoryEntry.objects.create(
            period=self.period,
            period_wide_category_entry=self.pwce,
            acf_tossup_cur_in_period=opce_cur_value,
            acf_bonus_cur_in_period=opce_cur_value,
            vhsl_bonus_cur_in_period=opce_cur_value)
        self.opce.save()

    # Returns 3 tuple of tuples.  The first layer of tuples is just the list of 
    # each category entry, the second is a CategoryEntry, a CategoryEntryForDistribution, 
    # a PeriodWideCategoryEntry and a OnePeriodCategoryEntry that all relate back to the CategoryEntry
    def create_generic_ce_hierarchy(self):
        
        cat_tuple = self.create_ce_and_dependencies(
            category_type=CATEGORY,
            cefd_fraction=4.2,
            cefd_min=12,
            cefd_max=13,
            pwce_cur_value=0,
            pwce_total_value=13, # TODO: Double check that this is right
            opce_cur_value=0,
            category_name="Literature")

        sub_cat_tuple1 = self.create_ce_and_dependencies(
            category_type=SUB_CATEGORY,
            cefd_fraction=2,
            cefd_min=6,
            cefd_max=6,
            pwce_cur_value=0,
            pwce_total_value=6, # TODO: Double check that this is right            
            opce_cur_value=0,
            category_name="Literature",
            sub_category_name="American")

        sub_cat_tuple2 = self.create_ce_and_dependencies(
            category_type=SUB_CATEGORY,
            cefd_fraction=2.2,
            cefd_min=6,
            cefd_max=7,
            pwce_cur_value=0,
            pwce_total_value=7, # TODO: Double check that this is right            
            opce_cur_value=0,
            category_name="Literature",
            sub_category_name="European")

        sub_sub_cat_tuple1 = self.create_ce_and_dependencies(
            category_type=SUB_SUB_CATEGORY,
            cefd_fraction=2,
            cefd_min=6,
            cefd_max=6,
            pwce_cur_value=0,
            pwce_total_value=6, # TODO: Double check that this is right            
            opce_cur_value=0,
            category_name="Literature",
            sub_category_name="American",
            sub_sub_category_name="Novels")

        sub_sub_cat_tuple2 = self.create_ce_and_dependencies(
            category_type=SUB_SUB_CATEGORY,
            cefd_fraction=1,
            cefd_min=3,
            cefd_max=3,
            pwce_cur_value=0,
            pwce_total_value=3, # TODO: Double check that this is right            
            opce_cur_value=0,
            category_name="Literature",
            sub_category_name="European",
            sub_sub_category_name="Novels")

        sub_sub_cat_tuple3 = self.create_ce_and_dependencies(
            category_type=SUB_SUB_CATEGORY,
            cefd_fraction=1.2,
            cefd_min=3,
            cefd_max=4,
            pwce_cur_value=0,
            pwce_total_value=4, # TODO: Double check that this is right            
            opce_cur_value=0,
            category_name="Literature",
            sub_category_name="European",
            sub_sub_category_name="Poetry")
            
        cats = (cat_tuple, None) # TODO: Change
        sub_cats = (sub_cat_tuple1, sub_cat_tuple2)
        sub_sub_cats = (sub_sub_cat_tuple1, sub_sub_cat_tuple2, sub_sub_cat_tuple3)
        return cats, sub_cats, sub_sub_cats

    # Creates a CategoryEntry, a CategoryEntryForDistribution, a PeriodWideCategoryEntry and a
    # OnePeriodCategoryEntry that all relate back to the CategoryEntry
    def create_ce_and_dependencies(
        self, 
        category_type, 
        cefd_fraction, 
        cefd_min, 
        cefd_max, 
        pwce_cur_value, 
        pwce_total_value, 
        opce_cur_value, 
        category_name, 
        sub_category_name=None, 
        sub_sub_category_name=None,
        dist=None,
        pwe=None,
        period=None):
            
        if (dist is None):
            dist=self.dist
            
        if (pwe is None):
            pwe=self.pwe
            
        if (period is None):
            period=self.period
            
        ce, cefd = self.create_just_ce_and_cefd(
            dist=dist,
            category_type=category_type,
            cefd_fraction=cefd_fraction,
            cefd_min=cefd_min,
            cefd_max=cefd_max,
            category_name=category_name,
            sub_category_name=sub_category_name,
            sub_sub_category_name=sub_sub_category_name)
        
        # TODO: Maybe put these into a separate method since don't always want to create them at same time as above
        pwce = PeriodWideCategoryEntry.objects.create(
            period_wide_entry=pwe,
            category_entry_for_distribution=cefd,
            acf_tossup_cur_across_periods=pwce_cur_value,
            acf_bonus_cur_across_periods=pwce_cur_value,
            vhsl_bonus_cur_across_periods=pwce_cur_value,
            acf_tossup_total_across_periods=pwce_total_value,
            acf_bonus_total_across_periods=pwce_total_value,
            vhsl_bonus_total_across_periods=pwce_total_value)
        pwce.save()
        
        opce = OnePeriodCategoryEntry.objects.create(
            period=period,
            period_wide_category_entry=pwce,
            acf_tossup_cur_in_period=opce_cur_value,
            acf_bonus_cur_in_period=opce_cur_value,
            vhsl_bonus_cur_in_period=opce_cur_value)
        opce.save()
        
        ce_tuple = (ce, cefd, pwce, opce)
        return ce_tuple
        
    # Creats a category entry and a category entry for distribution
    def create_just_ce_and_cefd(
        self,
        dist,
        category_type, 
        cefd_fraction, 
        cefd_min, 
        cefd_max,         
        category_name, 
        sub_category_name=None, 
        sub_sub_category_name=None):
            
        ce = None
        if (category_type==CATEGORY):
            ce, created = CategoryEntry.objects.get_or_create(category_name=category_name, category_type=category_type)            
        elif (category_type==SUB_CATEGORY):
            ce, created = CategoryEntry.objects.get_or_create(category_name=category_name, sub_category_name=sub_category_name, category_type=category_type)                        
        else:
            ce, created = CategoryEntry.objects.get_or_create(category_name=category_name, sub_category_name=sub_category_name, sub_sub_category_name=sub_sub_category_name, category_type=category_type)            
                    
        cefd = CategoryEntryForDistribution.objects.create(
            distribution=dist,
            category_entry=ce, 
            acf_tossup_fraction=cefd_fraction,
            acf_bonus_fraction=cefd_fraction,
            vhsl_bonus_fraction=cefd_fraction,
            min_total_questions_in_period=cefd_min,
            max_total_questions_in_period=cefd_max)
        cefd.save()
        
        return ce, cefd
    
        
    #########################################################################
    # Unit tests that don't depend on the database
    #########################################################################
                            
#    def test_is_answer(self):
#        answers = ["answer:", "Answer:", "ANSWER:", "ANSWER: _underlined_", "ANSWER: no underline", "ANSWER: <u>underline2</u>"]
#        for answer in answers:
#            self.assertTrue(is_answer(answer), msg=answer)
#        non_answers = ["question:", "answer", "ansER"]
#        for non_answer in non_answers:
#            self.assertFalse(is_answer(non_answer), msg=non_answer)
#    def test_remove_answer_label(self):
#        answers = ["ANSWER: <u><b>my answer</b></u>", "answer:      <u><b>my answer</b></u>", "Answer:\t<u><b>my answer</b></u>"]
#        for answer in answers:
#            self.assertEqual(remove_answer_label(answer), '<u><b>my answer</b></u>')
#    def test_are_special_characters_balanced(self):
#        balancedLines = ["", "No special chars", "_Underscores_", "~Italics~", "(Parens)", "_~Several_~ (items) in (one) _question_."]
#        unbalancedLines = ["_", "~", "_test__", "~~test~", "(test", "test)", "((test)", "(", ")", ")test(", "(test))"]
#        for balancedLine in balancedLines:
#            self.assertTrue(are_special_characters_balanced(balancedLine))
#        for unbalancedLine in unbalancedLines:
#            self.assertFalse(are_special_characters_balanced(unbalancedLine))
#    def test_is_bpart(self):
#        bonusParts = ['[10]', '[15]']
#        for bonusPart in bonusParts:
#            self.assertTrue(is_bpart(bonusPart), msg=bonusPart)
#        notBonusParts = ['(10)', '10', '[10', '10]', '(10]', '[10)', '[or foo]', '(not a number)', '[10.5]', '[<i>10</i>]']
#        for notBonus in notBonusParts:
#            self.assertFalse(is_bpart(notBonus), msg=notBonus)
#    def test_is_vhsl_bpart(self):
#        bonusParts = ['[V10]', '[V15]']
#        for bonusPart in bonusParts:
#            self.assertTrue(is_vhsl_bpart(bonusPart), msg=bonusPart)
#        notBonusParts = ['(V10)', '[10]', '(10)', 'V10', '[10', '10]', '(10]', '[V10)', '[or foo]', '(not a number)']
#        for notBonus in notBonusParts:
#            self.assertFalse(is_vhsl_bpart(notBonus), msg=notBonus)
#    def test_is_category(self):
#        categories = ["{History - European}, 'ANSWER: _foo_ {Literature - American}"]
#        for category in categories:
#            self.assertTrue(is_category(category), msg=category)
#        notCategories = ["answer: _foo_", '{History - World', 'History - Other}']
#        for notCategory in notCategories:
#            self.assertFalse(is_category(notCategory), msg=notCategory)
#    def test_remove_category(self):
#        self.assertEqual(remove_category('ANSWER: _foo_ {History - European}'), 'ANSWER: _foo_ ')
#        self.assertEqual(remove_category('ANSWER: _foo_ {History - European'), 'ANSWER: _foo_ {History - European')
#    def test_get_bonus_part_value(self):
#        bonusParts = ['[10]']
#        for bonusPart in bonusParts:
#            self.assertEqual(get_bonus_part_value(bonusPart), '10')
#
#    def test_get_character_count(self):
#        emptyTossup = ""
#        self.assertEqual(get_character_count(emptyTossup), 0)
#
#        noSpecialCharacters = "123456789"
#        self.assertEqual(get_character_count(noSpecialCharacters), 9)
#
#        onlySpecialCharacters = "~~()"
#        self.assertEqual(get_character_count(onlySpecialCharacters), 0)
#
#        mixed = "(~1234~) ~67~"
#        self.assertEqual(get_character_count(mixed), 3)
#
#    def test_get_formatted_question_html(self):
#        emptyLine = ""
#        self.assertEqual(get_formatted_question_html(emptyLine, False, True, False), "")
#        self.assertEqual(get_formatted_question_html(emptyLine, True, True, False), "")
#
#        noSpecialChars = "No special chars"
#        self.assertEqual(get_formatted_question_html(noSpecialChars, False, True, False), noSpecialChars)
#        self.assertEqual(get_formatted_question_html(noSpecialChars, True, True, False), noSpecialChars)
#
#        specialChars = "_Underlines_, ~italics~ and (parens).  And again _Underlines_, ~italics~ and (parens)."
#        self.assertEqual(get_formatted_question_html(specialChars, False, True, False), "_Underlines_, <i>italics</i> and <strong class="pronunciation-guide">(parens)</strong>.  And again _Underlines_, <i>italics</i> and <strong class="pronunciation-guide">(parens)</strong>.")
#        self.assertEqual(get_formatted_question_html(specialChars, True, True, False), "<u><b>Underlines</b></u>, <i>italics</i> and <strong class="pronunciation-guide">(parens)</strong>.  And again <u><b>Underlines</b></u>, <i>italics</i> and <strong class="pronunciation-guide">(parens)</strong>.")
#
#        newLinesNoParens = "(No parens).&lt;br&gt;New line."
#        self.assertEqual(get_formatted_question_html(newLinesNoParens, False, False, False), "(No parens).&lt;br&gt;New line.")
#        self.assertEqual(get_formatted_question_html(newLinesNoParens, False, False, True), "(No parens).<br />New line.")
#
#    def test_does_answerline_have_underlines(self):
#        self.assertFalse(does_answerline_have_underlines("ANSWER: Foo"))
#        self.assertTrue(does_answerline_have_underlines("ANSWER: _Foo_"))
#        self.assertTrue(does_answerline_have_underlines(""))
#
#            
#    #########################################################################
#    # Tests with database dependencies
#    #########################################################################            
#            
#    def test_parse_packet_data(self):
#        self.create_generic_distribution()
#
#        euroHistory = DistributionEntry(category="History", subcategory="European", distribution=self.dist)
#        euroHistory.save()
#
#        americanHistory = DistributionEntry(category="History", subcategory="American", distribution=self.dist)
#        americanHistory.save()
#
#        validTossup = 'This is a valid test tossup.\nANSWER: _My Answer_'
#        tossups, bonuses, tossup_errors, bonus_errors = parse_packet_data(validTossup.splitlines())
#        self.assertEqual(len(tossups), 1)
#        self.assertEqual(tossups[0].tossup_text, 'This is a valid test tossup.');
#        self.assertEqual(tossups[0].tossup_answer, '_My Answer_');
#        self.assertEqual(tossups[0].category, None);
#
#        validTossupWithCategory = 'This should be a ~European History~ tossup.\nANSWER: _Charles I_ {History - European}'
#        tossups, bonuses, tossup_errors, bonus_errors = parse_packet_data(validTossupWithCategory.splitlines())
#        self.assertEqual(len(tossups), 1)
#        self.assertEqual(tossups[0].tossup_text, 'This should be a ~European History~ tossup.');
#        self.assertEqual(tossups[0].tossup_answer, '_Charles I_ ');
#        self.assertEqual(str(tossups[0].category), 'History - European');
#
#        validBonus = 'This is a valid bonus.  For 10 points each:\n[10] Prompt 1.\nANSWER: _Answer 1_\n[10] Prompt 2.\nANSWER: _Answer 2_\n[10] Prompt 3.\nANSWER: _Answer 3_'
#        tossups, bonuses, tossup_errors, bonus_errors = parse_packet_data(validBonus.splitlines())
#        self.assertEqual(len(bonuses), 1)
#        self.assertEqual(str(bonuses[0].question_type), 'ACF-style bonus')
#        self.assertEqual(bonuses[0].leadin, 'This is a valid bonus.  For 10 points each:')
#        self.assertEqual(bonuses[0].part1_text, 'Prompt 1.')
#        self.assertEqual(bonuses[0].part1_answer, '_Answer 1_')
#        self.assertEqual(bonuses[0].part2_text, 'Prompt 2.')
#        self.assertEqual(bonuses[0].part2_answer, '_Answer 2_')
#        self.assertEqual(bonuses[0].part3_text, 'Prompt 3.')
#        self.assertEqual(bonuses[0].part3_answer, '_Answer 3_')
#
#        validBonusWithCategory = validBonus + " {History - American}"
#        tossups, bonuses, tossup_errors, bonus_errors = parse_packet_data(validBonusWithCategory.splitlines())
#        self.assertEqual(len(bonuses), 1)
#        self.assertEqual(str(bonuses[0].category), "History - American");
#
#        validVHSLBonus = '[V10] This is a valid VHSL bonus.\nANSWER: _VHSL Answer_'
#        tossups, bonuses, tossup_errors, bonus_errors = parse_packet_data(validVHSLBonus.splitlines())
#        self.assertEqual(len(bonuses), 1)
#        self.assertEqual(str(bonuses[0].question_type), 'VHSL bonus')
#        self.assertEqual(bonuses[0].leadin, '')
#        self.assertEqual(bonuses[0].part1_text, 'This is a valid VHSL bonus.')
#        self.assertEqual(bonuses[0].part1_answer, '_VHSL Answer_')
#
#        multipleQuestions = 'This is tossup 1.\nANSWER: _Tossup 1 Answer_\n[V10] This is a VHSL bonus.\nANSWER: _VHSL Answer_\nThis is another tossup.\nANSWER: _Tossup 2 Answer_'
#        multipleQuestions += '\n' + validBonus
#        tossups, bonuses, tossup_errors, bonus_errors = parse_packet_data(multipleQuestions.splitlines())
#        self.assertEqual(len(bonuses), 2)
#        self.assertEqual(len(tossups), 2)
#        self.assertEqual(tossups[0].tossup_text, 'This is tossup 1.')
#        self.assertEqual(tossups[0].tossup_answer, '_Tossup 1 Answer_')
#        self.assertEqual(tossups[1].tossup_text, 'This is another tossup.')
#        self.assertEqual(tossups[1].tossup_answer, '_Tossup 2 Answer_')
#        self.assertEqual(bonuses[0].part1_text, 'This is a VHSL bonus.')
#        self.assertEqual(bonuses[0].part1_answer, '_VHSL Answer_')
#        self.assertEqual(str(bonuses[0].question_type), 'VHSL bonus')
#        self.assertEqual(str(bonuses[1].question_type), 'ACF-style bonus')
#        self.assertEqual(bonuses[1].leadin, 'This is a valid bonus.  For 10 points each:')
#        self.assertEqual(bonuses[1].part1_text, 'Prompt 1.')
#        self.assertEqual(bonuses[1].part1_answer, '_Answer 1_')
#        self.assertEqual(bonuses[1].part2_text, 'Prompt 2.')
#        self.assertEqual(bonuses[1].part2_answer, '_Answer 2_')
#        self.assertEqual(bonuses[1].part3_text, 'Prompt 3.')
#        self.assertEqual(bonuses[1].part3_answer, '_Answer 3_')
#
#        multipleQuestionsBlankLines = 'This is tossup 1.\nANSWER: _Tossup 1 Answer_\n\n\nThis is tossup 2.\nANSWER: _Tossup 2 Answer_'
#        tossups, bonuses, tossup_errors, bonus_errors = parse_packet_data(multipleQuestionsBlankLines.splitlines())
#        self.assertEqual(len(bonuses), 0)
#        self.assertEqual(len(tossups), 2)
#        self.assertEqual(tossups[0].tossup_text, 'This is tossup 1.')
#        self.assertEqual(tossups[0].tossup_answer, '_Tossup 1 Answer_')
#        self.assertEqual(tossups[1].tossup_text, 'This is tossup 2.')
#        self.assertEqual(tossups[1].tossup_answer, '_Tossup 2 Answer_')
#
#        tossupWithoutAnswer = 'This is not a valid tossup.  It does not have an answer.'
#        tossups, bonuses, tossup_errors, bonus_errors = parse_packet_data(tossupWithoutAnswer.splitlines())
#        self.assertEqual(len(tossups), 0)
#        self.assertEqual(len(bonuses), 0)
#
#        tossupWithLineBreaks = 'This is not a valid tossup.\nIt has a line break before its answer.\nANSWER: _foo_'
#        tossups, bonuses, tossup_errors, bonus_errors = parse_packet_data(tossupWithLineBreaks.splitlines())
#        self.assertEqual(len(tossups), 1)
#        self.assertEqual(tossups[0].tossup_text, 'It has a line break before its answer.');
#        self.assertEqual(tossups[0].tossup_answer, '_foo_');
#        self.assertEqual(len(bonuses), 0)
#
#        tossupWithoutQuestion = 'ANSWER: This is an answer line without a question'
#        tossups, bonuses, tossup_errors, bonus_errors = parse_packet_data(tossupWithoutQuestion.splitlines())
#        self.assertEqual(len(tossups), 0)
#        self.assertEqual(len(bonuses), 0)
#        self.assertEqual(len(tossup_errors), 1)
#
#        tossupWithSingleQuotes = "This is a tossup with 'single quotes' in it.\nANSWER: '_Single Quoted Answer_'"
#        tossups, bonuses, tossup_errors, bonus_errors = parse_packet_data(tossupWithSingleQuotes.splitlines())
#        self.assertEqual(len(tossups), 1)
#        self.assertEqual(tossups[0].tossup_text, "This is a tossup with &#39;single quotes&#39; in it.");
#        self.assertEqual(tossups[0].tossup_answer, "&#39;_Single Quoted Answer_&#39;");
#
#        tossupWithDoubleQuotes = 'This is a tossup with "double quotes" in it.\nANSWER: "_Double Quoted Answer_"'
#        tossups, bonuses, tossup_errors, bonus_errors = parse_packet_data(tossupWithDoubleQuotes.splitlines())
#        self.assertEqual(len(tossups), 1)
#        self.assertEqual(tossups[0].tossup_text, 'This is a tossup with &quot;double quotes&quot; in it.');
#        self.assertEqual(tossups[0].tossup_answer, '&quot;_Double Quoted Answer_&quot;');
#
#        tossupWithDoubleQuotes = 'This is a tossup with an <i>italic tag</i> in it.\nANSWER: <i>_Italic Answer_</i>'
#        tossups, bonuses, tossup_errors, bonus_errors = parse_packet_data(tossupWithDoubleQuotes.splitlines())
#        self.assertEqual(len(tossups), 1)
#        self.assertEqual(tossups[0].tossup_text, 'This is a tossup with an &lt;i&gt;italic tag&lt;/i&gt; in it.');
#        self.assertEqual(tossups[0].tossup_answer, '&lt;i&gt;_Italic Answer_&lt;/i&gt;');
#
#        bonusWithNonIntegerValues = 'This is a bonus with non-integer values.  For 10 points each:\n[A] Prompt 1.\nANSWER: _Answer 1_\n[10.5] Prompt 2.\nANSWER: _Answer 2_\n[10C] Prompt 3.\nANSWER: _Answer 3_'
#        tossups, bonuses, tossup_errors, bonus_errors = parse_packet_data(bonusWithNonIntegerValues.splitlines())
#        self.assertEqual(len(bonuses), 0)
#
#        bonusWithHtmlValues = 'This is a bonus with html values.  For 10 points each:\n[<i>10</i>] Prompt 1.\nANSWER: _Answer 1_\n[10] Prompt 2.\nANSWER: _Answer 2_\n[<i>10</i>] Prompt 3.\nANSWER: _Answer 3_'
#        tossups, bonuses, tossup_errors, bonus_errors = parse_packet_data(bonusWithHtmlValues.splitlines())
#        self.assertEqual(len(bonuses), 0)
#
#        bonusWithQuotesAndHtml = 'This is a <i>valid</i> "bonus".  For 10 points each:\n[10] <i>Prompt 1</i>.\nANSWER: "_Answer 1_"\n[10] "Prompt 2."\nANSWER: <i>_Answer 2_</i>\n[10] <i>Prompt 3.</i>\nANSWER: <i>_Answer 3_</i>'
#        tossups, bonuses, tossup_errors, bonus_errors = parse_packet_data(bonusWithQuotesAndHtml.splitlines())
#        self.assertEqual(len(bonuses), 1)
#        self.assertEqual(str(bonuses[0].question_type), 'ACF-style bonus')
#        self.assertEqual(bonuses[0].leadin, 'This is a &lt;i&gt;valid&lt;/i&gt; &quot;bonus&quot;.  For 10 points each:')
#        self.assertEqual(bonuses[0].part1_text, '&lt;i&gt;Prompt 1&lt;/i&gt;.')
#        self.assertEqual(bonuses[0].part1_answer, '&quot;_Answer 1_&quot;')
#        self.assertEqual(bonuses[0].part2_text, '&quot;Prompt 2.&quot;')
#        self.assertEqual(bonuses[0].part2_answer, '&lt;i&gt;_Answer 2_&lt;/i&gt;')
#        self.assertEqual(bonuses[0].part3_text, '&lt;i&gt;Prompt 3.&lt;/i&gt;')
#        self.assertEqual(bonuses[0].part3_answer, '&lt;i&gt;_Answer 3_&lt;/i&gt;')
#
#        tossupWithUnbalancedSpecialCharsInQuestion = 'This is a tossup question with ~an unclosed tilde.\nANSWER: _foo_'
#        tossups, bonuses, tossup_errors, bonus_errors = parse_packet_data(tossupWithUnbalancedSpecialCharsInQuestion.splitlines())
#        self.assertEqual(len(tossup_errors), 1)
#
#        tossupWithUnbalancedSpecialCharsInAnswer = 'This is a tossup question.\nANSWER: _unclosed answer'
#        tossups, bonuses, tossup_errors, bonus_errors = parse_packet_data(tossupWithUnbalancedSpecialCharsInAnswer.splitlines())
#        self.assertEqual(len(tossup_errors), 1)
#
#        bonusWithUnbalancedSpecialCharsInLeadin = 'This is a bonus with (unbalanced leadin characters.  For 10 points each:\n[10] <i>Prompt 1</i>.\nANSWER: "_Answer 1_"\n[10] "Prompt 2."\nANSWER: <i>_Answer 2_</i>\n[10] <i>Prompt 3.</i>\nANSWER: <i>_Answer 3_</i>'
#        tossups, bonuses, tossup_errors, bonus_errors = parse_packet_data(bonusWithUnbalancedSpecialCharsInLeadin.splitlines())
#        self.assertEqual(len(bonus_errors), 1)
#
#        bonusWithUnbalancedSpecialCharsInPrompts = 'This is a bonus with unbalanced prompt characters.  For 10 points each:\n[10] ~Prompt 1.\nANSWER: "_Answer 1_"\n[10] "Prompt 2."\nANSWER: <i>_Answer 2_</i>\n[10] <i>Prompt 3.</i>\nANSWER: <i>_Answer 3_</i>'
#        tossups, bonuses, tossup_errors, bonus_errors = parse_packet_data(bonusWithUnbalancedSpecialCharsInPrompts.splitlines())
#        self.assertEqual(len(bonus_errors), 1)
#
#        bonusWithUnbalancedSpecialCharsInAnswers = 'This is a bonus with unbalanced answer characters.  For 10 points each:\n[10] Prompt 1.\nANSWER: "_Answer 1_"\n[10] "Prompt 2."\nANSWER: _Answer 2\n[10] <i>Prompt 3.</i>\nANSWER: <i>_Answer 3_</i>'
#        tossups, bonuses, tossup_errors, bonus_errors = parse_packet_data(bonusWithUnbalancedSpecialCharsInAnswers.splitlines())
#        self.assertEqual(len(bonus_errors), 1)
#
#    def test_tossup_to_html(self):
#        self.create_generic_distribution()
#
#        acfTossup, created = QuestionType.objects.get_or_create(question_type="ACF-style tossup")
#
#        americanHistory = DistributionEntry(category="History", subcategory="American", distribution=self.dist)
#        americanHistory.save()
#
#        tossup_text = "(Test) ~tossup~."
#        tossup_answer = "_test answer_"
#        tossup_no_category_no_question_type = Tossup(tossup_text=tossup_text, tossup_answer=tossup_answer)
#        expectedOutput = "<p><strong>(Test)</strong> <i>tossup</i>.<br />ANSWER: <u><b>test answer</b></u></p>"
#        self.assertEqual(tossup_no_category_no_question_type.to_html(), expectedOutput)
#        self.assertEqual(tossup_no_category_no_question_type.to_html(include_category=True), expectedOutput)
#        expectedOutputWithCharCount = expectedOutput + "<p><strong>Character Count:</strong> 8</p>"
#        self.assertEqual(tossup_no_category_no_question_type.to_html(include_character_count=True), expectedOutputWithCharCount)
#
#        tossup_with_category = Tossup(tossup_text=tossup_text, tossup_answer=tossup_answer, category=americanHistory)
#        self.assertEqual(tossup_with_category.to_html(), expectedOutput)
#        expectedOutputWithCategory = "<p><strong>(Test)</strong> <i>tossup</i>.<br />ANSWER: <u><b>test answer</b></u></p><p><strong>Category:</strong> History - American</p>"
#        self.assertEqual(tossup_with_category.to_html(include_category=True), expectedOutputWithCategory)
#
#    def test_bonus_to_html(self):
#        self.create_generic_distribution()
#
#        acfBonus, created = QuestionType.objects.get_or_create(question_type="ACF-style bonus")
#
#        vhslBonus, created = QuestionType.objects.get_or_create(question_type="VHSL bonus")
#
#        americanHistory = DistributionEntry(category="History", subcategory="American", distribution=self.dist)
#        americanHistory.save()
#
#        leadin = "Leadin with ~italics~ and (parens) and _underlines_."
#        part1_text = "Part 1 with ~italics~ and (parens) and _underlines_."
#        part1_answer = "_~Answer 1~_ [or foo (bar)]"
#        part2_text = "Part 2."
#        part2_answer = "_answer 2_"
#        part3_text = "Part 3."
#        part3_answer = "_answer 3_"
#        acf_bonus_no_category = Bonus(leadin=leadin, part1_text=part1_text, part1_answer=part1_answer, part2_text=part2_text, part2_answer=part2_answer, part3_text=part3_text, part3_answer=part3_answer)
#        expectedOutput = "<p>Leadin with <i>italics</i> and <strong class="pronunciation-guide">(parens)</strong> and _underlines_.<br />"
#        expectedOutput += "[10] Part 1 with <i>italics</i> and <strong class="pronunciation-guide">(parens)</strong> and _underlines_.<br />"
#        expectedOutput += "ANSWER: <u><b><i>Answer 1</i></b></u> [or foo <strong>(bar)</strong>]<br />"
#        expectedOutput += "[10] Part 2.<br />"
#        expectedOutput += "ANSWER: <u><b>answer 2</b></u><br />"
#        expectedOutput += "[10] Part 3.<br />"
#        expectedOutputWithoutLastLine = expectedOutput
#        expectedOutput += "ANSWER: <u><b>answer 3</b></u></p>"
#        self.assertEqual(acf_bonus_no_category.to_html(), expectedOutput)
#        self.assertEqual(acf_bonus_no_category.to_html(include_category=True), expectedOutput)
#        expectedOutputWithCharCount = expectedOutput + "<p><strong>Character Count:</strong> "
#        expectedOutputWithCharCount += "98</p>"
#        self.assertEqual(acf_bonus_no_category.to_html(include_character_count=True), expectedOutputWithCharCount)
#        acf_bonus_no_category.category = americanHistory
#        self.assertEqual(acf_bonus_no_category.to_html(), expectedOutput)
#        expectedOutputWithCategory = expectedOutputWithoutLastLine + "ANSWER: <u><b>answer 3</b></u></p><p><strong>Category:</strong> History - American</p>"
#        self.assertEqual(acf_bonus_no_category.to_html(include_category=True), expectedOutputWithCategory)
#
#        vhsl_bonus_no_category = Bonus(part1_text=part1_text, part1_answer=part1_answer, question_type=vhslBonus)
#        expectedVhslOutput = "<p>Part 1 with <i>italics</i> and <strong class="pronunciation-guide">(parens)</strong> and _underlines_.<br />"
#        expectedVhslOutput += "ANSWER: <u><b><i>Answer 1</i></b></u> [or foo <strong>(bar)</strong>]</p>"
#        self.assertEqual(vhsl_bonus_no_category.to_html(), expectedVhslOutput)
#        vhsl_bonus_no_category.category = americanHistory
#        self.assertEqual(vhsl_bonus_no_category.to_html(), expectedVhslOutput)
#        expectedVhslOutput = "<p>Part 1 with <i>italics</i> and <strong class="pronunciation-guide">(parens)</strong> and _underlines_.<br />"
#        expectedVhslOutput += "ANSWER: <u><b><i>Answer 1</i></b></u> [or foo <strong>(bar)</strong>]</p><p><strong>Category:</strong> History - American</p>"
#        self.assertEqual(vhsl_bonus_no_category.to_html(include_category=True), expectedVhslOutput)

    def test_character_count_ignores_moderator_instructions(self):
        base = "This is a question about a thing that does stuff."
        base_count = get_character_count(base, True)

        # Leading directive sentences are excluded
        for directive in ("Description acceptable. ",
                          "A description is acceptable. ",
                          "Note to moderator: read the answerline carefully. ",
                          "Note to players: Description acceptable. ",
                          "Two answers required. "):
            self.assertEqual(get_character_count(directive + base, True), base_count, msg=directive)
            self.assertEqual(get_character_count(directive + base, False), len(base), msg=directive)

        # Trailing directives and inline markers are excluded
        self.assertEqual(get_character_count(base + " You have ten seconds.", True), base_count)
        self.assertEqual(get_character_count(base.replace("about", "[emphasize] about"), True), base_count)

        # Mid-sentence content mentions still count
        content = "Critics found the description acceptable in most reviews."
        self.assertEqual(get_character_count(content, True), len(content))

        # Markup-wrapped directives are also excluded
        self.assertEqual(get_character_count("~Description acceptable.~ " + base, True), base_count)

    def test_category_entry_get_requirements_methods(self):
        self.create_generic_period(cefd_fraction=2.2)
                
        self.assertEqual(self.cefd.get_acf_tossup_integer(), 2)
        self.assertEqual(self.cefd.get_acf_tossup_remainder(), 0.20)
        self.assertEqual(self.cefd.get_acf_tossup_upper_bound(), 3)
        self.assertEqual(self.cefd.get_acf_bonus_integer(), 2)
        self.assertEqual(self.cefd.get_acf_bonus_remainder(), 0.20)
        self.assertEqual(self.cefd.get_acf_bonus_upper_bound(), 3)        
        self.assertEqual(self.cefd.get_vhsl_bonus_integer(), 2)
        self.assertEqual(self.cefd.get_vhsl_bonus_remainder(), 0.20)
        self.assertEqual(self.cefd.get_vhsl_bonus_upper_bound(), 3)
        
        self.create_generic_period(cefd_fraction=1)
                
        self.assertEqual(self.cefd.get_acf_tossup_integer(), 1)
        self.assertEqual(self.cefd.get_acf_tossup_remainder(), 0)
        self.assertEqual(self.cefd.get_acf_tossup_upper_bound(), 1)        
        self.assertEqual(self.cefd.get_acf_bonus_integer(), 1)
        self.assertEqual(self.cefd.get_acf_bonus_remainder(), 0)
        self.assertEqual(self.cefd.get_acf_bonus_upper_bound(), 1)                
        self.assertEqual(self.cefd.get_vhsl_bonus_integer(), 1)
        self.assertEqual(self.cefd.get_vhsl_bonus_remainder(), 0)
        self.assertEqual(self.cefd.get_vhsl_bonus_upper_bound(), 1)        
    
    def test_sub_sub_category_to_string(self):
        self.create_generic_distribution()
        
        category_entry = CategoryEntry(category_type=CATEGORY, category_name="Arts")
        category_entry.save()

        sub_category_entry = CategoryEntry(category_type=SUB_CATEGORY, category_name="Arts", sub_category_name="Opera")
        sub_category_entry.save()

        sub_sub_category_entry = CategoryEntry(category_type=SUB_SUB_CATEGORY, category_name="Arts", sub_category_name="Opera", sub_sub_category_name="Baroque")
        sub_sub_category_entry.save()
        
        self.assertEqual(str(category_entry), "Arts")
        self.assertEqual(str(sub_category_entry), "Arts - Opera")                
        self.assertEqual(str(sub_sub_category_entry), "Arts - Opera - Baroque")

    def test_period_wide_entry_reset_current_values(self):        
        self.create_generic_qset()
        
        pwe = PeriodWideEntry.objects.create(period_type=ACF_REGULAR_PERIOD, question_set=self.qset, distribution=self.dist, acf_tossup_cur=5, acf_bonus_cur=5, vhsl_bonus_cur=5, acf_tossup_total=10, acf_bonus_total=10, vhsl_bonus_total=10)
        pwe.save()
        pwe.reset_current_values()
        self.assertEqual(pwe.acf_tossup_cur, 0)
        self.assertEqual(pwe.acf_bonus_cur, 0)
        self.assertEqual(pwe.vhsl_bonus_cur, 0)
        
    def test_period_reset_current_values(self):
        self.create_generic_period()
        self.period.reset_current_values()
        self.assertEqual(self.period.acf_tossup_cur, 0)
        self.assertEqual(self.period.acf_bonus_cur, 0)
        self.assertEqual(self.period.vhsl_bonus_cur, 0)        

    def test_period_wide_category_entry_reset_current_values(self):
        self.create_generic_period()
        self.pwce.reset_current_values()        
        self.assertEqual(self.pwce.acf_tossup_cur_across_periods, 0)
        self.assertEqual(self.pwce.acf_bonus_cur_across_periods, 0)
        self.assertEqual(self.pwce.vhsl_bonus_cur_across_periods, 0)

    def test_period_wide_category_entry_reset_total_values(self):
        self.create_generic_period()
        self.pwce.reset_total_values()
        self.assertEqual(self.pwce.acf_tossup_total_across_periods, 0)
        self.assertEqual(self.pwce.acf_bonus_total_across_periods, 0)
        self.assertEqual(self.pwce.vhsl_bonus_total_across_periods, 0)

    def test_period_wide_category_entry_get_category_type(self):
        self.create_generic_period()
        self.assertEqual(self.pwce.get_category_type(), CATEGORY)        
                
    def test_one_period_category_entry_get_linked_category_entry_for_distribution(self):
        self.create_generic_period()
        linked_entry = self.opce.get_linked_category_entry_for_distribution()
        self.assertEqual(linked_entry, self.cefd)
    
    def test_one_period_category_entry_get_total_questions_all_types(self):
        self.create_generic_period(opce_cur_value=10)
        self.assertEqual(self.opce.get_total_questions_all_types(), 30)
        
    def test_one_period_category_entry_is_over_min_limits(self):
        self.create_generic_period(cefd_fraction=5, opce_cur_value=10)
        self.assertEqual(self.opce.is_over_min_acf_tossup_limit(), True)
        self.assertEqual(self.opce.is_over_min_acf_bonus_limit(), True)
        self.assertEqual(self.opce.is_over_min_vhsl_bonus_limit(), True)
        
        self.create_generic_period(cefd_fraction=4.2, opce_cur_value=4)
        self.assertEqual(self.opce.is_over_min_acf_tossup_limit(), False)
        self.assertEqual(self.opce.is_over_min_acf_bonus_limit(), False)
        self.assertEqual(self.opce.is_over_min_vhsl_bonus_limit(), False)
        
        self.create_generic_period(cefd_fraction=5, opce_cur_value=5)
        self.assertEqual(self.opce.is_over_min_acf_tossup_limit(), False)
        self.assertEqual(self.opce.is_over_min_acf_bonus_limit(), False)
        self.assertEqual(self.opce.is_over_min_vhsl_bonus_limit(), False)        
        
    def test_one_period_category_entry_is_under_max_total_questions_limit(self):
        # It creates (3*10=30) total questions by default, and we've said the max total is 5
        self.create_generic_period(opce_cur_value=10, cefd_max=5)
        self.assertEqual(self.opce.is_under_max_total_questions_limit(), False)
                
        # Now try when the max total is 40        
        self.create_generic_period(opce_cur_value=10, cefd_max=40)
        self.assertEqual(self.opce.is_under_max_total_questions_limit(), True)
        
    def test_one_period_category_entry_is_over_min_total_questions_limit(self):
        # It creates (3*10=30) total questions by default, and we've said the min total is 5
        self.create_generic_period(opce_cur_value=10, cefd_min=5)        
        self.assertEqual(self.opce.is_over_min_total_questions_limit(), True)
                
        # Now try when the min total is 40
        self.create_generic_period(opce_cur_value=10, cefd_min=40)
        self.assertEqual(self.opce.is_over_min_total_questions_limit(), False)
        
    def test_one_period_category_entry_reset_current_values(self):
        self.create_generic_period(opce_cur_value=10)
        self.opce.reset_current_values()
        self.assertEqual(self.opce.acf_tossup_cur_in_period, 0)
        self.assertEqual(self.opce.acf_bonus_cur_in_period, 0)
        self.assertEqual(self.opce.vhsl_bonus_cur_in_period, 0)
    
    #############################################################
    # Packetizer Tests
    #############################################################

    def test_get_parents_from_category_entry(self):
        # Just category entry
        dist = Distribution.objects.create(name="new_distribution", acf_tossup_per_period_count=20, acf_bonus_per_period_count=20, vhsl_bonus_per_period_count=20)
        dist.save()

        dist2 = Distribution.objects.create(name="new_distribution2", acf_tossup_per_period_count=20, acf_bonus_per_period_count=20, vhsl_bonus_per_period_count=20)
        dist2.save()
        
        ce, created = CategoryEntry.objects.get_or_create(category_name="AHistory", category_type=CATEGORY)
        ce.save()
        
        c, sc, ssc = get_parents_from_category_entry(ce)
        self.assertEqual(c, ce)
        self.assertEqual(sc, None)
        self.assertEqual(ssc, None)
        
        # From a subcat with a parent
        
        subcat1, created = CategoryEntry.objects.get_or_create(category_name="AHistory", sub_category_name="European", category_type=SUB_CATEGORY)
        subcat1.save()
        
        c, sc, ssc = get_parents_from_category_entry(subcat1)
        self.assertEqual(c, ce)
        self.assertEqual(sc, subcat1)
        self.assertEqual(ssc, None)        
        
        # From a subcat without a parent for some reason
        
        subcat2, created = CategoryEntry.objects.get_or_create(category_name="ALiterature", sub_category_name="European", category_type=SUB_CATEGORY)
        subcat2.save()
        c, sc, ssc = get_parents_from_category_entry(subcat2)
        self.assertEqual(c, None)
        self.assertEqual(sc, subcat2)
        self.assertEqual(ssc, None)
                
        # From a subsubcat with valid parents
        
        subsubcat1, created = CategoryEntry.objects.get_or_create(category_name="AHistory", sub_category_name="European", sub_sub_category_name="British", category_type=SUB_SUB_CATEGORY)
        subsubcat1.save()
        c, sc, ssc = get_parents_from_category_entry(subsubcat1)                
        self.assertEqual(c, ce)
        self.assertEqual(sc, subcat1)
        self.assertEqual(ssc, subsubcat1)
        
        # From a subsubcat without a parent
        
        subsubcat2, created = CategoryEntry.objects.get_or_create(category_name="AGeography", sub_category_name="World", sub_sub_category_name="French", category_type=SUB_SUB_CATEGORY)
        subsubcat2.save()
        c, sc, ssc = get_parents_from_category_entry(subsubcat2)                        
        self.assertEqual(c, None)
        self.assertEqual(sc, None)
        self.assertEqual(ssc, subsubcat2)

    def test_get_period_entries_from_category_entry(self):
        # Create a category entry and a period
        self.create_generic_period()
        
        ce = CategoryEntry(category_name="History", category_type=CATEGORY)
        ce.save()
                
        pwce, opce = get_period_entries_from_category_entry(self.ce, self.period)
        self.assertEqual(pwce, self.pwce)
        self.assertEqual(opce, self.opce)

    def test_get_parents_from_period_wide_category_entry(self):
        self.create_generic_period()
        
        # (ce, cefd, pwce, opce)
        cats, sub_cats, sub_sub_cats = self.create_generic_ce_hierarchy()
        
        # Try for category
        cat, subcat, subsubcat = get_parents_from_period_wide_category_entry(cats[0][2])
        self.assertEqual(cat, cats[0][2])
        self.assertEqual(subcat, None)
        self.assertEqual(subsubcat, None)
        
        # Try for sub category
        cat, subcat, subsubcat = get_parents_from_period_wide_category_entry(sub_cats[0][2])
        self.assertEqual(cat, cats[0][2])
        self.assertEqual(subcat, sub_cats[0][2])
        self.assertEqual(subsubcat, None)
        
        # Try for sub sub category "Literature - American - Novels" option 
        cat, subcat, subsubcat = get_parents_from_period_wide_category_entry(sub_sub_cats[0][2])
        self.assertEqual(cat, cats[0][2])
        self.assertEqual(subcat, sub_cats[0][2])
        self.assertEqual(subsubcat, sub_sub_cats[0][2])

        # Try for sub sub category "Literature - European - Poetry" option
        cat, subcat, subsubcat = get_parents_from_period_wide_category_entry(sub_sub_cats[2][2])
        self.assertEqual(cat, cats[0][2])
        self.assertEqual(subcat, sub_cats[1][2])
        self.assertEqual(subsubcat, sub_sub_cats[2][2])

    def test_get_parents_from_category_entry_for_distribution(self):
        self.create_generic_period()
        
        # (ce, cefd, pwce, opce)
        cats, sub_cats, sub_sub_cats = self.create_generic_ce_hierarchy()

        # Try for category
        cat, subcat, subsubcat = get_parents_from_category_entry_for_distribution(cats[0][1])
        self.assertEqual(cat, cats[0][1])
        self.assertEqual(subcat, None)
        self.assertEqual(subsubcat, None)
        
        # Try for sub category
        cat, subcat, subsubcat = get_parents_from_category_entry_for_distribution(sub_cats[0][1])
        self.assertEqual(cat, cats[0][1])
        self.assertEqual(subcat, sub_cats[0][1])
        self.assertEqual(subsubcat, None)
        
        # Try for sub sub category "Literature - American - Novels" option 
        cat, subcat, subsubcat = get_parents_from_category_entry_for_distribution(sub_sub_cats[0][1])
        self.assertEqual(cat, cats[0][1])
        self.assertEqual(subcat, sub_cats[0][1])
        self.assertEqual(subsubcat, sub_sub_cats[0][1])

        # Try for sub sub category "Literature - European - Poetry" option
        cat, subcat, subsubcat = get_parents_from_category_entry_for_distribution(sub_sub_cats[2][1])
        self.assertEqual(cat, cats[0][1])
        self.assertEqual(subcat, sub_cats[1][1])
        self.assertEqual(subsubcat, sub_sub_cats[2][1])        

    def test_get_fraction_array(self):
        self.create_generic_period()
                
        fractions = get_fraction_array(self.pwce, 1)
        self.assertEqual(len(fractions), 0)

        fractions = get_fraction_array(self.pwce, 1.101)
        self.assertEqual(len(fractions), 101)        

#        if (c_pwce is not None and c_pwce.acf_tossup_cur_across_periods >= c_pwce.acf_tossup_total_across_periods):
#            return False
#                
#        if (c_opce is not None and c_opce.is_over_min_acf_tossup_limit()):
#            return False
#
#        if (c_opce is not None and c_opce.is_over_min_total_questions_limit()):
#            return False
#        
#        if (sc_pwce is not None and sc_pwce.acf_tossup_cur_across_periods >= sc_pwce.acf_tossup_total_across_periods):
#            return False
#            
#        if (sc_opce is not None and sc_opce.is_over_min_acf_tossup_limit()):
#            return False
#
#        if (sc_opce is not None and sc_opce.is_over_min_total_questions_limit()):
#            return False
#
#        if (ssc_pwce is not None and ssc_pwce.acf_tossup_cur_across_periods >= ssc_pwce.acf_tossup_total_across_periods):
#            return False
#            
#        if (ssc_opce is not None and ssc_opce.is_over_min_acf_tossup_limit()):
#            return False
#
#        if (ssc_opce is not None and ssc_opce.is_over_min_total_questions_limit()):
#            return False
        
        
        pass


class AutoPacketizeTests(TestCase):
    """Tests for packetizer.auto_packetize."""

    def setUp(self):
        self.user = User.objects.create_user(username="packetizeuser", password="top_secret", email="qems2test@gmail.com")
        self.writer = Writer.objects.get(user=self.user.id)

        self.dist = Distribution.objects.create(name="Packetize Test Distribution")
        self.qset = QuestionSet.objects.create(
            name="Packetize Test Set", date=timezone.now(), host="host", address="",
            owner=self.writer, num_packets=4, distribution=self.dist,
            tossups_per_packet=6, bonuses_per_packet=6)

        self.entries = {}
        for category, subcategory in [
                ('History', 'European'), ('History', 'American'),
                ('Literature', 'American'), ('Literature', 'European'),
                ('Science', 'Biology'),
                ('Fine Arts', 'Visual'), ('Fine Arts', 'Music')]:
            entry = DistributionEntry.objects.create(
                distribution=self.dist, category=category, subcategory=subcategory)
            self.entries[(category, subcategory)] = entry

    def _add_tossup(self, entry, text):
        return Tossup.objects.create(
            author=self.writer, question_set=self.qset, tossup_text=text,
            tossup_answer='_answer_', category=entry,
            created_date=datetime.now(), last_changed_date=datetime.now())

    def _add_bonus(self, entry, text):
        return Bonus.objects.create(
            author=self.writer, question_set=self.qset, leadin=text,
            part1_text='p1', part1_answer='_a1_', part2_text='p2', part2_answer='_a2_',
            part3_text='p3', part3_answer='_a3_', category=entry,
            created_date=datetime.now(), last_changed_date=datetime.now())

    def _create_questions(self):
        # Regular load for 4 packets of 6/6:
        # History 2/2 per packet, Literature 1.5/1.5, Science 1/1, Fine Arts 1.5/1.5
        for i in range(4):
            self._add_tossup(self.entries[('History', 'European')], 'HE tu {0}'.format(i))
            self._add_tossup(self.entries[('History', 'American')], 'HA tu {0}'.format(i))
            self._add_bonus(self.entries[('History', 'European')], 'HE bs {0}'.format(i))
            self._add_bonus(self.entries[('History', 'American')], 'HA bs {0}'.format(i))
            self._add_tossup(self.entries[('Science', 'Biology')], 'SB tu {0}'.format(i))
            self._add_bonus(self.entries[('Science', 'Biology')], 'SB bs {0}'.format(i))
        for i in range(3):
            self._add_tossup(self.entries[('Literature', 'American')], 'LA tu {0}'.format(i))
            self._add_tossup(self.entries[('Literature', 'European')], 'LE tu {0}'.format(i))
            self._add_bonus(self.entries[('Literature', 'American')], 'LA bs {0}'.format(i))
            self._add_bonus(self.entries[('Literature', 'European')], 'LE bs {0}'.format(i))
            self._add_tossup(self.entries[('Fine Arts', 'Visual')], 'FV tu {0}'.format(i))
            self._add_tossup(self.entries[('Fine Arts', 'Music')], 'FM tu {0}'.format(i))
            self._add_bonus(self.entries[('Fine Arts', 'Visual')], 'FV bs {0}'.format(i))
            self._add_bonus(self.entries[('Fine Arts', 'Music')], 'FM bs {0}'.format(i))
        # Two extra history tossups that exceed every cap -> tiebreakers
        self._add_tossup(self.entries[('History', 'European')], 'HE extra 1')
        self._add_tossup(self.entries[('History', 'American')], 'HA extra 2')

    def _quotas(self):
        PacketizationEntry.objects.create(
            question_set=self.qset, path='History', depth=0,
            min_tossups=2, max_tossups=2, min_bonuses=2, max_bonuses=2)
        PacketizationEntry.objects.create(
            question_set=self.qset, path='Literature', depth=0,
            min_tossups=Decimal('1.5'), max_tossups=Decimal('1.5'),
            min_bonuses=Decimal('1.5'), max_bonuses=Decimal('1.5'))
        PacketizationEntry.objects.create(
            question_set=self.qset, path='Science', depth=0,
            min_tossups=1, max_tossups=1, min_bonuses=1, max_bonuses=1)
        PacketizationEntry.objects.create(
            question_set=self.qset, path='Fine Arts', depth=0,
            min_tossups=Decimal('1.5'), max_tossups=Decimal('1.5'),
            min_bonuses=Decimal('1.5'), max_bonuses=Decimal('1.5'))
        return build_quota_dict(self.qset)

    def _packetize(self, seed=42):
        report = auto_packetize(self.qset, 4, 6, 6, self._quotas(), created_by=self.writer, seed=seed)
        packets = list(Packet.objects.filter(question_set=self.qset).order_by('packet_name'))
        return report, packets

    def test_auto_packetize_basic_structure(self):
        self._create_questions()
        report, packets = self._packetize()

        self.assertEqual(len(packets), 4)
        for packet in packets:
            regular_tossups = Tossup.objects.filter(packet=packet, question_number__lte=6)
            regular_bonuses = Bonus.objects.filter(packet=packet, question_number__lte=6)
            self.assertEqual(regular_tossups.count(), 6)
            self.assertEqual(regular_bonuses.count(), 6)
            # Sequential numbering from 1
            tu_numbers = sorted(t.question_number for t in Tossup.objects.filter(packet=packet))
            self.assertEqual(tu_numbers[:6], [1, 2, 3, 4, 5, 6])

        # Nothing left unassigned
        self.assertEqual(Tossup.objects.filter(question_set=self.qset, packet=None).count(), 0)
        self.assertEqual(Bonus.objects.filter(question_set=self.qset, packet=None).count(), 0)

    def test_auto_packetize_category_quotas(self):
        self._create_questions()
        report, packets = self._packetize()

        for packet in packets:
            regular_tossups = Tossup.objects.filter(packet=packet, question_number__lte=6)
            regular_bonuses = Bonus.objects.filter(packet=packet, question_number__lte=6)
            tu_by_cat = {}
            bs_by_cat = {}
            for t in regular_tossups:
                tu_by_cat[t.category.category] = tu_by_cat.get(t.category.category, 0) + 1
            for b in regular_bonuses:
                bs_by_cat[b.category.category] = bs_by_cat.get(b.category.category, 0) + 1

            self.assertEqual(tu_by_cat.get('History', 0), 2, msg=str(tu_by_cat))
            self.assertEqual(bs_by_cat.get('History', 0), 2)
            self.assertEqual(tu_by_cat.get('Science', 0), 1)
            self.assertEqual(bs_by_cat.get('Science', 0), 1)
            # Fractional 1.5/1.5: combined total is exactly 3, each type 1 or 2
            for cat in ('Literature', 'Fine Arts'):
                tu = tu_by_cat.get(cat, 0)
                bs = bs_by_cat.get(cat, 0)
                self.assertEqual(tu + bs, 3, msg='{0}: {1}+{2}'.format(cat, tu, bs))
                self.assertIn(tu, (1, 2))
                self.assertIn(bs, (1, 2))

    def test_auto_packetize_subcategory_spread(self):
        self._create_questions()
        report, packets = self._packetize()

        # 4 History-European tossups over 4 packets with 2 History per packet
        # should spread to exactly one per packet
        for packet in packets:
            he = Tossup.objects.filter(packet=packet, question_number__lte=6,
                                       category=self.entries[('History', 'European')])
            self.assertEqual(he.count(), 1)

    def test_auto_packetize_tiebreakers(self):
        self._create_questions()
        report, packets = self._packetize()

        tiebreakers = Tossup.objects.filter(question_set=self.qset, question_number__gt=6)
        self.assertEqual(tiebreakers.count(), 2)
        for tb in tiebreakers:
            self.assertEqual(tb.question_number, 7)
            # Tiebreakers are exempt from caps: this packet now has 3 History tossups
            self.assertEqual(tb.category.category, 'History')
        # Spread across different packets
        self.assertEqual(len(set(tb.packet_id for tb in tiebreakers)), 2)

    def test_auto_packetize_ordering(self):
        self._create_questions()
        report, packets = self._packetize()

        for packet in packets:
            tossups = list(Tossup.objects.filter(packet=packet, question_number__lte=6).order_by('question_number'))
            cats = [t.category.category for t in tossups]
            adjacencies = sum(1 for i in range(len(cats) - 1) if cats[i] == cats[i + 1])
            self.assertEqual(adjacencies, 0, msg='Packet {0}: {1}'.format(packet.packet_name, cats))
            # History has 2 per packet: quarter balance means they can't both
            # be in the first three slots and can't both be in the last three
            history_positions = [i for i, c in enumerate(cats) if c == 'History']
            self.assertFalse(all(p < 3 for p in history_positions), msg=str(cats))
            self.assertFalse(all(p >= 3 for p in history_positions), msg=str(cats))

    def test_auto_packetize_overwrites_existing_assignments(self):
        self._create_questions()
        report, packets = self._packetize()

        # Cram everything into the first packet with bogus numbers, then re-run
        first = packets[0]
        Tossup.objects.filter(question_set=self.qset).update(packet=first, question_number=999)
        Bonus.objects.filter(question_set=self.qset).update(packet=first, question_number=999)

        report, packets = self._packetize(seed=7)
        for packet in packets:
            self.assertEqual(Tossup.objects.filter(packet=packet, question_number__lte=6).count(), 6)
        self.assertEqual(Tossup.objects.filter(question_number=999).count(), 0)

    def test_auto_packetize_lower_level_max(self):
        self._create_questions()
        # Six more Literature-American tossups (9 total) try to cluster;
        # a subcategory max of 1 per packet must hold for regular slots
        for i in range(3):
            self._add_tossup(self.entries[('Literature', 'American')], 'LA extra {0}'.format(i))
        PacketizationEntry.objects.create(
            question_set=self.qset, path='Literature - American', depth=1,
            max_tossups=1)
        report, packets = self._packetize()

        for packet in packets:
            la = Tossup.objects.filter(packet=packet, question_number__lte=6,
                                       category=self.entries[('Literature', 'American')])
            self.assertLessEqual(la.count(), 1)

    def test_ensure_packets_continues_naming(self):
        Packet.objects.create(question_set=self.qset, packet_name='Round 01', created_by=self.writer)
        Packet.objects.create(question_set=self.qset, packet_name='Round 02', created_by=self.writer)
        packets = _ensure_packets(self.qset, 4, self.writer)
        self.assertEqual([p.packet_name for p in packets],
                         ['Round 01', 'Round 02', 'Round 03', 'Round 04'])
