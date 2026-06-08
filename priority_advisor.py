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
