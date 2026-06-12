__author__ = 'mbentley'

import re
import json
import random
import string

from datetime import datetime
from django.utils import timezone

from qems2.qsub.models import *
from qems2.qsub.utils import *
from qems2.qsub.model_utils import *

# TODO: Add tests
def create_acf_packet(qset, packet_name, created_by, regular_distribution, tiebreaker_distribution):
    packet = Packet.objects.create(question_set=qset, packet_name=packet_name, created_by=created_by, packet_type=ACF_PACKET)
    packet.save()
    create_period_recursive(question_set, ACF_REGULAR_PERIOD, regular_distribution, packet)
    create_period_recursive(question_set, ACF_TIEBREAKER_PERIOD, tiebreaker_distribution, packet)
    
# TODO: Add tests
def create_vhsl_packet(qset, tossup_distribution, bonus_distribution, tiebreaker_distribution):
    packet = Packet.objects.create(question_set=qset, packet_name=packet_name, created_by=created_by, packet_type=VHSL_PACKET)
    packet.save()
    create_period_recursive(question_set, VHSL_TOSSUP_PERIOD, tossup_distribution, packet)
    create_period_recursive(question_set, VHSL_BONUS_PERIOD, bonus_distribution, packet)
    create_period_recursive(question_set, VHSL_TIEBREAKER_PERIOD, tiebreaker_distribution, packet)
    
# Creates a new period, figures out if it needs to create a new period entry, populates
# PeriodWideCategoryEntries and OnePeriodCategoryEntries appropriately
# TODO: Add tests
def create_period_recursive(question_set, period_type, distribution, packet):
    pwe, created = PeriodWideEntry.objects.get_or_create(question_set=qset, period_type=period_type, distribution=distribution)
    pwe.period_count += 1
    pwe.save()
    
    if (created):
        create_period_wide_category_entries(pwe)
        
    period = Period.objects.create(name=period_type, packet=packet, period_wide_entry=pwe)        
    create_one_period_category_entries(pwe, period)    

# TODO: Add tests
def create_period_wide_category_entries(period_wide_entry):
    categories = CategoryEntry.objects.filter(distribution=period_wide_entry.distribution)
    for category in categories:
        pwce = PeriodWideCategoryEntry.objects.create(period_wide_entry=period_wide_entry, category_entry=category)
        pwce.save()

# TODO: Add tests
def create_one_period_category_entries(period_wide_entry, period):
    period_wide_category_entries = PeriodWideCategoryEntry.objects.filter(period_wide_entry=period_wide_entry)
    for pwce in period_wide_category_entry:
        opce = OnePeriodCategoryEntry.objects.create(period=period, period_wide_category_entry=pwce)
        opce.save()

# TODO: Add tests
# set the total questions to be an integer, and then store the fractions for random selection
def assign_pwce(pwce_list, packet_count, total_acf_tossup, total_acf_bonus, total_vhsl_bonus):
    cur_acf_tossup_count = 0
    cur_acf_bonus_count = 0
    cur_vhsl_bonus_count = 0
    
    acf_tossup_fractions = []
    acf_bonus_fractions = []
    vhsl_bonus_fractions = []
    
    for pwce in pwce_list:            
        pwce.acf_tossup_total_across_periods = int(pwce.category_entry_for_distribution.acf_tossup_fraction * packet_count)
        cur_acf_tossup_count += pwce.acf_tossup_total_across_periods            
        acf_tossup_fractions = get_fraction_array(pwce, pwce.acf_tossup_total_across_periods)
                    
        pwce.acf_bonus_total_across_periods = int(pwce.category_entry_for_distribution.acf_bonus_fraction * packet_count)
        cur_acf_bonus_count += pwce.acf_bonus_total_across_periods                        
        acf_bonus_fractions = get_fraction_array(pwce, pwce.acf_bonus_total_across_periods)

        pwce.vhsl_bonus_total_across_periods = int(pwce.category_entry_for_distribution.vhsl_bonus_fraction * packet_count)
        cur_vhsl_bonus_count += pwce.vhsl_bonus_total_across_periods            
        vhsl_bonus_fractions = get_fraction_array(pwce, pwce.vhsl_bonus_total_across_periods)
        
        pwce.save()
    
    get_pwce_from_fractions(acf_tossup_fractions, ACF_STYLE_TOSSUP, total_acf_tossup - cur_acf_tossup_count)        
    get_pwce_from_fractions(acf_bonus_fractions, ACF_STYLE_BONUS, total_acf_bonus - cur_acf_bonus_count)
    get_pwce_from_fractions(vhsl_bonus_fractions, VHSL_BONUS, total_vhsl_bonus - cur_vhsl_bonus_count)    
    
