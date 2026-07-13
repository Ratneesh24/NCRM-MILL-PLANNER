"""
crm/campaign.py — Mill Sequencing & Roll Campaign Optimiser  (Guideline §6)
===========================================================================
Groups similar coils to minimise changeovers:
  • roll-type campaigns  (45 min per roll change)
  • within-campaign coil sequencing by gauge / width / grade progression
  • capacity-aware selection (defers what won't fit the shift)
  • Priority order vs Alternate (min-changeover) order — with comparison
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import pandas as pd

from . import config as C
from .scoring import SectionScore


# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class Campaign:
    roll_type:  str
    mill:       str
    sections:   List[str]        = field(default_factory=list)
    coils:      List[dict]       = field(default_factory=list)
    total_mt:   float            = 0.0
    n_coils:    int              = 0
    change_from: str             = ""
    needs_change: bool           = False
    roll_life:  int              = 100
    mt_start:   float            = 0.0     # MT already on roll at campaign start
    mt_end:     float            = 0.0
    over_life:  bool             = False
    consumer:   str              = ""
    warnings:   List[str]        = field(default_factory=list)

    @property
    def life_pct(self) -> int:
        return int(min(self.mt_end / self.roll_life * 100, 100)) \
               if self.roll_life else 0


@dataclass
class MillPlan:
    mill:          str
    plan_type:     str            # "priority" | "alternate"
    campaigns:     List[Campaign] = field(default_factory=list)
    deferred:      List[str]      = field(default_factory=list)
    n_changes:     int            = 0
    downtime_min:  int            = 0
    planned_mt:    float          = 0.0
    capacity_mt:   float          = 0.0
    n_coils:       int            = 0

    @property
    def utilisation(self) -> float:
        return round(self.planned_mt / self.capacity_mt * 100, 1) \
               if self.capacity_mt else 0.0


# ══════════════════════════════════════════════════════════════════════════════
# COIL SEQUENCING WITHIN A CAMPAIGN  (Guideline §6)
# ══════════════════════════════════════════════════════════════════════════════
def sequence_coils(df: pd.DataFrame, section_key: str) -> List[dict]:
    """
    Order coils to minimise in-campaign setup disruption:
      1. Width  descending  — protects roll edges (never step up)
      2. Thick  descending  — steady hydraulic load progression
      3. Grade grouped      — same quality together
      4. Age    descending  — oldest first within identical setup
    """
    d = df.copy()
    for col, default in (("width", 0), ("thick", 0), ("coil_age", 0),
                         ("rt", 0), ("mt", 0)):
        if col not in d.columns:
            d[col] = default
    d = d.sort_values(
        by=["width", "thick", "quality", "coil_age"],
        ascending=[False, False, True, False])

    out = []
    for _, r in d.iterrows():
        out.append({
            "coil":     str(r.get("coil", r.get("Coil Number", ""))),
            "mt":       round(float(r.get("mt", 0)), 3),
            "width":    float(r.get("width", 0)),
            "thick":    float(r.get("thick", 0)),
            "rt":       float(r.get("rt", 0)),
            "quality":  str(r.get("quality", "")),
            "customer": str(r.get("customer", ""))[:20],
            "age":      float(r.get("coil_age", 0)),
            "qual_risk":float(r.get("qual_risk", 0)),
            "section":  section_key,
        })
    return out


def _campaign_setup_cost(coils: List[dict]) -> Dict:
    """Count in-campaign setup disruptions after sequencing."""
    ws = wd = td = gd = 0
    for i in range(len(coils) - 1):
        a, b = coils[i], coils[i + 1]
        if b["width"] > a["width"] + 2:  ws += 1     # width step-UP (bad)
        if abs(a["width"] - b["width"]) > 2:  wd += 1
        if abs(a["thick"] - b["thick"]) > 0.10: td += 1
        if a["quality"] != b["quality"]:  gd += 1
    return {"width_stepups": ws, "width_changes": wd,
            "thick_changes": td, "grade_changes": gd}


# ══════════════════════════════════════════════════════════════════════════════
# CAPACITY
# ══════════════════════════════════════════════════════════════════════════════
def blended_capacity(mill: str, sections: List[SectionScore]) -> float:
    """
    Shift capacity depends on the MIX of rolling types selected.
    Weighted by the MT share of each type.
    """
    cap = C.SHIFT_CAPACITY.get(mill, {})
    tot = {"first_rolling": 0.0, "re_rolling": 0.0, "finishing": 0.0}
    for s in sections:
        if s.mill == mill:
            tot[s.sec_type] += s.total_mt
    grand = sum(tot.values())
    if grand <= 0:
        return float(max(cap.values()) if cap else 60)
    return round(sum(tot[k] / grand * cap.get(k, 0) for k in tot), 1)


# ══════════════════════════════════════════════════════════════════════════════
# BUILD CAMPAIGNS
# ══════════════════════════════════════════════════════════════════════════════
def build_mill_plan(
    ordered:       List[SectionScore],
    mill:          str,
    current_roll:  str,
    mt_on_roll:    float,
    capacity_mt:   float,
    plan_type:     str = "priority",
) -> MillPlan:
    """Walk the ordered sections, grouping into roll campaigns."""
    plan = MillPlan(mill=mill, plan_type=plan_type, capacity_mt=capacity_mt)
    roll      = current_roll
    used      = mt_on_roll
    remaining = capacity_mt

    for s in [x for x in ordered if x.mill == mill]:
        if remaining <= 0.5:
            plan.deferred.append(f"{s.section_key} ({s.total_mt:.0f} MT)")
            continue

        needed = s.roll_type
        change = (needed != roll)

        # Provisionally cost the changeover, but only COMMIT it if the
        # section actually gets rolled (otherwise we'd charge 45 min for
        # a section we end up deferring).
        prov_remaining = remaining
        prov_used      = used
        if change:
            prov_remaining -= capacity_mt * (C.ROLL_CHANGE_MIN / C.SHIFT_MINUTES)
            prov_used       = 0.0            # fresh roll fitted
            if prov_remaining <= 0.5:
                plan.deferred.append(f"{s.section_key} ({s.total_mt:.0f} MT)")
                continue

        life    = C.roll_life(needed, s.section_key, s.consumer)
        allowed = min(s.total_mt, prov_remaining, max(life - prov_used, 0))
        seq     = sequence_coils(s.coils_df, s.section_key)

        take, run = [], 0.0
        for c in seq:
            if run + c["mt"] > allowed + 0.05:
                break
            take.append(c); run += c["mt"]

        if not take:
            plan.deferred.append(f"{s.section_key} ({s.total_mt:.0f} MT)")
            continue        # roll NOT changed — nothing was rolled

        # Commit the changeover now that we know the section runs
        if change:
            plan.n_changes    += 1
            plan.downtime_min += C.ROLL_CHANGE_MIN
        remaining = prov_remaining
        used      = prov_used

        left = s.total_mt - run
        if left > 0.5:
            plan.deferred.append(f"{s.section_key} (+{left:.0f} MT carry-over)")

        # append to open campaign of same roll, else start a new one
        if plan.campaigns and plan.campaigns[-1].roll_type == needed \
                and not change:
            cp = plan.campaigns[-1]
            cp.coils.extend(take)
            cp.sections.append(s.section_key)
            cp.total_mt = round(cp.total_mt + run, 2)
            cp.n_coils += len(take)
            cp.mt_end   = round(used + run, 1)
            cp.over_life = cp.mt_end > cp.roll_life
        else:
            cp = Campaign(
                roll_type=needed, mill=mill, sections=[s.section_key],
                coils=take, total_mt=round(run, 2), n_coils=len(take),
                change_from=roll if change else "", needs_change=change,
                roll_life=life, mt_start=round(used, 1),
                mt_end=round(used + run, 1), consumer=s.consumer,
            )
            cp.over_life = cp.mt_end > life
            plan.campaigns.append(cp)

        if cp.over_life:
            cp.warnings.append(
                f"⚠️ Roll life {life} MT will be exceeded "
                f"({cp.mt_end:.0f} MT) — plan a dressing")

        # setup-quality warnings inside the campaign
        cost = _campaign_setup_cost(cp.coils)
        if cost["width_stepups"]:
            cp.warnings.append(
                f"⚠️ {cost['width_stepups']} width step-up(s) — roll edge risk")

        used       = cp.mt_end
        roll       = needed
        remaining -= run

    plan.planned_mt = round(sum(c.total_mt for c in plan.campaigns), 1)
    plan.n_coils    = sum(c.n_coils for c in plan.campaigns)
    return plan


# ══════════════════════════════════════════════════════════════════════════════
# ALTERNATE ORDER — minimum changeovers
# ══════════════════════════════════════════════════════════════════════════════
def alternate_order(sections: List[SectionScore], mill: str,
                    current_roll: str) -> List[SectionScore]:
    """
    Group by roll type. Mounted roll's group first (zero changeover).
    Remaining groups ordered by their most urgent section.
    Priority order preserved *inside* each group.
    """
    ms = sorted([s for s in sections if s.mill == mill],
                key=lambda x: x.rank)
    groups: Dict[str, List[SectionScore]] = {}
    for s in ms:
        groups.setdefault(s.roll_type, []).append(s)

    order = [current_roll] if current_roll in groups else []
    order += sorted([r for r in groups if r != current_roll],
                    key=lambda r: -max(s.score for s in groups[r]))
    return [s for r in order for s in groups[r]]


def compare_plans(
    scored:        List[SectionScore],
    current_rolls: Dict[str, str],
    mt_on_rolls:   Dict[str, float],
) -> Dict:
    """
    Build BOTH plans for both mills and compare.
    Returns {mill: {"priority": MillPlan, "alternate": MillPlan,
                    "savings_min": int, "recommend": str}}
    """
    out: Dict[str, Dict] = {}
    for mill in ("CRM04", "CRM06"):
        ms = [s for s in scored if s.mill == mill]
        if not ms:
            continue
        cap  = blended_capacity(mill, scored)
        roll = current_rolls.get(mill, "Light Matt")
        used = mt_on_rolls.get(mill, 0.0)

        prio = build_mill_plan(sorted(ms, key=lambda x: x.rank),
                               mill, roll, used, cap, "priority")
        alt  = build_mill_plan(alternate_order(scored, mill, roll),
                               mill, roll, used, cap, "alternate")

        savings = prio.downtime_min - alt.downtime_min
        # Recommend alternate only if it saves time AND doesn't delay a
        # CRITICAL consumer
        recommend = "priority"
        if savings > 0:
            prio_first = prio.campaigns[0].consumer if prio.campaigns else ""
            alt_first  = alt.campaigns[0].consumer  if alt.campaigns  else ""
            recommend  = "priority" if prio_first != alt_first else "alternate"
            if savings >= 45 and prio_first == alt_first:
                recommend = "alternate"

        out[mill] = {
            "priority": prio, "alternate": alt,
            "savings_min": savings, "recommend": recommend,
            "capacity": cap,
        }
    return out
