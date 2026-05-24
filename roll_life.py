"""
roll_life.py — Roll Life Tracker & Campaign Planner
====================================================
Tracks how many MT each roll type can handle before dressing/replacement,
warns when the current plan will exhaust a roll mid-campaign, and suggests
where to insert roll changes to avoid unplanned stoppages.

Roll life values (MT) — based on CRM Sahibabad typical data:
  These are CONFIGURABLE — the planner can update them from the UI.
  Stored in Supabase learning_db under key 'roll_life_config'.
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional, Tuple
import json

# ── Default roll life limits (MT per campaign) ──────────────────────────────
DEFAULT_ROLL_LIFE = {
    # CRM04
    'CRM04': {
        'LIGHT_MATT':    300.0,   # roughing rolls — high life
        'BRIGHT':        180.0,   # finish rolls — moderate life
        'SUPER_BRIGHT':  120.0,   # precision finish — lower life
        'CHROME_PLATED':  80.0,   # most expensive, shortest campaign
    },
    # CRM06
    'CRM06': {
        'LIGHT_MATT':    280.0,
        'BRIGHT':        160.0,
        'HEAVY_MATT':    200.0,   # robust roll, longer life
    },
}

# Roll type for every section key (mirror of optimiser.py)
from optimiser import ROLL_TYPE


@dataclass
class RollState:
    """Current state of one roll on one mill."""
    mill:          str
    roll_type:     str
    mt_used:       float = 0.0          # MT already rolled on this roll
    mt_life:       float = 0.0          # total MT life of this roll
    roll_number:   str   = ""           # optional identifier
    installed_date: str  = ""

    @property
    def mt_remaining(self) -> float:
        return max(0.0, self.mt_life - self.mt_used)

    @property
    def pct_used(self) -> float:
        if self.mt_life <= 0:
            return 0.0
        return min(100.0, (self.mt_used / self.mt_life) * 100)

    @property
    def status(self) -> str:
        p = self.pct_used
        if p >= 95:  return 'CRITICAL'
        if p >= 80:  return 'WARNING'
        if p >= 60:  return 'MONITOR'
        return 'OK'


@dataclass
class CampaignSegment:
    """One continuous run of a roll type within a plan."""
    mill:          str
    roll_type:     str
    sections:      List[str]
    total_mt:      float
    coil_count:    int
    start_mt_used: float        # roll MT used at start of this segment
    end_mt_used:   float        # roll MT used at end of this segment
    mt_life:       float        # roll life limit
    exhausts_at:   Optional[str] = None   # section key where roll exhausts
    exhausts_after_mt: float = 0.0        # MT into that section when it exhausts

    @property
    def will_exhaust(self) -> bool:
        return self.end_mt_used > self.mt_life

    @property
    def pct_consumed(self) -> float:
        if self.mt_life <= 0: return 0.0
        return min(100.0, (self.end_mt_used / self.mt_life) * 100)


def analyse_roll_life(
    sections: List[Dict],
    crm04_state: RollState,
    crm06_state: RollState,
    roll_life_config: Optional[Dict] = None,
) -> Dict:
    """
    Main analysis function.

    Parameters
    ----------
    sections         : ordered list of section dicts from generator
    crm04_state      : current roll state on CRM04 (type + MT already used)
    crm06_state      : current roll state on CRM06
    roll_life_config : optional override dict {mill: {roll_type: mt_life}}

    Returns
    -------
    dict with warnings, campaigns, recommended change points, summary
    """
    config = roll_life_config or DEFAULT_ROLL_LIFE

    # ── Build per-mill section sequences with tonnage ──────────────────
    crm04_secs = [s for s in sections if s['mill'] in ('CRM04', 'CRM04/06')]
    crm06_secs = [s for s in sections if s['mill'] in ('CRM06', 'CRM04/06')]

    def _simulate(mill_secs, roll_state, mill_label):
        mill_config = config.get(mill_label, {})
        campaigns   = []
        warnings    = []
        recommendations = []

        current_roll = roll_state.roll_type
        current_mt   = roll_state.mt_used
        current_life = roll_state.mt_life or \
                       mill_config.get(current_roll, 150.0)

        seg_sections = []
        seg_mt       = 0.0
        seg_coils    = 0
        seg_start_mt = current_mt

        for s in mill_secs:
            s_roll = ROLL_TYPE.get(s['section_key'], 'UNKNOWN')
            s_mt   = float(s['coils_df']['Input Coil Weight'].sum())
            s_n    = len(s['coils_df'])

            if s_roll != current_roll:
                # Close current campaign segment
                if seg_sections:
                    seg = CampaignSegment(
                        mill          = mill_label,
                        roll_type     = current_roll,
                        sections      = seg_sections[:],
                        total_mt      = seg_mt,
                        coil_count    = seg_coils,
                        start_mt_used = seg_start_mt,
                        end_mt_used   = seg_start_mt + seg_mt,
                        mt_life       = current_life,
                    )
                    # Check if it will exhaust
                    if seg.will_exhaust:
                        _mark_exhaustion(seg, mill_secs, mill_config)
                    campaigns.append(seg)

                # Roll change here
                new_life = mill_config.get(s_roll, 150.0)
                warnings.append({
                    'type':     'ROLL_CHANGE',
                    'mill':     mill_label,
                    'from_roll': current_roll,
                    'to_roll':   s_roll,
                    'after_section': seg_sections[-1] if seg_sections else '—',
                    'before_section': s['section_key'],
                    'mt_on_old_roll': current_mt + seg_mt,
                    'pct_used': min(100, (current_mt + seg_mt) / current_life * 100)
                        if current_life > 0 else 0,
                })
                current_roll  = s_roll
                current_life  = new_life
                current_mt    = seg_start_mt + seg_mt  # carry forward
                seg_start_mt  = current_mt
                seg_sections  = []
                seg_mt        = 0.0
                seg_coils     = 0

            seg_sections.append(s['section_key'])
            seg_mt    += s_mt
            seg_coils += s_n

        # Final segment
        if seg_sections:
            seg = CampaignSegment(
                mill          = mill_label,
                roll_type     = current_roll,
                sections      = seg_sections,
                total_mt      = seg_mt,
                coil_count    = seg_coils,
                start_mt_used = seg_start_mt,
                end_mt_used   = seg_start_mt + seg_mt,
                mt_life       = current_life,
            )
            if seg.will_exhaust:
                _mark_exhaustion(seg, mill_secs, mill_config)
            campaigns.append(seg)

        # ── Generate warnings for each campaign ──────────────────────
        for c in campaigns:
            if c.will_exhaust:
                warnings.append({
                    'type':      'EXHAUSTION',
                    'severity':  'CRITICAL',
                    'mill':      mill_label,
                    'roll_type': c.roll_type,
                    'message':   (
                        f"{mill_label} {c.roll_type} roll will EXHAUST "
                        f"during {c.exhausts_at or 'plan'} section. "
                        f"Roll life: {c.mt_life:.0f} MT | "
                        f"Planned load: {c.end_mt_used:.1f} MT "
                        f"({c.pct_consumed:.0f}% of life)"
                    ),
                    'exhausts_at':  c.exhausts_at,
                    'mt_overrun':   round(c.end_mt_used - c.mt_life, 1),
                    'recommendation': (
                        f"Insert a roll change after "
                        f"{c.mt_life - c.start_mt_used:.0f} MT "
                        f"({_mt_to_coils(c, c.mt_life - c.start_mt_used)} coils approx)"
                    ),
                })
            elif c.pct_consumed >= 80:
                warnings.append({
                    'type':      'HIGH_USAGE',
                    'severity':  'WARNING',
                    'mill':      mill_label,
                    'roll_type': c.roll_type,
                    'message':   (
                        f"{mill_label} {c.roll_type} roll will reach "
                        f"{c.pct_consumed:.0f}% of life after today's plan. "
                        f"Consider scheduling roll dress before next campaign."
                    ),
                    'pct_after': round(c.pct_consumed, 1),
                    'mt_remaining': round(c.mt_life - c.end_mt_used, 1),
                    'recommendation': 'Schedule roll dressing before next shift',
                })
            elif c.pct_consumed >= 60:
                warnings.append({
                    'type':      'MONITOR',
                    'severity':  'INFO',
                    'mill':      mill_label,
                    'roll_type': c.roll_type,
                    'message':   (
                        f"{mill_label} {c.roll_type} roll at "
                        f"{c.pct_consumed:.0f}% life after today."
                    ),
                    'pct_after': round(c.pct_consumed, 1),
                    'mt_remaining': round(c.mt_life - c.end_mt_used, 1),
                    'recommendation': 'Monitor — no immediate action needed',
                })

        return campaigns, warnings

    crm04_campaigns, crm04_warnings = _simulate(crm04_secs, crm04_state, 'CRM04')
    crm06_campaigns, crm06_warnings = _simulate(crm06_secs, crm06_state, 'CRM06')

    all_warnings = crm04_warnings + crm06_warnings
    critical     = [w for w in all_warnings if w.get('severity') == 'CRITICAL']
    warning_lvl  = [w for w in all_warnings if w.get('severity') == 'WARNING']

    return {
        'crm04_campaigns': [_seg_to_dict(c) for c in crm04_campaigns],
        'crm06_campaigns': [_seg_to_dict(c) for c in crm06_campaigns],
        'crm04_warnings':  crm04_warnings,
        'crm06_warnings':  crm06_warnings,
        'all_warnings':    all_warnings,
        'summary': {
            'critical_count':     len(critical),
            'warning_count':      len(warning_lvl),
            'total_mt_crm04':     sum(c.total_mt for c in crm04_campaigns),
            'total_mt_crm06':     sum(c.total_mt for c in crm06_campaigns),
            'crm04_roll_changes': sum(1 for w in crm04_warnings
                                      if w['type'] == 'ROLL_CHANGE'),
            'crm06_roll_changes': sum(1 for w in crm06_warnings
                                      if w['type'] == 'ROLL_CHANGE'),
            'status': 'CRITICAL' if critical else
                      'WARNING'  if warning_lvl else 'OK',
        }
    }


def _mark_exhaustion(seg: CampaignSegment, mill_secs, mill_config):
    """Find exactly which section the roll exhausts in."""
    remaining = seg.mt_life - seg.start_mt_used
    for s in mill_secs:
        if s['section_key'] not in seg.sections:
            continue
        s_mt = float(s['coils_df']['Input Coil Weight'].sum())
        if remaining <= 0:
            seg.exhausts_at       = s['section_key']
            seg.exhausts_after_mt = 0.0
            return
        if s_mt >= remaining:
            seg.exhausts_at       = s['section_key']
            seg.exhausts_after_mt = remaining
            return
        remaining -= s_mt


def _mt_to_coils(seg: CampaignSegment, mt_target: float) -> int:
    """Estimate coil count to reach mt_target MT in a segment."""
    if seg.total_mt <= 0 or seg.coil_count <= 0:
        return 0
    avg_wt = seg.total_mt / seg.coil_count
    return max(1, int(mt_target / avg_wt))


def _seg_to_dict(seg: CampaignSegment) -> dict:
    return {
        'mill':           seg.mill,
        'roll_type':      seg.roll_type,
        'sections':       seg.sections,
        'total_mt':       round(seg.total_mt, 1),
        'coil_count':     seg.coil_count,
        'start_mt_used':  round(seg.start_mt_used, 1),
        'end_mt_used':    round(seg.end_mt_used, 1),
        'mt_life':        round(seg.mt_life, 1),
        'pct_consumed':   round(seg.pct_consumed, 1),
        'will_exhaust':   seg.will_exhaust,
        'exhausts_at':    seg.exhausts_at,
        'status':         'CRITICAL' if seg.will_exhaust else
                          'WARNING'  if seg.pct_consumed >= 80 else
                          'MONITOR'  if seg.pct_consumed >= 60 else 'OK',
    }