def get_fraction_array(pwce, value):
    fractions = []
    fraction = round(round(value - int(value), 4) * 1000, 0)
    print("Fraction before rounding: " + str(fraction))
    fraction = int(fraction)
    print("Fraction after rounding: " + str(fraction))
    for i in range(0, fraction):
        fractions.append(pwce)
    
    return fractions    

# TODO: Add tests        
def get_pwce_from_fractions(fractions, question_type, items_to_process, seed=-1):
    if (seed != -1):
        random.seed(seed)
    else:
        random.seed()
    
    pwce_list = []
    cur_items_processed = 0
    
    # Break fraction ties based on probability
    while (cur_items_processed < items_to_process and len(fractions) > 0):
        index = random.randint(0, len(fractions) - 1)
        pwce = fractions[index]
        
        cur_items_processed += 1
        
        if (question_type == ACF_STYLE_TOSSUP):
            pwce.acf_tossup_total_across_periods += 1
            pwce.save()
        elif (question_type == ACF_STYLE_BONUS):
            pwce.acf_bonus_total_across_periods += 1
            pwce.save()
        elif (question_type == VHSL_BONUS):
            pwce.vhsl_bonus_total_across_periods += 1
            pwce.save()
                    
        # Remove this from the list of eligible categories to choose from
        for i in range(len(fractions) - 1, 0):
            if (fractions[i] == pwce):
                fractions.pop(i)
                
    return pwce_list

# A category entry might be a sub-sub or sub category meaning that it has
# 1 or 2 parent categories.  This method returns the whole set
def get_parents_from_category_entry(category_entry):
    if (category_entry.category_type == CATEGORY):
        return category_entry, None, None
    elif (category_entry.category_type == SUB_CATEGORY):
        category = None
        try:
            category = CategoryEntry.objects.get(category_name=category_entry.category_name, category_type=CATEGORY)
        except Exception as ex:
            print("Could not find parent for: " + str(category_entry))

        return category, category_entry, None
    else:
        category = None
        try:
            category = CategoryEntry.objects.get(category_name=category_entry.category_name, category_type=CATEGORY)
        except Exception as ex:
            print("Could not find parent for: " + str(category_entry))

        sub_category = None
        try:
            sub_category = CategoryEntry.objects.get(category_name=category_entry.category_name, sub_category_name=category_entry.sub_category_name, category_type=SUB_CATEGORY)
        except Exception as ex:
            print("Could not find parent for: " + str(category_entry))
                
        return category, sub_category, category_entry

def get_parents_from_category_entry_for_distribution(cefd):
    category_entry = cefd.category_entry
    ce_cat, ce_subcat, ce_subsubcat = get_parents_from_category_entry(category_entry)
    cat = None
    subcat = None
    subsubcat = None
    
    if (ce_cat is not None):
        cat = CategoryEntryForDistribution.objects.get(distribution=cefd.distribution, category_entry=ce_cat)

    if (ce_subcat is not None):
        subcat = CategoryEntryForDistribution.objects.get(distribution=cefd.distribution, category_entry=ce_subcat)

    if (ce_subsubcat is not None):
        subsubcat = CategoryEntryForDistribution.objects.get(distribution=cefd.distribution, category_entry=ce_subsubcat)
                
    return cat, subcat, subsubcat

def get_parents_from_period_wide_category_entry(pwce):
    cefd = pwce.category_entry_for_distribution
    cefd_cat, cefd_subcat, cefd_subsubcat = get_parents_from_category_entry_for_distribution(cefd)
    cat = None
    subcat = None
    subsubcat = None
    if (cefd_cat is not None):
        cat = PeriodWideCategoryEntry.objects.get(period_wide_entry=pwce.period_wide_entry, category_entry_for_distribution=cefd_cat)

    if (cefd_subcat is not None):
        subcat = PeriodWideCategoryEntry.objects.get(period_wide_entry=pwce.period_wide_entry, category_entry_for_distribution=cefd_subcat)

    if (cefd_subsubcat is not None):
        subsubcat = PeriodWideCategoryEntry.objects.get(period_wide_entry=pwce.period_wide_entry, category_entry_for_distribution=cefd_subsubcat)
        
    return cat, subcat, subsubcat

