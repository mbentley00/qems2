"""Populate the local database with a few example tournaments for demoing the
app (question sets, packets, tossups/bonuses, plus some playtest buzzes and
comments). Idempotent: re-running first removes the previously seeded examples.

    python manage.py seed_example_sets

Creates a login account you can use:  username "demo", password "demo".
Does NOT touch any real (non-example) data.
"""

import random
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from django.contrib.sites.models import Site
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone
from django_comments.models import Comment

from qems2.qsub.models import (
    QuestionSet, Distribution, DistributionEntry, SetWideDistributionEntry,
    TieBreakDistributionEntry, Packet, Tossup, Bonus, QuestionType, Writer,
    TossupBuzz, BonusResult, PlaytestSession, PLAYTEST_SOURCE_WEB,
    TossupHistory, BonusHistory)
from qems2.qsub.utils import ACF_STYLE_TOSSUP, ACF_STYLE_BONUS, QUESTION_CREATE

# Marker stamped on example sets' host field so re-seeding only removes our own.
EXAMPLE_HOST = 'Example Tournament (seeded)'

# (category, subcategory, tossup_text, tossup_answer). Each tossup carries a
# "(*)" power mark so the clue-by-clue power feature is demonstrable.
TOSSUPS = [
    ('History', 'European',
     "This ruler's reforms included the Table of Ranks, and he founded a new "
     "capital on the Baltic. He crushed Charles XII at (*) Poltava during the "
     "Great Northern War. For 10 points, name this tsar who modernized Russia.",
     "Peter the _Great_ [or _Peter I_]"),
    ('History', 'American',
     "During this conflict, ironclads dueled at Hampton Roads and Pickett led a "
     "doomed (*) charge at Gettysburg. For 10 points, name this 1861-1865 war "
     "between the Union and the Confederacy.",
     "_American Civil War_ [or _War Between the States_]"),
    ('Literature', 'British',
     "This author created the scheming clerk Uriah Heep and the debt-ridden "
     "Micawber. His novels include Great Expectations and (*) Oliver Twist. For "
     "10 points, name this Victorian author of A Tale of Two Cities.",
     "Charles _Dickens_"),
    ('Literature', 'World',
     "In this novel the Buendia family endures seven generations in the town of "
     "Macondo. Its author was a Colombian (*) Nobel laureate. For 10 points, "
     "name this magical-realist work by Gabriel Garcia Marquez.",
     "_One Hundred Years of Solitude_ [or _Cien anos de soledad_]"),
    ('Science', 'Physics',
     "This quantity is conserved when no external torque acts and equals moment "
     "of inertia times angular velocity. It is the rotational analog of (*) "
     "linear momentum. For 10 points, name this vector quantity denoted L.",
     "_angular momentum_"),
    ('Science', 'Biology',
     "This organelle contains folded cristae and its own circular DNA, and hosts "
     "the citric acid cycle. It is nicknamed the (*) powerhouse of the cell. For "
     "10 points, name this organelle that generates ATP.",
     "_mitochondri_on [or _mitochondri_a]"),
    ('Fine Arts', 'Painting',
     "This artist depicted limp, melting clocks in The Persistence of Memory and "
     "co-wrote a film with Luis Bunuel. He sported a famous waxed (*) mustache. "
     "For 10 points, name this Spanish Surrealist.",
     "Salvador _Dali_"),
    ('Fine Arts', 'Music',
     "This composer went deaf yet finished his Ninth Symphony, whose choral "
     "finale sets Schiller's Ode to (*) Joy. For 10 points, name this German "
     "composer of the Moonlight Sonata and Fifth Symphony.",
     "Ludwig van _Beethoven_"),
    ('Geography', 'World',
     "This river flows north through Sudan and Egypt before splitting into a "
     "Mediterranean (*) delta. Its Aswan High Dam created Lake Nasser. For 10 "
     "points, name this longest river in the world.",
     "_Nile_ River"),
    ('Religion', 'Christianity',
     "In this event from Acts, tongues of fire rested on the apostles, who began "
     "speaking in foreign (*) languages. For 10 points, name this Christian feast "
     "occurring fifty days after Easter.",
     "_Pentecost_ [accept _Shavuot_ before mention]"),
    ('Mythology', 'Greek',
     "To atone for killing his family, this hero performed twelve labors, "
     "including slaying the Nemean lion and the (*) Hydra. For 10 points, name "
     "this strongest Greek hero, called Hercules by the Romans.",
     "_Heracles_ [or _Herakles_; or _Hercules_]"),
    ('Philosophy', 'European',
     "This thinker's categorical imperative commands acting only on maxims one "
     "could will to become (*) universal laws. For 10 points, name this German "
     "author of the Critique of Pure Reason.",
     "Immanuel _Kant_"),
]

