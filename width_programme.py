"""
width_programme.py — Width Programme & Cross-Section Transition Optimiser
==========================================================================
Feature 6 from the roadmap:

The standard optimiser (optimiser.py) minimises roll CHANGES between sections.
This module goes one level deeper — it minimises the ROLL EDGE STRESS when
transitioning between sections by considering:

1. Width proximity: if two adjacent sections both have similar max-widths,
   the roll edge wear profile is compatible → lower transition cost even if
   roll type changes.

2. Width step-down rule: when moving from a wide-coil section to a narrow-coil
   section on the SAME roll type, the roll already has an edge impression at
   the wider gauge. Running narrow coils next accelerates edge cracking.
   Better to: finish all wide coils across sections before switching to narrow.

3. Cross-section width cascade: identifies cases where coils from DIFFERENT
   sections share the same width band and could be interleaved into a combined
   programme — one continuous width-descending run across multiple grade types.

4. Transition matrix: for every pair of adjacent sections in the plan,
   compute a "transition cost score" (0–100) based on:
     - Roll type change cost (from optimiser.py)
     - Width gap at transition point (last coil of section A vs first of section B)
     - Thickness step change stress
"""

from __future__ import annotations
from typing import List, Dict, Tuple, Optional
import pandas as pd
from optimiser import ROLL_TYPE, get_change_cost

# ── Width gap penalty (mm difference → extra stress score) ──────────────────
def width_gap_penalty(w_last: float, w_first: float) -> float:
    """
    Score 0–50 for the width discontinuity at a section transition.
    A gap of 0 mm  → 0 penalty
    A gap of 50 mm → 15 penalty
    A gap of 150mm → 40 penalty
    A gap of 200mm → 50 penalty (max)
    """
    gap = abs(w_last - w_first)
    if gap <= 10:   return 0
    if gap <= 50:   return gap * 0.3
    if gap <= 100:  return 15 + (gap - 50) * 0.3
    if gap <= 200:  return 30 + (gap - 100) * 0.2
    return 50.0


def thickness_step_stress(t_last: float, t_first: float) -> float:
    """
    Stress score for thickness discontinuity (sudden force change on rolls).
    Same thickness band → 0
    Big step up (thicker next) → moderate (mill adjusts hydraulically)
    Big step down → higher (risk of chatter)
    """
    diff = t_first - t_last
    if abs(diff) < 0.2:   return 0
    if diff > 0:          return min(20, diff * 5)    # thicker next — manageable
    return min(30, abs(diff) * 8)                      # thinner next — worse


def transition_score(sec_a: Dict, sec_b: Dict) -> Dict:
    """
    Compute a comprehensive transition score between two consecutive sections.
    Lower = better transition.

    Returns dict with score breakdown.
    """
    roll_a = ROLL_TYPE.get(sec_a['section_key'], 'UNKNOWN')
    roll_b = ROLL_TYPE.get(sec_b['section_key'], 'UNKNOWN')
    roll_cost = get_change_cost(roll_a, roll_b)   # minutes

    df_a = sec_a['coils_df']
    df_b = sec_b['coils_df']

    # Last coil of section A, first coil of section B (after width-desc sort)
    last_w  = float(df_a['Actual Width'].iloc[-1])     if len(df_a) else 0
    first_w = float(df_b['Actual Width'].iloc[0])      if len(df_b) else 0
    last_t  = float(df_a['Actual Thick'].iloc[-1])     if len(df_a) else 0
    first_t = float(df_b['Actual Thick'].iloc[0])      if len(df_b) else 0

    w_penalty = width_gap_penalty(last_w, first_w)
    t_stress  = thickness_step_stress(last_t, first_t)
    roll_norm = min(50, roll_cost / 1.2)   # normalise 60min → 50pts

    total = roll_norm + w_penalty + t_stress

    # Width direction: ascending step (last < first) is WORSE (rolling wider after narrower)
    width_direction = 'STEP_UP' if first_w > last_w + 10 else \
                      'STEP_DOWN' if last_w > first_w + 10 else 'SIMILAR'

    return {
        'from_section':    sec_a['section_key'],
        'to_section':      sec_b['section_key'],
        'from_roll':       roll_a,
        'to_roll':         roll_b,
        'roll_change_min': roll_cost,
        'last_width_mm':   last_w,
        'first_width_mm':  first_w,
        'width_gap_mm':    round(abs(last_w - first_w), 0),
        'width_direction': width_direction,
        'last_thick_mm':   last_t,
        'first_thick_mm':  first_t,
        'w_penalty':       round(w_penalty, 1),
        't_stress':        round(t_stress, 1),
        'roll_cost_score': round(roll_norm, 1),
        'total_score':     round(total, 1),
        'rating':          'POOR' if total > 60 else
                           'FAIR' if total > 30 else
                           'GOOD' if total > 10 else 'EXCELLENT',
    }