def get_period_entries_from_category_entry(category_entry, period):   
    dist = period.period_wide_entry.distribution
    
    cefd = CategoryEntryForDistribution.objects.get(distribution=dist, category_entry=category_entry)
    pwce = PeriodWideCategoryEntry.objects.get(period_wide_entry=period.period_wide_entry, category_entry_for_distribution=cefd)
    opce = OnePeriodCategoryEntry.objects.get(period=period, period_wide_category_entry=pwce)
    return pwce, opce

class DistributionRequirement():
    acf_tossups_written = 0
    acf_tossups_needed = 0
    acf_bonuses_written = 0
    acf_bonuses_needed = 0
    vhsl_bonuses_written = 0
    vhsl_bonuses_needed = 0
    category_entry = None
    period_name = ''
    
    def __init__(self, category_entry):
        self.category_entry = category_entry
    
    def __str__(self):
        return str(self.category_entry)
    
    def is_requirement_satisfied(self):
        if (self.acf_tossups_written < self.acf_tossups_needed):
            return False
            
        if (self.acf_bonuses_written < self.acf_bonuses_needed):
            return False
            
        if (self.vhsl_bonuses_writen < self.vhsl_bonuses_needed):
            return False
            
        return True


#########################################################################
# Auto-packetization
#
# Assigns every tossup and bonus in a set to a packet so that each packet
# follows per-category quotas, spreads subcategories across the whole
# tournament, and orders questions within each packet semi-randomly with
# the major categories balanced by quarter.  Questions beyond the regular
# per-packet counts become tiebreakers, which are exempt from the quota
# caps.
#########################################################################

import math

def get_path_parts(dist_entry):
    """Category path tuple for a DistributionEntry, e.g.
    ('History', 'American', '1865-1945')."""
    if dist_entry is None:
        return ()
    parts = [dist_entry.category]
    if dist_entry.subcategory:
        for sub in dist_entry.subcategory.split(' - '):
            sub = sub.strip()
            if sub:
                parts.append(sub)
    return tuple(parts)

def _quota_cap(value):
    """Per-packet cap implied by a possibly-fractional quota: 1.5 allows 2."""
    if value is None:
        return None
    return int(math.ceil(float(value) - 1e-9))

def _combined_cap(quota):
    """Cap on tossups+bonuses combined for a path whose quota is fractional.
    1.5/1.5 caps the combined count at 3 even though each type allows 2."""
    max_tu = quota.get('max_tu')
    max_bs = quota.get('max_bs')
    if max_tu is None or max_bs is None:
        return None
    total = float(max_tu) + float(max_bs)
    return int(math.ceil(total - 1e-9))

class PacketTarget:
    """Mutable per-packet assignment state."""

    def __init__(self, packet, index):
        self.packet = packet
        self.index = index
        self.tu_counts = {}   # path prefix tuple -> count of regular tossups
        self.bs_counts = {}
        self.tossups = []     # (question, path_parts)
        self.bonuses = []
        self.tb_tossups = []  # tiebreakers
        self.tb_bonuses = []

    def counts(self, qtype):
        return self.tu_counts if qtype == 'tu' else self.bs_counts

    def regular(self, qtype):
        return self.tossups if qtype == 'tu' else self.bonuses

    def combined_count(self, prefix):
        return self.tu_counts.get(prefix, 0) + self.bs_counts.get(prefix, 0)

    def can_take(self, parts, qtype, per_packet_limit, quotas):
        if len(self.regular(qtype)) >= per_packet_limit:
            return False
        counts = self.counts(qtype)
        max_key = 'max_tu' if qtype == 'tu' else 'max_bs'
        for i in range(1, len(parts) + 1):
            prefix = parts[:i]
            quota = quotas.get(prefix)
            if not quota:
                continue
            cap = _quota_cap(quota.get(max_key))
            if cap is not None and counts.get(prefix, 0) >= cap:
                return False
            combined = _combined_cap(quota)
            if combined is not None and self.combined_count(prefix) >= combined:
                return False
        return True

    def take(self, question, parts, qtype):
        self.regular(qtype).append((question, parts))
        counts = self.counts(qtype)
        for i in range(1, len(parts) + 1):
            prefix = parts[:i]
            counts[prefix] = counts.get(prefix, 0) + 1

def _interleave_groups(groups, rnd):
    """Round-robin merge of question groups so that consecutive questions
    come from different subcategories as much as possible."""
    for group in groups:
        rnd.shuffle(group)
    groups = sorted(groups, key=len, reverse=True)
    merged = []
    i = 0
    while True:
        added = False
        for group in groups:
            if i < len(group):
                merged.append(group[i])
                added = True
        if not added:
            break
        i += 1
    return merged