# (category, subcategory, leadin, [(part_text, part_answer), ...])
BONUSES = [
    ('History', 'European',
     "For 10 points each, answer the following about the French Revolution:",
     [("This Parisian fortress was stormed on July 14, 1789.", "_Bastille_"),
      ("This lawyer led the Committee of Public Safety during the Terror before "
       "his own 1794 execution.", "Maximilien _Robespierre_"),
      ("This general seized power in 1799 and later crowned himself emperor.",
       "_Napoleon_ Bonaparte [or _Napoleon I_]")]),
    ('History', 'American',
     "For 10 points each, answer the following about the American founding:",
     [("This 1776 document was primarily drafted by Thomas Jefferson.",
       "_Declaration of Independence_"),
      ("These first ten amendments protect individual liberties.",
       "_Bill of Rights_"),
      ("This Virginian presided over the Constitutional Convention and became "
       "the first president.", "George _Washington_")]),
    ('Literature', 'British',
     "For 10 points each, answer the following about Shakespeare's tragedies:",
     [("This Danish prince delivers the 'To be or not to be' soliloquy.",
       "_Hamlet_"),
      ("This Moorish general is deceived by Iago into killing Desdemona.",
       "_Othello_"),
      ("This Scottish thane murders King Duncan after a prophecy by three "
       "witches.", "_Macbeth_")]),
    ('Literature', 'World',
     "For 10 points each, answer the following about Russian literature:",
     [("This author wrote War and Peace and Anna Karenina.", "Leo _Tolstoy_"),
      ("This author of Crime and Punishment also wrote The Brothers Karamazov.",
       "Fyodor _Dostoevsky_ [or _Dostoyevsky_]"),
      ("In Crime and Punishment, this impoverished student murders a pawnbroker.",
       "_Raskolnikov_ [accept _Rodion_ Romanovich Raskolnikov]")]),
    ('Science', 'Physics',
     "For 10 points each, answer the following about thermodynamics:",
     [("This law states that the entropy of an isolated system never decreases.",
       "_second_ law of thermodynamics"),
      ("This quantity, often denoted S, measures disorder.", "_entropy_"),
      ("This temperature scale sets zero at absolute zero.", "_Kelvin_ scale")]),
    ('Science', 'Biology',
     "For 10 points each, answer the following about genetics:",
     [("This molecule's double-helix structure was described by Watson and "
       "Crick.", "_DNA_ [or _deoxyribonucleic acid_]"),
      ("This monk's pea-plant experiments founded the study of heredity.",
       "Gregor _Mendel_"),
      ("This process copies DNA into messenger RNA.", "_transcription_")]),
    ('Fine Arts', 'Painting',
     "For 10 points each, answer the following about Renaissance art:",
     [("This Florentine painted the Mona Lisa and The Last Supper.",
       "_Leonardo_ da Vinci"),
      ("This artist frescoed the Sistine Chapel ceiling and sculpted David.",
       "_Michelangelo_ Buonarroti"),
      ("This painter of The School of Athens died young in 1520.",
       "_Raphael_ [or Raffaello _Sanzio_]")]),
    ('Fine Arts', 'Music',
     "For 10 points each, answer the following about opera:",
     [("This Italian composed Aida, La traviata, and Rigoletto.",
       "Giuseppe _Verdi_"),
      ("This Austrian prodigy wrote The Magic Flute and Don Giovanni.",
       "Wolfgang Amadeus _Mozart_"),
      ("This voice type is the highest female vocal range.", "_soprano_")]),
    ('Geography', 'World',
     "For 10 points each, name these world capitals:",
     [("This capital of Japan was formerly called Edo.", "_Tokyo_"),
      ("This Australian capital was purpose-built between Sydney and Melbourne.",
       "_Canberra_"),
      ("This Andean capital of Peru was founded by Francisco Pizarro.",
       "_Lima_")]),
    ('Religion', 'Christianity',
     "For 10 points each, answer the following about world religions:",
     [("This religion's followers observe the Five Pillars, including the hajj.",
       "_Islam_ [accept _Muslim_]"),
      ("This Indian religion teaches the Four Noble Truths.", "_Buddhism_"),
      ("This oldest Abrahamic faith observes the Sabbath and Yom Kippur.",
       "_Judaism_ [accept _Jewish_]")]),
    ('Mythology', 'Greek',
     "For 10 points each, answer the following about Greek gods:",
     [("This king of the gods wields a thunderbolt.", "_Zeus_"),
      ("This goddess of wisdom sprang from Zeus's head and protects Athens.",
       "_Athena_"),
      ("This messenger god wears winged sandals.", "_Hermes_")]),
    ('Philosophy', 'European',
     "For 10 points each, answer the following about ancient philosophy:",
     [("This student of Socrates founded the Academy and wrote The Republic.",
       "_Plato_"),
      ("This student of Plato tutored Alexander the Great.", "_Aristotle_"),
      ("This Athenian, condemned to drink hemlock, taught via a questioning "
       "method.", "_Socrates_")]),
]