def analyse_width_programme(sections: List[Dict]) -> Dict:
    """
    Full width programme analysis across all sections.

    Returns:
        transition_matrix : score for each consecutive section pair
        width_cascade_check : per-section cascade continuity
        cross_section_groups : sections that share width bands (merge candidates)
        recommendations : list of actionable suggestions
        programme_score : overall programme quality 0-100
    """
    # ── 1. Transition matrix ───────────────────────────────────────────
    transitions = []
    for i in range(len(sections) - 1):
        t = transition_score(sections[i], sections[i + 1])
        transitions.append(t)

    # ── 2. Per-section width cascade check ────────────────────────────
    cascade_issues = []
    for s in sections:
        df = s['coils_df']
        if len(df) < 2:
            continue
        widths = df['Actual Width'].tolist()
        violations = []
        for i in range(len(widths) - 1):
            if widths[i+1] > widths[i] + 2:   # allow 2mm tolerance
                violations.append({
                    'position': i + 1,
                    'width_before': widths[i],
                    'width_after':  widths[i+1],
                    'step_up_mm':   round(widths[i+1] - widths[i], 0),
                })
        if violations:
            cascade_issues.append({
                'section':    s['section_key'],
                'mill':       s['mill'],
                'violations': violations,
                'count':      len(violations),
            })

    # ── 3. Cross-section width band grouping ──────────────────────────
    WIDTH_BANDS = [
        ('WIDE',   460, 600),
        ('MID',    400, 459),
        ('NARROW', 300, 399),
    ]
    band_groups = {b[0]: [] for b in WIDTH_BANDS}
    for s in sections:
        df = s['coils_df']
        if df.empty:
            continue
        for band_name, lo, hi in WIDTH_BANDS:
            band_coils = df[
                (df['Actual Width'] >= lo) & (df['Actual Width'] <= hi)
            ]
            if len(band_coils) >= 2:
                band_groups[band_name].append({
                    'section':   s['section_key'],
                    'mill':      s['mill'],
                    'roll_type': ROLL_TYPE.get(s['section_key'], '?'),
                    'coils':     len(band_coils),
                    'mt':        round(float(band_coils['Input Coil Weight'].sum()), 1),
                    'max_width': float(band_coils['Actual Width'].max()),
                    'min_width': float(band_coils['Actual Width'].min()),
                })

    # ── 4. Recommendations ────────────────────────────────────────────
    recommendations = []

    # Flag poor transitions
    for t in transitions:
        if t['rating'] == 'POOR':
            recommendations.append({
                'type':     'POOR_TRANSITION',
                'severity': 'HIGH',
                'title':    f"Poor transition: {t['from_section']} → {t['to_section']}",
                'detail':   (
                    f"Score {t['total_score']:.0f}/100 (POOR)\n"
                    f"Width gap: {t['width_gap_mm']:.0f} mm "
                    f"({t['last_width_mm']:.0f} → {t['first_width_mm']:.0f} mm)\n"
                    f"Roll change: {t['roll_change_min']} min\n"
                    f"Direction: {t['width_direction']}"
                ),
                'suggestion': (
                    f"Consider grouping narrow coils from '{t['from_section']}' "
                    f"at the end and wide coils in '{t['to_section']}' at the start "
                    f"to reduce the width gap at this transition."
                ),
            })
        elif t['rating'] == 'FAIR' and t['width_direction'] == 'STEP_UP':
            recommendations.append({
                'type':     'WIDTH_STEP_UP',
                'severity': 'MEDIUM',
                'title':    f"Width step-up at {t['from_section']} → {t['to_section']}",
                'detail':   (
                    f"Last coil of '{t['from_section']}': {t['last_width_mm']:.0f} mm\n"
                    f"First coil of '{t['to_section']}': {t['first_width_mm']:.0f} mm\n"
                    f"Step-up of {t['width_gap_mm']:.0f} mm — roll edge at risk."
                ),
                'suggestion': (
                    f"Move wider coils in '{t['to_section']}' earlier, "
                    f"or reorder the sections so wider sections come first."
                ),
            })

    # Cross-section merge opportunities
    for band_name, group in band_groups.items():
        same_roll_sections = {}
        for g in group:
            rt = g['roll_type']
            same_roll_sections.setdefault(rt, []).append(g)
        for rt, secs in same_roll_sections.items():
            if len(secs) >= 2:
                total_mt = sum(g['mt'] for g in secs)
                sec_names = [g['section'] for g in secs]
                recommendations.append({
                    'type':     'MERGE_OPPORTUNITY',
                    'severity': 'INFO',
                    'title':    f"{band_name} width band ({rt}): "
                                f"{len(secs)} sections share this width range",
                    'detail':   (
                        f"Sections: {', '.join(sec_names)}\n"
                        f"Combined MT in this band: {total_mt:.1f} MT\n"
                        f"All on {rt} rolls — could run as one continuous programme."
                    ),
                    'suggestion': (
                        f"Group {band_name.lower()} coils from all "
                        f"{rt} sections into one continuous run "
                        f"before switching to narrower coils. "
                        f"Reduces edge step marks and extends roll life."
                    ),
                })

    # Width cascade violations
    for issue in cascade_issues:
        recommendations.append({
            'type':     'CASCADE_VIOLATION',
            'severity': 'MEDIUM',
            'title':    f"Width cascade violation in {issue['section']} "
                        f"({issue['count']} step-ups found)",
            'detail':   '\n'.join(
                f"  Position {v['position']}: "
                f"{v['width_before']:.0f} → {v['width_after']:.0f} mm "
                f"(+{v['step_up_mm']:.0f} mm)"
                for v in issue['violations'][:5]
            ),
            'suggestion': 'Re-sort section by Actual Width descending.',
        })

    # ── 5. Overall programme score ─────────────────────────────────────
    if transitions:
        avg_score = sum(t['total_score'] for t in transitions) / len(transitions)
        programme_score = max(0, round(100 - avg_score, 1))
    else:
        programme_score = 100.0

    return {
        'transitions':          transitions,
        'cascade_issues':       cascade_issues,
        'band_groups':          band_groups,
        'recommendations':      recommendations,
        'programme_score':      programme_score,
        'programme_rating':     'EXCELLENT' if programme_score >= 85 else
                                'GOOD'      if programme_score >= 65 else
                                'FAIR'      if programme_score >= 45 else 'POOR',
        'summary': {
            'n_transitions':    len(transitions),
            'poor_transitions': sum(1 for t in transitions if t['rating'] == 'POOR'),
            'fair_transitions': sum(1 for t in transitions if t['rating'] == 'FAIR'),
            'good_transitions': sum(1 for t in transitions
                                    if t['rating'] in ('GOOD', 'EXCELLENT')),
            'cascade_violations': sum(i['count'] for i in cascade_issues),
            'merge_opportunities': sum(
                1 for r in recommendations if r['type'] == 'MERGE_OPPORTUNITY'
            ),
        }
    }