def _compute_budgets(by_top, qtype, quotas, num_targets, capacity):
    """Regular-phase deal budget per top category: its per-packet minimum
    times the packet count (or everything available without a quota).  When
    the budgets exceed total capacity -- the quotas over-subscribe the packet
    size -- scale them proportionally so small categories dealt late aren't
    squeezed out entirely."""
    min_key = 'min_tu' if qtype == 'tu' else 'min_bs'
    budgets = {}
    avail = {}
    for top, leaf_groups in by_top.items():
        avail[top] = sum(len(v) for v in leaf_groups.values())
        quota = quotas.get(top)
        minimum = quota.get(min_key) if quota else None
        if minimum is not None:
            budgets[top] = min(avail[top], int(round(float(minimum) * num_targets)))
        else:
            budgets[top] = avail[top]

    total = sum(budgets.values())
    if total > capacity:
        scaled = {top: budgets[top] * capacity / float(total) for top in budgets}
        floors = {top: int(scaled[top]) for top in budgets}
        remainder = capacity - sum(floors.values())
        for top in sorted(budgets, key=lambda t: scaled[t] - floors[t], reverse=True)[:remainder]:
            floors[top] += 1
        budgets = {top: min(floors[top], avail[top]) for top in budgets}
    return budgets

def _deal_questions(questions, qtype, targets, per_packet_limit, quotas, rnd):
    """Deal questions of one type into packets.  Returns leftovers that no
    packet could legally take."""
    # Group by top category, then by leaf path within the category
    by_top = {}
    for question, parts in questions:
        top = parts[:1] if parts else ()
        by_top.setdefault(top, {}).setdefault(parts, []).append((question, parts))

    budgets = _compute_budgets(by_top, qtype, quotas, len(targets),
                               per_packet_limit * len(targets))

    leftovers = []
    # Deal biggest categories first so their per-packet spread is the least
    # constrained by earlier choices
    for top in sorted(by_top, key=lambda t: -sum(len(v) for v in by_top[t].values())):
        leaf_groups = [group for _, group in sorted(by_top[top].items())]
        ordered = _interleave_groups(leaf_groups, rnd)

        # Questions beyond the budget become tiebreakers.  Without this,
        # over-supplied categories dealt early fill the packets and crowd out
        # the categories dealt later.
        budget = budgets[top]
        if len(ordered) > budget:
            leftovers.extend(ordered[budget:])
            ordered = ordered[:budget]

        for question, parts in ordered:
            candidates = [t for t in targets if t.can_take(parts, qtype, per_packet_limit, quotas)]
            if not candidates:
                leftovers.append((question, parts))
                continue
            # Prefer packets with the fewest of this top category (counting
            # both question types so fractional quotas alternate 2+1/1+2),
            # then the emptiest packet -- keeping fill balanced so packets
            # never run out of room for categories dealt later -- and then
            # the fewest of this leaf to spread subcategories
            candidates.sort(key=lambda t: (
                t.counts(qtype).get(parts[:1], 0) if parts else 0,
                t.combined_count(parts[:1]) if parts else 0,
                len(t.regular(qtype)),
                t.counts(qtype).get(parts, 0),
                rnd.random()))
            candidates[0].take(question, parts, qtype)
    return leftovers

def _deal_tiebreakers(leftovers, qtype, targets, rnd):
    """Tiebreakers are exempt from quota caps; spread them evenly."""
    rnd.shuffle(leftovers)
    for question, parts in leftovers:
        order = sorted(targets,
                       key=lambda t: (len(t.tb_tossups if qtype == 'tu' else t.tb_bonuses),
                                      len(t.regular(qtype)), rnd.random()))
        target = order[0]
        (target.tb_tossups if qtype == 'tu' else target.tb_bonuses).append((question, parts))

def _top_of(item):
    parts = item[1]
    return parts[0] if parts else ''