EXAMPLE_TOURNAMENTS = [
    'Demo Open 2026',
    'Example Fall Novice',
    'Sample Collegiate Championship',
]

# (username, first, last) for the example writers/players.
EXAMPLE_WRITERS = [
    ('ada', 'Ada', 'Lovelace'),
    ('grace', 'Grace', 'Hopper'),
    ('linus', 'Linus', 'Pauling'),
    ('marie', 'Marie', 'Curie'),
]


class Command(BaseCommand):
    help = 'Populate the local database with example tournaments and a demo login.'

    def handle(self, *args, **options):
        random.seed(2026)
        self._ensure_question_types()
        demo = self._ensure_account('demo', 'Demo', 'User', password='demo',
                                    superuser=True)
        writers = [self._ensure_account(u, f, l, password=u)
                   for (u, f, l) in EXAMPLE_WRITERS]
        all_authors = [demo] + writers

        # Remove any previously seeded examples so this command is idempotent.
        removed = QuestionSet.objects.filter(host=EXAMPLE_HOST).count()
        QuestionSet.objects.filter(host=EXAMPLE_HOST).delete()
        if removed:
            self.stdout.write('Removed {0} previously seeded example set(s).'.format(removed))

        for i, name in enumerate(EXAMPLE_TOURNAMENTS):
            self._build_tournament(name, demo, writers, all_authors, seed_offset=i)

        self.stdout.write(self.style.SUCCESS(
            'Done. Created {0} example tournaments.'.format(len(EXAMPLE_TOURNAMENTS))))
        self.stdout.write(self.style.SUCCESS(
            'Log in with username "demo" / password "demo".'))

    # -- setup helpers --------------------------------------------------------

    def _ensure_question_types(self):
        for name in (ACF_STYLE_TOSSUP, ACF_STYLE_BONUS):
            QuestionType.objects.get_or_create(question_type=name)
        self.tu_type = QuestionType.objects.filter(question_type=ACF_STYLE_TOSSUP).first()
        self.bs_type = QuestionType.objects.filter(question_type=ACF_STYLE_BONUS).first()

    def _ensure_account(self, username, first, last, password, superuser=False):
        user, created = User.objects.get_or_create(
            username=username, defaults={'first_name': first, 'last_name': last})
        user.first_name = first
        user.last_name = last
        user.is_staff = superuser
        user.is_superuser = superuser
        user.set_password(password)
        user.save()
        # Writer is auto-created by a post_save signal on User.
        return Writer.objects.get(user=user)

    # -- tournament builder ---------------------------------------------------

    def _build_tournament(self, name, owner, writers, authors, seed_offset):
        distribution = Distribution.objects.create(name='{0} Distribution'.format(name))

        # One DistributionEntry per (category, subcategory) seen in the content.
        cat_pairs = []
        seen = set()
        for cat, sub, _t, _a in TOSSUPS:
            if (cat, sub) not in seen:
                seen.add((cat, sub)); cat_pairs.append((cat, sub))
        entries = {}
        for cat, sub in cat_pairs:
            entries[(cat, sub)] = DistributionEntry.objects.create(
                distribution=distribution, category=cat, subcategory=sub,
                min_tossups=1, max_tossups=1, min_bonuses=1, max_bonuses=1)

        qset = QuestionSet.objects.create(
            name=name, date=timezone.now().date(), host=EXAMPLE_HOST,
            address='Online', owner=owner, distribution=distribution, num_packets=2,
            tossups_per_packet=6, bonuses_per_packet=6)

        owner.question_set_editor.add(qset)
        for w in writers:
            w.question_set_writer.add(qset)
        # Make the first example writer an editor too, for variety.
        if writers:
            writers[0].question_set_editor.add(qset)
            qset.editor.add(writers[0])
        qset.editor.add(owner)

        for entry in DistributionEntry.objects.filter(distribution=distribution):
            SetWideDistributionEntry.objects.create(
                question_set=qset, dist_entry=entry,
                num_tossups=2 * (entry.max_tossups or 0),
                num_bonuses=2 * (entry.max_bonuses or 0))
            TieBreakDistributionEntry.objects.create(
                question_set=qset, dist_entry=entry, num_tossups=1, num_bonuses=1)

        # Split the question bank across two packets, rotated per tournament so
        # the three examples don't read identically.
        order = list(range(len(TOSSUPS)))
        rng = random.Random(100 + seed_offset)
        rng.shuffle(order)
        half = len(order) // 2
        packet_indices = [order[:half], order[half:]]

        created_tossups, created_bonuses = [], []
        for p, idxs in enumerate(packet_indices, start=1):
            packet = Packet.objects.create(
                packet_name='Packet {0}'.format(p), question_set=qset, created_by=owner)
            for n, idx in enumerate(idxs, start=1):
                cat, sub, text, answer = TOSSUPS[idx]
                created_tossups.append(self._make_tossup(
                    qset, packet, n, entries[(cat, sub)], text, answer,
                    rng.choice(authors)))

                bcat, bsub, leadin, parts = BONUSES[idx]
                created_bonuses.append(self._make_bonus(
                    qset, packet, n, entries[(bcat, bsub)], leadin, parts,
                    rng.choice(authors)))

        self._seed_playtest(qset, created_tossups, created_bonuses, authors, rng)
        self._seed_comments(created_tossups, created_bonuses, authors, rng)
        self.stdout.write('Created "{0}": {1} tossups, {2} bonuses.'.format(
            name, len(created_tossups), len(created_bonuses)))

    def _backdate(self, question, history_model, rng):
        """Spread created/last-changed dates over the past two weeks so the
        Recent Changes and recent-question play modes have something to show."""
        when = timezone.now() - timedelta(days=rng.randint(0, 13),
                                          hours=rng.randint(0, 23))
        question.created_date = when
        question.last_changed_date = when
        question.save()
        # Keep the creation history row in step so Recent Changes doesn't count
        # it as an edit.
        if question.question_history_id:
            history_model.objects.filter(
                question_history_id=question.question_history_id).update(change_date=when)

    def _make_tossup(self, qset, packet, number, entry, text, answer, author):
        t = Tossup(question_set=qset, packet=packet, question_number=number,
                   tossup_text=text, tossup_answer=answer, category=entry,
                   author=author, question_type=self.tu_type)
        t.save_question(QUESTION_CREATE, author)
        self._backdate(t, TossupHistory, random.Random(t.id))
        return t

    def _make_bonus(self, qset, packet, number, entry, leadin, parts, author):
        (p1, a1), (p2, a2), (p3, a3) = parts
        b = Bonus(question_set=qset, packet=packet, question_number=number,
                  leadin=leadin, part1_text=p1, part1_answer=a1,
                  part2_text=p2, part2_answer=a2, part3_text=p3, part3_answer=a3,
                  part1_difficulty='e', part2_difficulty='m', part3_difficulty='h',
                  category=entry, author=author, question_type=self.bs_type)
        b.save_question(QUESTION_CREATE, author)
        self._backdate(b, BonusHistory, random.Random(b.id))
        return b

    # -- playtest + comment seeding ------------------------------------------

    def _seed_playtest(self, qset, tossups, bonuses, authors, rng):
        """Record some buzzes/bonus results so the Playtest Results panels and
        scoreboards aren't empty."""
        sessions = {}

        def session_for(player):
            if player.id not in sessions:
                sessions[player.id] = PlaytestSession.objects.create(
                    question_set=qset, player=player, source=PLAYTEST_SOURCE_WEB)
            return sessions[player.id]

        for t in tossups:
            words = max(8, len(t.tossup_text.split()))
            for _ in range(rng.randint(0, 3)):
                player = rng.choice(authors)
                idx = rng.randint(2, words - 1)
                correct = rng.random() < 0.65
                powered = correct and idx <= words // 3
                value = (15 if powered else 10) if correct else (-5 if rng.random() < 0.5 else 0)
                TossupBuzz.objects.create(
                    tossup=t, session=session_for(player), player=player,
                    buzz_word_index=idx, total_words=words,
                    char_position=len(' '.join(t.tossup_text.split()[:idx])),
                    correct=correct, powered=powered, value=value,
                    answer_given='', source=PLAYTEST_SOURCE_WEB)

        for b in bonuses:
            for _ in range(rng.randint(0, 3)):
                player = rng.choice(authors)
                p1 = rng.random() < 0.8
                p2 = rng.random() < 0.55
                p3 = rng.random() < 0.3
                BonusResult.objects.create(
                    bonus=b, session=session_for(player), player=player,
                    part1_correct=p1, part2_correct=p2, part3_correct=p3,
                    total=10 * sum((p1, p2, p3)), source=PLAYTEST_SOURCE_WEB)

    def _seed_comments(self, tossups, bonuses, authors, rng):
        site = Site.objects.get_current()
        snippets = [
            'Nice clue placement here.',
            'Is this giveaway too easy?',
            'Might want to add a pronunciation guide.',
            'Great question, leaving as-is.',
            'Double-check the difficulty on part 3.',
            'Consider reordering the middle clues.',
        ]
        pool = tossups + bonuses
        for q in rng.sample(pool, min(len(pool), 8)):
            author = rng.choice(authors)
            Comment.objects.create(
                content_type=ContentType.objects.get_for_model(q),
                object_pk=str(q.id), site=site, user=author.user,
                comment=rng.choice(snippets), is_public=True, is_removed=False)
