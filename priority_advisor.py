"""
priority_advisor.py — Production Priority Advisor
==================================================
Scores every section in today's plan and recommends which to
prioritise first on CRM04 and CRM06, based on:

  1. DOWNSTREAM STARVATION — which consumer is most at risk?
  2. CUSTOMER URGENCY      — which SOs are overdue or near TDC expiry?
  3. AGE PENALTY           — oldest coils push up priority
  4. ANNEALING PIPELINE    — what needs to be sent to anneal TODAY
                             to return in 72h for next plan cycle?
  5. PLANNING MODE         — shift-in-charge sets the mode:
       BALANCED      → all factors weighted equally
       TUBE_URGENT   → Tube Plant demand maximised
       HT_URGENT     → H&T Line demand maximised
       MAX_PROD      → highest MT/hour sections first
       CLEAR_BACKLOG → oldest coils cleared first
       FEED_ANNEAL   → prioritise First/Re-Rolling to feed annealing pipeline

Outputs:
  - Priority score (0-100) for each section
  - Recommended sequence for CRM04 and CRM06
  - Shift briefing text (ready to share on WhatsApp)
  - Warnings for downstream starvation risk
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Dict, Optional
import pandas as pd
import numpy as np


# ── Downstream consumer for each section ────────────────────────────────────
# Corrected flow (confirmed by planner):
#
#   VIA CRS FIRST (Rolling → CRS → final destination):
#     TUBE_FH           → CRS → Tube Plant (Sahibabad Tube Plant, C09 product)
#     CRCA_FINISH       → CRS → OEM/Customer dispatch (C09 CRCA)
#     CRCA_FINISH_CRM06 → CRS → LG Bala dispatch (C01 TSBM41)
#
#   DIRECT FROM ROLLING (no CRS step needed):
#     HT_FINISH               → H&T Line (B28/B29, direct after rolling)
#     SKIN_PASS_SUPER_BRIGHT  → Skin Pass (C01 TATXXD/T012, direct)
#     SKIN_PASS_CHROME        → Skin Pass (C01 TATD12/D012, direct)
#     SKIN_PASS_HEAVY_MATT    → Skin Pass (C01 TATBID/BD01, direct)
#
#   VIA ANNEALING (72h cycle):
#     RE_ROLLING    → Annealing → then CRS or H&T downstream
#     FIRST_ROLLING → Annealing → then CRS/LG Bala downstream
#     ROLLING       → Rewinding → Annealing → Skin Pass downstream

SECTION_DOWNSTREAM = {
    'TUBE_FH':                'CRS → Tube Plant',
    'HT_FINISH':              'H&T Line (direct)',
    'CRCA_FINISH':            'CRS → OEM/Dispatch',
    'CRCA_FINISH_CRM06':      'CRS → LG Bala',
    'SKIN_PASS_SUPER_BRIGHT': 'Skin Pass (direct)',
    'SKIN_PASS_CHROME':       'Skin Pass (direct)',
    'SKIN_PASS_HEAVY_MATT':   'Skin Pass (direct)',
    'RE_ROLLING':             'Annealing → CRS/H&T',
    'FIRST_ROLLING':          'Annealing → CRS',
    'ROLLING':                'Rewinding → Annealing → SPM',
    'ROLLING_BRIGHT':         'Rewinding → Chrome SPM',
}

# Sections that go via CRS before final dispatch
VIA_CRS = {'TUBE_FH', 'CRCA_FINISH', 'CRCA_FINISH_CRM06'}

# Sections that go direct to downstream (no intermediate)
DIRECT_DISPATCH = {
    'HT_FINISH',              # → H&T Line directly
    'SKIN_PASS_SUPER_BRIGHT', # → Skin Pass directly
    'SKIN_PASS_CHROME',       # → Skin Pass directly
    'SKIN_PASS_HEAVY_MATT',   # → Skin Pass directly
}

# Sections that eventually reach CRS (after rolling, possibly via annealing)
REACHES_CRS = {'TUBE_FH', 'CRCA_FINISH', 'CRCA_FINISH_CRM06',
               'RE_ROLLING', 'FIRST_ROLLING'}

# Sections feeding annealing (72h cycle — need to roll NOW to return day-after-tomorrow)
FEEDS_ANNEAL = {'RE_ROLLING', 'FIRST_ROLLING', 'ROLLING', 'ROLLING_BRIGHT'}

# Base urgency of downstream consumer
# Tube Plant = 10: internal captive, line stops if no material
# H&T Line   = 9: direct from rolling, no buffer
# Skin Pass  = 8: direct from rolling, no buffer
# CRS        = 7: Tube + OEM pass through here — bottleneck
# Annealing  = 5: 72h cycle gives some buffer
CONSUMER_URGENCY = {
    'CRS → Tube Plant':       10,
    'H&T Line (direct)':       9,
    'Skin Pass (direct)':      8,
    'CRS → OEM/Dispatch':      7,
    'CRS → LG Bala':           6,
    'Annealing → CRS/H&T':    5,
    'Annealing → CRS':        5,
    'Rewinding → Annealing → SPM': 4,
    'Rewinding → Chrome SPM': 4,
}

# Mill speed MT/hour per section type (approximate)
MILL_SPEED = {
    'TUBE_FH':                15.0,
    'HT_FINISH':              13.0,
    'CRCA_FINISH':            12.0,
    'CRCA_FINISH_CRM06':      12.0,
    'SKIN_PASS_SUPER_BRIGHT': 11.0,
    'SKIN_PASS_CHROME':       10.0,
    'SKIN_PASS_HEAVY_MATT':    9.0,
    'RE_ROLLING':             16.0,
    'FIRST_ROLLING':          16.0,
    'ROLLING':                18.0,
    'ROLLING_BRIGHT':         14.0,
}

# Planning modes
MODES = {
    'BALANCED':     'Balanced — all factors equally weighted',
    'TUBE_URGENT':  'Tube Urgent — maximise Tube Plant feed',
    'HT_URGENT':    'H&T Urgent — maximise H&T Line feed',
    'MAX_PROD':     'Max Production — highest MT/hour first',
    'CLEAR_BACKLOG':'Clear Backlog — oldest coils first',
    'FEED_ANNEAL':  'Feed Annealing — prioritise pipeline for next 72h',
}

# Customer priority (informal — matches planner knowledge)
CUSTOMER_PRIORITY = {
    'SAHIBABAD TUBE PLANT':           10,
    'TUBE':                           10,
    'TMA INTERNATIONAL':               8,
    'TMA':                             8,
    'BANDSAW STRIP':                   7,
    'BANDSAW':                         7,
    'L.G BALAKRISHNAN':                5,
    'LG BALA':                         5,
    'CALLIDA':                         6,
    'VAISH':                           6,
    'RUPH':                            5,
    'SFC':                             5,
    'DEFAULT':                         4,
}


def _customer_priority_score(customer_desc: str) -> int:
    cu = str(customer_desc).upper()
    for key, score in CUSTOMER_PRIORITY.items():
        if key in cu:
            return score
    return CUSTOMER_PRIORITY['DEFAULT']


@dataclass
class SectionScore:
    section_key:    str
    mill:           str
    label:          str
    n_coils:        int
    total_mt:       float
    avg_age:        float
    max_age:        float
    downstream:     str
    feeds_anneal:   bool
    is_direct:      bool
    mt_per_hour:    float

    # Score components (0-100 each)
    downstream_score:  float = 0.0
    age_score:         float = 0.0
    customer_score:    float = 0.0
    anneal_score:      float = 0.0
    production_score:  float = 0.0

    # Final weighted score
    total_score:  float = 0.0
    rank_crm04:   int   = 0
    rank_crm06:   int   = 0

    # Warnings
    warnings: List[str] = field(default_factory=list)


def _compute_scores(section: SectionScore,
                    mode: str,
                    downstream_demand: Dict[str, float],
                    shift_no: int) -> SectionScore:
    """Compute all score components for one section."""

    # ── 1. Downstream starvation score ───────────────────────────────
    consumer = section.downstream
    base_urgency = CONSUMER_URGENCY.get(consumer, 5)
    demand_factor = downstream_demand.get(consumer, 1.0)  # 1.0=normal, >1=starved
    section.downstream_score = min(100, base_urgency * 10 * demand_factor)

    # ── 2. Age score — oldest coils score highest ─────────────────────
    # Age > 14 days = critical, 7-14 = high, 3-7 = medium, <3 = low
    if section.max_age >= 21:
        section.age_score = 100
    elif section.max_age >= 14:
        section.age_score = 80
    elif section.max_age >= 7:
        section.age_score = 55
    elif section.max_age >= 3:
        section.age_score = 30
    else:
        section.age_score = 10

    # ── 3. Customer priority score ────────────────────────────────────
    section.customer_score = min(100, base_urgency * 10)

    # ── 4. Annealing pipeline score ───────────────────────────────────
    # If feeding annealing: score is high because these coils need 72h to return
    # Shift 1/2 = roll for anneal now → returns in time for next shift cycle
    # Shift 3 = less urgent to feed anneal (returns weekend)
    if section.feeds_anneal:
        base_anneal = 70
        if shift_no == 3:
            base_anneal = 40   # night shift — less critical to feed anneal
        section.anneal_score = base_anneal
    else:
        section.anneal_score = 0

    # ── 5. Production efficiency score ────────────────────────────────
    max_speed = max(MILL_SPEED.values())
    section.production_score = (section.mt_per_hour / max_speed) * 100

    # ── Weighted total by mode ─────────────────────────────────────────
    weights = _mode_weights(mode, section)
    section.total_score = (
        section.downstream_score  * weights['downstream'] +
        section.age_score         * weights['age'] +
        section.customer_score    * weights['customer'] +
        section.anneal_score      * weights['anneal'] +
        section.production_score  * weights['production']
    )

    # ── Warnings ──────────────────────────────────────────────────────
    if section.max_age >= 21:
        section.warnings.append(
            f"⚠️ {section.max_age:.0f}-day coil — TDC expiry risk")
    if section.feeds_anneal and shift_no == 1:
        section.warnings.append(
            "🔄 Rolls today → returns from annealing in 72h (Shift 1, day after tomorrow)")
    if demand_factor >= 1.5:
        section.warnings.append(
            f"🔴 {consumer} is starved — urgent feed required")

    return section


def _mode_weights(mode: str, section: SectionScore) -> Dict[str, float]:
    """Return score weights for each planning mode."""
    if mode == 'BALANCED':
        return {'downstream': 0.30, 'age': 0.20,
                'customer': 0.25, 'anneal': 0.15, 'production': 0.10}

    elif mode == 'TUBE_URGENT':
        w = {'downstream': 0.15, 'age': 0.10,
             'customer': 0.10, 'anneal': 0.05, 'production': 0.10}
        # Massive bonus for Tube Plant sections
        if section.downstream == 'Tube Plant':
            w['downstream'] = 0.80
        return w

    elif mode == 'HT_URGENT':
        w = {'downstream': 0.15, 'age': 0.15,
             'customer': 0.10, 'anneal': 0.05, 'production': 0.05}
        if section.downstream == 'H&T Line':
            w['downstream'] = 0.80
        # Also boost RE_ROLLING since it feeds H&T after annealing
        if section.section_key == 'RE_ROLLING':
            w['anneal'] = 0.30
        return w

    elif mode == 'MAX_PROD':
        return {'downstream': 0.10, 'age': 0.05,
                'customer': 0.05, 'anneal': 0.05, 'production': 0.75}

    elif mode == 'CLEAR_BACKLOG':
        return {'downstream': 0.10, 'age': 0.70,
                'customer': 0.10, 'anneal': 0.05, 'production': 0.05}

    elif mode == 'FEED_ANNEAL':
        w = {'downstream': 0.10, 'age': 0.15,
             'customer': 0.10, 'anneal': 0.10, 'production': 0.05}
        if section.feeds_anneal:
            w['anneal'] = 0.70
        return w

    # Default = BALANCED
    return {'downstream': 0.30, 'age': 0.20,
            'customer': 0.25, 'anneal': 0.15, 'production': 0.10}


def compute_priority(
    sections: List[Dict],
    mode: str = 'BALANCED',
    shift_no: int = 1,
    downstream_demand: Optional[Dict[str, float]] = None,
) -> Dict:
    """
    Main entry point.

    Parameters
    ----------
    sections         : section list from generator.build_sections()
    mode             : planning mode key (see MODES dict)
    shift_no         : 1, 2, or 3
    downstream_demand: optional dict {consumer: demand_factor}
                       1.0 = normal, 2.0 = starved, 0.5 = surplus

    Returns
    -------
    dict with:
        crm04_sequence : ordered list of SectionScore for CRM04
        crm06_sequence : ordered list of SectionScore for CRM06
        all_scores     : all SectionScore objects
        warnings       : global warnings
        briefing       : shift briefing text (WhatsApp-ready)
        kpis           : summary metrics
    """
    if downstream_demand is None:
        downstream_demand = {}

    scored = []
    for s in sections:
        df    = s['coils_df']
        n     = len(df)
        mt    = float(df['Input Coil Weight'].sum())
        avg_a = float(df['Coil Age(# Days)'].fillna(0).mean())
        max_a = float(df['Coil Age(# Days)'].fillna(0).max())
        sk    = s['section_key']
        mill  = s['mill']
        ds    = SECTION_DOWNSTREAM.get(sk, 'Unknown')
        cust_scores = df['Customer Desc'].apply(_customer_priority_score)

        sec = SectionScore(
            section_key   = sk,
            mill          = mill,
            label         = s['label'],
            n_coils       = n,
            total_mt      = round(mt, 2),
            avg_age       = round(avg_a, 1),
            max_age       = round(max_a, 1),
            downstream    = ds,
            feeds_anneal  = sk in FEEDS_ANNEAL,
            is_direct     = sk in DIRECT_DISPATCH,
            mt_per_hour   = MILL_SPEED.get(sk, 14.0),
        )
        sec.customer_score = float(cust_scores.mean()) * 10  # scale to 0-100
        sec = _compute_scores(sec, mode, downstream_demand, shift_no)
        scored.append(sec)

    # Rank separately for CRM04 and CRM06
    crm04 = sorted(
        [s for s in scored if s.mill in ('CRM04',)],
        key=lambda x: x.total_score, reverse=True
    )
    crm06 = sorted(
        [s for s in scored if s.mill in ('CRM06',)],
        key=lambda x: x.total_score, reverse=True
    )
    # Sections on both mills (CRM04/06) — show in both lists
    both = [s for s in scored if s.mill == 'CRM04/06']
    for s in both:
        s04 = SectionScore(**{k: v for k, v in s.__dict__.items()
                               if k != 'warnings'})
        s04.mill = 'CRM04'
        s04.warnings = list(s.warnings)
        crm04.append(s04)
        crm04.sort(key=lambda x: x.total_score, reverse=True)

        s06 = SectionScore(**{k: v for k, v in s.__dict__.items()
                               if k != 'warnings'})
        s06.mill = 'CRM06'
        s06.warnings = list(s.warnings)
        crm06.append(s06)
        crm06.sort(key=lambda x: x.total_score, reverse=True)

    for i, s in enumerate(crm04, 1): s.rank_crm04 = i
    for i, s in enumerate(crm06, 1): s.rank_crm06 = i

    # Global warnings
    global_warnings = []
    direct_mt = sum(s.total_mt for s in scored if s.is_direct)
    anneal_mt = sum(s.total_mt for s in scored if s.feeds_anneal)

    # Recalculate consumer-specific MT with updated downstream labels
    tube_mt = sum(s.total_mt for s in scored
                  if 'Tube Plant' in s.downstream)
    ht_mt   = sum(s.total_mt for s in scored
                  if 'H&T Line'  in s.downstream)
    crs_mt  = sum(s.total_mt for s in scored
                  if 'CRS'       in s.downstream)
    spm_mt  = sum(s.total_mt for s in scored
                  if 'Skin Pass' in s.downstream)
    ann_mt  = sum(s.total_mt for s in scored
                  if 'Annealing' in s.downstream)
    if direct_mt < 300:
        global_warnings.append(
            f"⚠️ Only {direct_mt:.0f} MT going direct to dispatch — "
            f"downstream consumers may be under-fed today")
    if anneal_mt > 400 and mode != 'FEED_ANNEAL':
        global_warnings.append(
            f"🔄 {anneal_mt:.0f} MT going to annealing — "
            f"ensure furnace capacity is available")

    # KPIs
    # KPIs — use substring match since downstream labels now include flow description
    total_mt = sum(s.total_mt for s in scored)
    tube_mt  = sum(s.total_mt for s in scored if 'Tube Plant' in s.downstream)
    ht_mt    = sum(s.total_mt for s in scored if 'H&T Line'   in s.downstream)
    crs_mt   = sum(s.total_mt for s in scored if 'CRS'        in s.downstream)
    spm_mt   = sum(s.total_mt for s in scored if 'Skin Pass'  in s.downstream)
    ann_mt   = sum(s.total_mt for s in scored if 'Annealing'  in s.downstream)

    # CRS load = Tube FH + CRCA (direct via CRS) + eventual from annealing
    crs_direct_mt = sum(s.total_mt for s in scored if s.section_key in VIA_CRS)

    kpis = {
        'total_mt':       round(total_mt, 1),
        'tube_mt':        round(tube_mt, 1),
        'ht_mt':          round(ht_mt, 1),
        'crs_direct_mt':  round(crs_direct_mt, 1),   # going via CRS today
        'spm_mt':         round(spm_mt, 1),
        'anneal_mt':      round(ann_mt, 1),
        'direct_mt':      round(direct_mt, 1),        # direct to H&T + SPM
        'mode':           mode,
        'mode_desc':      MODES.get(mode, mode),
        'shift_no':       shift_no,
    }

    briefing = _generate_briefing(crm04, crm06, kpis,
                                  global_warnings, mode, shift_no)

    return {
        'crm04_sequence': crm04,
        'crm06_sequence': crm06,
        'all_scores':     scored,
        'warnings':       global_warnings,
        'briefing':       briefing,
        'kpis':           kpis,
    }


def _generate_briefing(crm04, crm06, kpis, warnings,
                       mode, shift_no) -> str:
    """Generate a plain-text shift briefing ready for WhatsApp."""
    shift_names = {1: 'Shift 1 (06:00-14:00)',
                   2: 'Shift 2 (14:00-22:00)',
                   3: 'Shift 3 (22:00-06:00)'}
    lines = [
        f"🏭 *CRM NARROW COMPLEX — SHIFT PLAN*",
        f"📅 {shift_names.get(shift_no, f'Shift {shift_no}')}",
        f"⚙️  Mode: {MODES.get(mode, mode)}",
        f"",
        f"📊 *PLAN SUMMARY*",
        f"Total MT planned   : {kpis['total_mt']} MT",
        f"",
        f"📦 Via CRS → Dispatch:",
        f"  → Tube Plant (C09)  : {kpis['tube_mt']} MT",
        f"  → OEM/LG Bala (CRCA): {round(kpis['crs_direct_mt']-kpis['tube_mt'],1)} MT",
        f"  → CRS total load    : {kpis['crs_direct_mt']} MT",
        f"",
        f"📦 Direct Dispatch (no CRS):",
        f"  → H&T Line          : {kpis['ht_mt']} MT",
        f"  → Skin Pass         : {kpis['spm_mt']} MT",
        f"",
        f"🔄 To Annealing (returns ~72h):",
        f"  → Annealing feed    : {kpis['anneal_mt']} MT",
        f"",
        f"🎯 *CRM-04 PRIORITY SEQUENCE*",
    ]
    for s in crm04[:5]:
        score_bar = '█' * int(s.total_score / 10)
        lines.append(
            f"  {s.rank_crm04}. {s.section_key.replace('_',' ').title():30s}"
            f" {s.total_mt:6.1f}MT  Score:{s.total_score:.0f}")
        if s.warnings:
            for w in s.warnings[:1]:
                lines.append(f"     {w}")

    lines += ["", "🎯 *CRM-06 PRIORITY SEQUENCE*"]
    for s in crm06[:5]:
        lines.append(
            f"  {s.rank_crm06}. {s.section_key.replace('_',' ').title():30s}"
            f" {s.total_mt:6.1f}MT  Score:{s.total_score:.0f}")
        if s.warnings:
            for w in s.warnings[:1]:
                lines.append(f"     {w}")

    if warnings:
        lines += ["", "⚠️ *WARNINGS*"]
        for w in warnings:
            lines.append(f"  {w}")

    lines += [
        "",
        "📌 *NOTE*: This is a recommendation. Shift-in-charge has final call.",
    ]
    return '\n'.join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# CRS OPTIMISER — Minimum Setting Changes
# ══════════════════════════════════════════════════════════════════════════════

# What constitutes a CRS setting change:
# 1. Width change > 2mm  (requires guide adjustment)
# 2. Thickness change > 0.05mm (requires pressure/tension adjustment)
# 3. Product change C09 → C01 (requires blade/tension setup change) — MAJOR
# 4. Customer change (requires label/inspection setup) — MINOR
#
# Weights for change cost:
CRS_CHANGE_COST = {
    'width_per_mm':    0.5,   # cost per mm of width change
    'thick_per_0.1mm': 2.0,   # cost per 0.1mm thickness change
    'product_change':  10.0,  # C09 ↔ C01 is a major setup
    'customer_change':  1.0,  # label / inspection change only
}


def _crs_change_cost(coil_a: dict, coil_b: dict) -> float:
    """
    Compute the setup cost of running coil_b immediately after coil_a at CRS.
    Lower = better (fewer / cheaper changes).
    """
    cost = 0.0

    w_diff = abs(coil_a['width'] - coil_b['width'])
    t_diff = abs(coil_a['thick'] - coil_b['thick'])
    cost += w_diff * CRS_CHANGE_COST['width_per_mm']
    cost += (t_diff / 0.1) * CRS_CHANGE_COST['thick_per_0.1mm']

    if coil_a.get('product') != coil_b.get('product'):
        cost += CRS_CHANGE_COST['product_change']

    if coil_a.get('customer') != coil_b.get('customer'):
        cost += CRS_CHANGE_COST['customer_change']

    return round(cost, 2)


def _count_changes(sequence: list) -> int:
    """Count number of actual setting changes in a CRS sequence."""
    changes = 0
    for i in range(len(sequence) - 1):
        a, b = sequence[i], sequence[i + 1]
        if (abs(a['width'] - b['width']) > 2 or
                abs(a['thick'] - b['thick']) > 0.05 or
                a.get('product') != b.get('product')):
            changes += 1
    return changes


def optimise_crs_sequence(sections: list) -> dict:
    """
    Given today's plan sections, find the CRS coil sequence that
    minimises total setting changes using a nearest-neighbour greedy
    optimiser with 2-opt improvement.

    Parameters
    ----------
    sections : section dicts from generator.build_sections()

    Returns
    -------
    dict with:
        original_sequence   : coils in current plan order
        optimised_sequence  : coils in CRS-optimal order
        original_changes    : number of setting changes in original
        optimised_changes   : number of setting changes in optimised
        changes_saved       : reduction
        total_cost_original : weighted cost score
        total_cost_optimised: weighted cost score
        change_events       : list of change events in optimised sequence
        width_groups        : coils grouped by width band
        recommendations     : actionable text
    """
    via_crs = ['TUBE_FH', 'CRCA_FINISH', 'CRCA_FINISH_CRM06']

    # Build flat coil list for CRS
    crs_coils = []
    for s in sections:
        if s['section_key'] not in via_crs:
            continue
        for _, row in s['coils_df'].iterrows():
            crs_coils.append({
                'coil_number': str(row.get('Coil Number', '')),
                'width':       float(row.get('Actual Width', 0)),
                'thick':       float(row.get('Plan Rolling Thick 1', 0)),
                'weight':      float(row.get('Input Coil Weight', 0)),
                'product':     str(row.get('Product Code', '')),
                'customer':    str(row.get('Customer Desc', '')).strip()[:20],
                'section':     s['section_key'],
                'age':         float(row.get('Coil Age(# Days)', 0)),
            })

    if not crs_coils:
        return {'error': 'No CRS coils in plan'}

    # Original sequence cost
    orig_cost    = sum(_crs_change_cost(crs_coils[i], crs_coils[i+1])
                       for i in range(len(crs_coils)-1))
    orig_changes = _count_changes(crs_coils)

    # ── Greedy nearest-neighbour ──────────────────────────────────────
    def greedy_from(start_idx):
        remaining = crs_coils[:]
        seq = [remaining.pop(start_idx)]
        while remaining:
            last = seq[-1]
            best = min(remaining,
                       key=lambda c: _crs_change_cost(last, c))
            seq.append(best)
            remaining.remove(best)
        return seq

    best_seq  = crs_coils[:]
    best_cost = orig_cost
    for i in range(len(crs_coils)):
        seq  = greedy_from(i)
        cost = sum(_crs_change_cost(seq[j], seq[j+1])
                   for j in range(len(seq)-1))
        if cost < best_cost:
            best_cost = cost
            best_seq  = seq

    # ── 2-opt improvement ─────────────────────────────────────────────
    improved = True
    while improved:
        improved = False
        for i in range(1, len(best_seq) - 1):
            for j in range(i + 1, len(best_seq)):
                new_seq = best_seq[:i] + best_seq[i:j+1][::-1] + best_seq[j+1:]
                new_cost = sum(_crs_change_cost(new_seq[k], new_seq[k+1])
                               for k in range(len(new_seq)-1))
                if new_cost < best_cost - 0.01:
                    best_seq  = new_seq
                    best_cost = new_cost
                    improved  = True

    opt_changes = _count_changes(best_seq)
    saved       = orig_changes - opt_changes

    # ── Build change event list ───────────────────────────────────────
    change_events = []
    for i in range(len(best_seq) - 1):
        a, b = best_seq[i], best_seq[i+1]
        events = []
        w_diff = abs(a['width'] - b['width'])
        t_diff = abs(a['thick'] - b['thick'])
        if w_diff > 2:
            events.append(f"Width: {a['width']:.0f}→{b['width']:.0f}mm "
                          f"(Δ{w_diff:.0f}mm)")
        if t_diff > 0.05:
            events.append(f"Thickness: {a['thick']:.2f}→{b['thick']:.2f}mm "
                          f"(Δ{t_diff:.2f}mm)")
        if a['product'] != b['product']:
            events.append(f"Product: {a['product']}→{b['product']} ⚠️ MAJOR")
        if events:
            change_events.append({
                'position':    i + 1,
                'from_coil':   a['coil_number'],
                'to_coil':     b['coil_number'],
                'from_width':  a['width'],
                'to_width':    b['width'],
                'from_thick':  a['thick'],
                'to_thick':    b['thick'],
                'changes':     events,
                'change_cost': _crs_change_cost(a, b),
                'is_major':    a['product'] != b['product'],
            })

    # ── Width group summary ───────────────────────────────────────────
    from collections import defaultdict
    width_groups = defaultdict(list)
    for c in best_seq:
        band = f"{int(round(c['width']/10)*10)} mm band"
        width_groups[band].append(c)

    # ── Recommendations ───────────────────────────────────────────────
    recs = []
    if saved > 0:
        recs.append(
            f"✅ Resequencing saves {saved} setting change(s) "
            f"({orig_changes} → {opt_changes}) — "
            f"estimated {saved * 8:.0f} min less downtime at CRS")
    else:
        recs.append("✅ Current sequence is already optimal for CRS")

    major = [e for e in change_events if e['is_major']]
    if major:
        recs.append(
            f"⚠️ {len(major)} product change(s) (C09↔C01) are unavoidable "
            f"— these require full CRS setup. "
            f"Schedule these at shift start or after a break.")

    # Width cascade check
    widths = [c['width'] for c in best_seq]
    if all(widths[i] >= widths[i+1] - 2 for i in range(len(widths)-1)):
        recs.append("✅ Width cascade maintained throughout CRS sequence")
    else:
        recs.append("⚠️ Some width step-ups in optimised sequence — "
                    "check roll edge condition before running narrow→wide")

    return {
        'original_sequence':    crs_coils,
        'optimised_sequence':   best_seq,
        'original_changes':     orig_changes,
        'optimised_changes':    opt_changes,
        'changes_saved':        saved,
        'total_cost_original':  round(orig_cost, 1),
        'total_cost_optimised': round(best_cost, 1),
        'change_events':        change_events,
        'width_groups':         dict(width_groups),
        'recommendations':      recs,
        'total_coils':          len(crs_coils),
        'total_mt':             round(sum(c['weight'] for c in crs_coils), 2),
    }


# ══════════════════════════════════════════════════════════════════════════════
# DOWNSTREAM DEPLETION FORECASTER — predict when H&T / CRS / SPM run out
# ══════════════════════════════════════════════════════════════════════════════

# Daily consumption capacity of each downstream stage (MT/day)
# These are editable defaults — adjust in the UI to match actual rates
DEFAULT_CONSUMPTION = {
    'CRS':       110.0,   # C R Slitter throughput MT/day
    'H&T':        45.0,   # H&T furnace line MT/day
    'SPM':        60.0,   # Skin Pass Mill MT/day
    'Annealing': 130.0,   # BAF charge capacity MT/day
}

# Which Current Stage values count as buffer for each consumer
BUFFER_STAGES = {
    'CRS':       ['C R SLITTER'],
    'H&T':       ['FURNACE'],
    'SPM':       ['SPM'],
    'Annealing': ['ANB', 'ANNEALING'],
}

# Which plan sections add to each consumer's buffer (today's rolling output)
SECTION_FEEDS = {
    'CRS':       ['TUBE_FH', 'CRCA_FINISH', 'CRCA_FINISH_CRM06'],
    'H&T':       ['HT_FINISH'],
    'SPM':       ['SKIN_PASS_SUPER_BRIGHT', 'SKIN_PASS_CHROME',
                  'SKIN_PASS_HEAVY_MATT'],
    'Annealing': ['RE_ROLLING', 'FIRST_ROLLING', 'ROLLING'],
}

ALERT_DAYS = {'critical': 1.0, 'warning': 2.0, 'watch': 3.5}


def forecast_depletion(wip_df, sections,
                       consumption: dict = None,
                       horizon_days: int = 7) -> dict:
    """
    Forecast when each downstream consumer's buffer runs out.

    Parameters
    ----------
    wip_df      : FULL raw WIP DataFrame (all stages, not just Rolling Mill)
    sections    : today's plan sections from build_sections()
    consumption : {consumer: MT/day} — override defaults
    horizon_days: forecast window

    Returns dict per consumer:
        buffer_mt, buffer_coils, incoming_today_mt, consumption_rate,
        days_to_empty, empty_date, status, day_by_day projection
    """
    import pandas as _pd
    from datetime import datetime, timedelta

    cons = dict(DEFAULT_CONSUMPTION)
    if consumption:
        cons.update(consumption)

    wip = wip_df.copy()
    wip.columns = wip.columns.str.strip()
    wip['Input Coil Weight'] = _pd.to_numeric(
        wip['Input Coil Weight'], errors='coerce').fillna(0)
    # Some rows have weight in kg — normalise anything > 100 to MT
    wip.loc[wip['Input Coil Weight'] > 100, 'Input Coil Weight'] /= 1000.0

    # Today's plan output by consumer
    plan_feed = {k: 0.0 for k in SECTION_FEEDS}
    for s in sections:
        for consumer, sec_keys in SECTION_FEEDS.items():
            if s['section_key'] in sec_keys:
                plan_feed[consumer] += float(
                    s['coils_df']['Input Coil Weight'].sum())

    results = {}
    today = datetime.now().date()

    for consumer, stages in BUFFER_STAGES.items():
        buf = wip[wip['Current Stage'].isin(stages)]
        buffer_mt    = round(float(buf['Input Coil Weight'].sum()), 1)
        buffer_coils = len(buf)
        rate         = cons.get(consumer, 50.0)
        incoming     = round(plan_feed.get(consumer, 0.0), 1)

        # Annealing 72h return: today's anneal feed reaches CRS/H&T at day 3
        # Simple projection: buffer + today's incoming, drain at `rate`/day
        projection = []
        level = buffer_mt
        empty_day = None
        for d in range(horizon_days + 1):
            if d == 0:
                level += incoming   # today's rolling lands in buffer
            else:
                level -= rate
            level = max(level, 0.0)
            projection.append({'day': d,
                               'date': str(today + timedelta(days=d)),
                               'buffer_mt': round(level, 1)})
            if empty_day is None and level <= 0:
                empty_day = d

        days_to_empty = round((buffer_mt + incoming) / rate, 1) if rate else 99
        if   days_to_empty <= ALERT_DAYS['critical']: status = 'CRITICAL'
        elif days_to_empty <= ALERT_DAYS['warning']:  status = 'WARNING'
        elif days_to_empty <= ALERT_DAYS['watch']:    status = 'WATCH'
        else:                                          status = 'OK'

        # How much must be rolled today to keep N days of cover
        target_cover_days = 3.0
        required_today = max(0.0, round(
            target_cover_days * rate - buffer_mt, 1))

        results[consumer] = {
            'buffer_mt':        buffer_mt,
            'buffer_coils':     buffer_coils,
            'incoming_today_mt': incoming,
            'consumption_rate': rate,
            'days_to_empty':    days_to_empty,
            'empty_date':       (str(today + timedelta(days=int(days_to_empty)))
                                 if days_to_empty < horizon_days else None),
            'status':           status,
            'required_today_mt': required_today,
            'projection':       projection,
        }

    return results


# ══════════════════════════════════════════════════════════════════════════════
# ORDERED ROLLING SHEET — printable per-mill coil list with section headers
# ══════════════════════════════════════════════════════════════════════════════

def build_rolling_sheet(sections, priority_result=None) -> dict:
    """
    Build an ordered, header-grouped rolling list per mill.
    If priority_result is given (from compute_priority), sections are
    ordered by priority rank; otherwise plan order is kept.

    Returns {'CRM04': [...], 'CRM06': [...]} where each item is either
    {'type':'header', ...} or {'type':'coil', ...} — ready for UI/print.
    """
    rank_map = {}
    if priority_result:
        for s in priority_result.get('crm04_sequence', []):
            rank_map[('CRM04', s.section_key)] = s.rank_crm04
        for s in priority_result.get('crm06_sequence', []):
            rank_map[('CRM06', s.section_key)] = s.rank_crm06

    sheets = {'CRM04': [], 'CRM06': []}
    for mill in ('CRM04', 'CRM06'):
        mill_secs = [s for s in sections if s['mill'] == mill]
        mill_secs.sort(key=lambda s: rank_map.get((mill, s['section_key']), 99))

        running_no = 0
        for rank_i, s in enumerate(mill_secs, 1):
            df = s['coils_df']
            sheets[mill].append({
                'type':       'header',
                'priority':   rank_i,
                'section':    s['section_key'],
                'label':      s['label'],
                'coil_count': len(df),
                'total_mt':   round(float(df['Input Coil Weight'].sum()), 1),
            })
            for _, r in df.iterrows():
                running_no += 1
                sheets[mill].append({
                    'type':     'coil',
                    'seq':      running_no,
                    'coil':     str(r.get('Coil Number', '')),
                    'width':    float(r.get('Actual Width', 0) or 0),
                    'thick':    float(r.get('Actual Thick', 0) or 0),
                    'rt':       float(r.get('Plan Rolling Thick 1', 0) or 0),
                    'weight':   round(float(r.get('Input Coil Weight', 0) or 0), 3),
                    'customer': str(r.get('Customer Desc', ''))[:18],
                    'remark':   str(r.get('Planning Remark', ''))[:25],
                })
    return sheets