def _order_within_packet(items, rnd):
    """Semi-random order with major categories balanced by quarter and no
    two consecutive questions from the same top category."""
    if len(items) <= 1:
        return list(items)

    quarters = [[] for _ in range(4)]
    by_top = {}
    for item in items:
        by_top.setdefault(_top_of(item), []).append(item)

    # Deal each category's questions round-robin into quarters, starting at a
    # random quarter, so e.g. 4 literature questions land one per quarter
    for top in sorted(by_top, key=lambda t: -len(by_top[t])):
        group = by_top[top]
        rnd.shuffle(group)
        start = rnd.randrange(4)
        for i, item in enumerate(group):
            quarters[(start + i) % 4].append(item)

    for quarter in quarters:
        rnd.shuffle(quarter)
    ordered = [item for quarter in quarters for item in quarter]

    # Fix-up passes: break same-category adjacencies by swapping with a
    # position where neither side ends up adjacent to a matching category
    for _ in range(3):
        clean = True
        for i in range(len(ordered) - 1):
            if _top_of(ordered[i]) != _top_of(ordered[i + 1]):
                continue
            clean = False
            for j in range(len(ordered)):
                if abs(j - i) <= 1:
                    continue
                a, b = ordered[i + 1], ordered[j]
                if _top_of(b) == _top_of(ordered[i]):
                    continue
                neighbors_j = [k for k in (j - 1, j + 1) if 0 <= k < len(ordered) and k != i + 1]
                if any(_top_of(ordered[k]) == _top_of(a) for k in neighbors_j):
                    continue
                if i + 2 < len(ordered) and i + 2 != j and _top_of(b) == _top_of(ordered[i + 2]):
                    continue
                ordered[i + 1], ordered[j] = b, a
                break
        if clean:
            break
    return ordered

def _offset_bonuses(tossups, bonuses, rnd):
    """Avoid a bonus sharing its position's top category with the tossup at
    the same question number (same category tossup leading into same
    category bonus)."""
    for i in range(min(len(tossups), len(bonuses))):
        if _top_of(bonuses[i]) != _top_of(tossups[i]):
            continue
        for j in range(len(bonuses)):
            if j == i:
                continue
            if j < len(tossups) and _top_of(bonuses[i]) == _top_of(tossups[j]):
                continue
            if _top_of(bonuses[j]) == _top_of(tossups[i]):
                continue

            # Keep bonus adjacency intact at both positions
            def adjacency_ok(pos, item):
                for k in (pos - 1, pos + 1):
                    if 0 <= k < len(bonuses) and k != i and k != j and _top_of(bonuses[k]) == _top_of(item):
                        return False
                return True

            if adjacency_ok(i, bonuses[j]) and adjacency_ok(j, bonuses[i]):
                bonuses[i], bonuses[j] = bonuses[j], bonuses[i]
                break
    return bonuses

def build_quota_dict(qset):
    """Read the saved PacketizationEntry rows for a set into the quota dict
    used by auto_packetize."""
    quotas = {}
    for entry in PacketizationEntry.objects.filter(question_set=qset):
        path = tuple(p.strip() for p in entry.path.split(' - ') if p.strip())
        quotas[path] = {
            'min_tu': entry.min_tossups,
            'max_tu': entry.max_tossups,
            'min_bs': entry.min_bonuses,
            'max_bs': entry.max_bonuses,
        }
    return quotas

def _ensure_packets(qset, num_packets, created_by):
    """Use the set's existing packets (sorted by name) and create more if
    needed to reach num_packets."""
    packets = list(Packet.objects.filter(question_set=qset).order_by('packet_name'))
    if len(packets) >= num_packets:
        return packets[:num_packets]

    # Try to continue the existing naming scheme ("Round 01" -> "Round 11")
    base = 'Packet'
    if packets:
        m = re.match(r'^(.*?)\s*\d+$', packets[-1].packet_name)
        if m and m.group(1).strip():
            base = m.group(1).strip()
    existing_names = set(p.packet_name for p in packets)
    next_num = len(packets) + 1
    while len(packets) < num_packets:
        name = '{0} {1:02d}'.format(base, next_num)
        next_num += 1
        if name in existing_names:
            continue
        packet = Packet.objects.create(question_set=qset, packet_name=name, created_by=created_by)
        packets.append(packet)
    packets.sort(key=lambda p: p.packet_name)
    return packets[:num_packets]

def auto_packetize(qset, num_packets, tossups_per_packet, bonuses_per_packet,
                   quotas, created_by, seed=None):
    """Assign every tossup and bonus in the set to a packet.

    Overwrites all existing packet assignments.  quotas maps category path
    tuples to per-packet {'min_tu','max_tu','min_bs','max_bs'} values
    (Decimals or None).  Returns a report dict.
    """
    rnd = random.Random(seed)
    packets = _ensure_packets(qset, num_packets, created_by)
    targets = [PacketTarget(p, i) for i, p in enumerate(packets)]

    tossups = [(t, get_path_parts(t.category)) for t in
               Tossup.objects.filter(question_set=qset).select_related('category')]
    bonuses = [(b, get_path_parts(b.category)) for b in
               Bonus.objects.filter(question_set=qset).select_related('category')]

    tu_left = _deal_questions(tossups, 'tu', targets, tossups_per_packet, quotas, rnd)
    bs_left = _deal_questions(bonuses, 'bs', targets, bonuses_per_packet, quotas, rnd)

    # Second chance for leftovers blocked only by category caps: packets with
    # free regular slots take them before they become tiebreakers
    still_tu = []
    for question, parts in tu_left:
        open_targets = [t for t in targets if len(t.tossups) < tossups_per_packet]
        if open_targets:
            open_targets.sort(key=lambda t: (len(t.tossups), t.combined_count(parts[:1]) if parts else 0, rnd.random()))
            open_targets[0].take(question, parts, 'tu')
        else:
            still_tu.append((question, parts))
    still_bs = []
    for question, parts in bs_left:
        open_targets = [t for t in targets if len(t.bonuses) < bonuses_per_packet]
        if open_targets:
            open_targets.sort(key=lambda t: (len(t.bonuses), t.combined_count(parts[:1]) if parts else 0, rnd.random()))
            open_targets[0].take(question, parts, 'bs')
        else:
            still_bs.append((question, parts))

    _deal_tiebreakers(still_tu, 'tu', targets, rnd)
    _deal_tiebreakers(still_bs, 'bs', targets, rnd)

    # Order each packet and persist.  bulk_update skips per-question save
    # signals (search indexes don't cover packet/number), which matters when
    # rewriting several hundred questions.
    report = {'packets': [], 'warnings': []}
    changed_tossups = []
    changed_bonuses = []
    for target in targets:
        ordered_tu = _order_within_packet(target.tossups, rnd)
        ordered_bs = _order_within_packet(target.bonuses, rnd)
        ordered_bs = _offset_bonuses(ordered_tu, ordered_bs, rnd)

        for number, (question, parts) in enumerate(ordered_tu + target.tb_tossups, start=1):
            question.packet = target.packet
            question.question_number = number
            changed_tossups.append(question)
        for number, (question, parts) in enumerate(ordered_bs + target.tb_bonuses, start=1):
            question.packet = target.packet
            question.question_number = number
            changed_bonuses.append(question)

        top_counts = {}
        for _, parts in target.tossups:
            top = parts[0] if parts else '(none)'
            top_counts.setdefault(top, [0, 0])[0] += 1
        for _, parts in target.bonuses:
            top = parts[0] if parts else '(none)'
            top_counts.setdefault(top, [0, 0])[1] += 1
        report['packets'].append({
            'packet': target.packet,
            'tossups': len(target.tossups),
            'bonuses': len(target.bonuses),
            'tiebreaker_tossups': len(target.tb_tossups),
            'tiebreaker_bonuses': len(target.tb_bonuses),
            'category_counts': {top: '{0}/{1}'.format(c[0], c[1]) for top, c in sorted(top_counts.items())},
        })

    Tossup.objects.bulk_update(changed_tossups, ['packet', 'question_number'], batch_size=200)
    Bonus.objects.bulk_update(changed_bonuses, ['packet', 'question_number'], batch_size=200)

    # Report quota shortfalls at the top level
    for path, quota in sorted(quotas.items()):
        if len(path) != 1:
            continue
        for qtype, min_key in (('tu', 'min_tu'), ('bs', 'min_bs')):
            minimum = quota.get(min_key)
            if minimum is None:
                continue
            label = 'tossups' if qtype == 'tu' else 'bonuses'

            # Set-wide: did the category get its intended share of regular slots?
            intended = int(round(float(minimum) * len(targets)))
            placed = sum(t.counts(qtype).get(path, 0) for t in targets)
            if placed < intended:
                report['warnings'].append(
                    '{0}: only {1} of the intended {2} regular {3} were placed '
                    '(the quotas may add up to more than the packet size, or too few are written)'.format(
                        ' - '.join(path), placed, intended, label))

            floor_min = int(float(minimum))
            short = [t.packet.packet_name for t in targets
                     if t.counts(qtype).get(path, 0) < floor_min]
            if short:
                report['warnings'].append(
                    '{0}: {1} packet(s) below the minimum of {2} {3} ({4})'.format(
                        ' - '.join(path), len(short), minimum, label,
                        ', '.join(short[:5]) + ('...' if len(short) > 5 else '')))

    return report
