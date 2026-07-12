"""
priority_advisor.py — Shift Planning & Priority Engine v4.0
============================================================
Tata Steel CRM Sahibabad — Narrow Complex

Answers at shift start:
  1. Which consumer is at risk? (H&T / Tube / OEM)
  2. Which sections to run on which mill today?
  3. In what roll sequence to minimise 45-min roll changes?
  4. Does the plan cover all three consumers?

Consumer daily ask (confirmed):
  H&T Line   :  35 MT/day  — FURNACE buffer, 48-72h cycle
  Tube Plant  : 210 MT/day — via CRS
  OEM         :  50 MT/day — via CRS
  All equal priority — tube just has a larger share of demand

Bottlenecks:
  H&T Line  — direct from mill, no CRS
  CRS       — all non-H&T material passes here

Capacity per shift (8 hours):
  1st Rolling (FR)  : CRM-06 only  → 120 MT/shift
  Re-Rolling  (RR)  : CRM-04 80 MT · CRM-06 95 MT
  Finishing   (FIN) : CRM-04 50 MT · CRM-06 60 MT
  (ROLLING = same as Re-Rolling for capacity purposes)

Roll life (MT before dress/change):
  Light Matt — 1st Rolling H&T material : 200 MT
  Light Matt — 1st Rolling other        : 100 MT
  Light Matt — Re-Rolling / ROLLING     : 100 MT
  Bright     — all Finishing sections   : 100 MT
  Super Bright                          : 300 MT
  Chrome Plated                         : 300 MT
  Heavy Matt                            : 200 MT
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import pandas as pd

# ══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════════════

CONFIG = {
    # ── Consumer daily ask (MT/day) ──────────────────────────────────────────
    "consumers": {
        "H&T Line":   {"daily_mt": 35.0,  "buffer_stage": ["FURNACE"],
                       "warn_days": 1.5,  "target_days": 2.5},
        "Tube Plant": {"daily_mt": 210.0, "buffer_stage": ["C R SLITTER"],
                       "warn_days": 1.0,  "target_days": 1.5},
        "OEM":        {"daily_mt": 50.0,  "buffer_stage": ["C R SLITTER"],
                       "warn_days": 1.0,  "target_days": 1.5},
    },

    # ── Section → consumer ───────────────────────────────────────────────────
    "section_to_consumer": {
        "HT_FINISH":              "H&T Line",
        "TUBE_FH":                "Tube Plant",
        "CRCA_FINISH":            "OEM",
        "CRCA_FINISH_CRM06":      "OEM",
        "SKIN_PASS_SUPER_BRIGHT": "OEM",
        "SKIN_PASS_CHROME":       "OEM",
        "SKIN_PASS_HEAVY_MATT":   "OEM",
        "ROLLING_BRIGHT":         "OEM",
        "RE_ROLLING":             "Tube Plant",  # feeds CRS → Tube pipeline
        "FIRST_ROLLING":          "Tube Plant",  # feeds CRS → Tube pipeline
        "ROLLING":                "OEM",
    },

    # ── Section → roll type ──────────────────────────────────────────────────
    "section_to_roll": {
        "FIRST_ROLLING":          "Light Matt",
        "RE_ROLLING":             "Light Matt",
        "ROLLING":                "Light Matt",
        "HT_FINISH":              "Bright",
        "TUBE_FH":                "Bright",
        "CRCA_FINISH":            "Bright",
        "CRCA_FINISH_CRM06":      "Bright",
        "SKIN_PASS_SUPER_BRIGHT": "Super Bright",
        "SKIN_PASS_CHROME":       "Chrome Plated",
        "ROLLING_BRIGHT":         "Chrome Plated",
        "SKIN_PASS_HEAVY_MATT":   "Heavy Matt",
    },

    # ── Roll life (MT) ────────────────────────────────────────────────────────
    # Key format: "roll_type|section_type" where section_type ∈
    #             {first_rolling_ht, first_rolling_other, re_rolling, finishing}
    "roll_life": {
        "Light Matt|first_rolling_ht":    200,
        "Light Matt|first_rolling_other": 100,
        "Light Matt|re_rolling":          100,
        "Light Matt|finishing":           100,   # fallback
        "Bright|finishing":               100,
        "Super Bright|finishing":         300,
        "Chrome Plated|finishing":        300,
        "Heavy Matt|finishing":           200,
    },

    # ── Mill capacity per shift (MT) ─────────────────────────────────────────
    "shift_capacity": {
        "CRM04": {"first_rolling": 0,    "re_rolling": 80,
                  "finishing": 50, "rolling": 80},
        "CRM06": {"first_rolling": 120,  "re_rolling": 95,
                  "finishing": 60, "rolling": 95},
    },

    # ── Sections that go direct (no CRS) ─────────────────────────────────────
    "direct_sections": {"HT_FINISH"},

    # ── Via CRS sections ──────────────────────────────────────────────────────
    "via_crs_sections": {
        "TUBE_FH", "CRCA_FINISH", "CRCA_FINISH_CRM06",
        "SKIN_PASS_SUPER_BRIGHT", "SKIN_PASS_CHROME",
        "SKIN_PASS_HEAVY_MATT", "ROLLING_BRIGHT",
    },

    # ── Feeds annealing pipeline ──────────────────────────────────────────────
    "feeds_anneal": {"RE_ROLLING", "FIRST_ROLLING", "ROLLING"},

    # ── Roll change time (minutes) ────────────────────────────────────────────
    "roll_change_min": 45,

    # ── CRS change cost weights ───────────────────────────────────────────────
    "crs_cost": {"width_per_mm": 0.5, "thick_per_0.1mm": 2.0,
                 "product_change": 10.0, "customer_change": 1.0},
}

# ── Classify section type for roll life lookup ────────────────────────────────
def _section_type(section_key: str, is_ht_material: bool = False) -> str:
    if section_key == "FIRST_ROLLING":
        return "first_rolling_ht" if is_ht_material else "first_rolling_other"
    if section_key in ("RE_ROLLING", "ROLLING"):
        return "re_rolling"
    return "finishing"


def get_roll_life(roll_type: str, section_key: str,
                  is_ht_material: bool = False) -> int:
    st = _section_type(section_key, is_ht_material)
    key = f"{roll_type}|{st}"
    return CONFIG["roll_life"].get(key,
           CONFIG["roll_life"].get(f"{roll_type}|finishing", 100))


MODES = {
    "H&T_PRIORITY":   "H&T Priority — ensure H&T Line is fed first",
    "TUBE_PRIORITY":  "Tube Priority — maximise Tube Plant feed",
    "OEM_PRIORITY":   "OEM Priority — maximise OEM/CRS dispatch",
}

ROLL_TYPES = ["Light Matt", "Bright", "Super Bright",
              "Chrome Plated", "Heavy Matt"]

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — CONSUMER COVERAGE
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class ConsumerCoverage:
    name:              str
    daily_mt:          float
    buffer_mt:         float = 0.0
    buffer_coils:      int   = 0
    coverage_days:     float = 0.0
    status:            str   = "OK"   # OK / WATCH / WARNING / CRITICAL
    shortfall_mt:      float = 0.0    # MT short of target
    target_days:       float = 2.5
    warn_days:         float = 1.5


def build_coverage(wip_df: pd.DataFrame,
                   overrides: Optional[Dict] = None) -> Dict[str, ConsumerCoverage]:
    cfg = CONFIG["consumers"]
    eff = {k: dict(v) for k, v in cfg.items()}
    if overrides:
        for k, v in overrides.items():
            if k in eff: eff[k]["daily_mt"] = v

    wip = wip_df.copy()
    wip.columns = wip.columns.str.strip()
    wip["Input Coil Weight"] = pd.to_numeric(
        wip.get("Input Coil Weight", 0), errors="coerce").fillna(0)
    wip.loc[wip["Input Coil Weight"] > 100, "Input Coil Weight"] /= 1000.0

    result = {}
    for cname, ccfg in eff.items():
        stages   = ccfg["buffer_stage"]
        rate     = ccfg["daily_mt"]
        warn     = ccfg["warn_days"]
        target   = ccfg["target_days"]
        buf      = wip[wip["Current Stage"].isin(stages)]

        # For CRS: H&T is separate; Tube vs OEM split by quality
        if cname == "Tube Plant":
            buf = buf[buf["Actual Quality"].str.contains(
                "TATFHC", na=False)] if "Actual Quality" in buf.columns else buf
        elif cname == "OEM":
            buf = buf[~buf["Actual Quality"].str.contains(
                "TATFHC", na=False)] if "Actual Quality" in buf.columns else buf

        buf_mt  = round(float(buf["Input Coil Weight"].sum()), 1)
        days    = round(buf_mt / rate, 2) if rate else 99
        short   = max(0, round((target * rate) - buf_mt, 1))

        if   days <= warn * 0.5:  status = "CRITICAL"
        elif days <= warn:        status = "WARNING"
        elif days <= target:      status = "WATCH"
        else:                     status = "OK"

        result[cname] = ConsumerCoverage(
            name=cname, daily_mt=rate, buffer_mt=buf_mt,
            buffer_coils=len(buf), coverage_days=days,
            status=status, shortfall_mt=short,
            target_days=target, warn_days=warn)
    return result


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — SHIFT PLAN BUILDER
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class ShiftSection:
    """One section selected for today's shift on one mill."""
    section_key:   str
    mill:          str
    label:         str
    roll_type:     str
    roll_life_mt:  int
    n_coils:       int
    total_mt:      float
    consumer:      str
    priority_rank: int   = 0
    coils_df:      object = None  # the actual DataFrame slice


def _section_capacity_type(sk: str) -> str:
    if sk == "FIRST_ROLLING":          return "first_rolling"
    if sk in ("RE_ROLLING", "ROLLING"): return "re_rolling"
    return "finishing"


def score_sections(sections: List[Dict],
                   coverage: Dict[str, ConsumerCoverage],
                   mode: str,
                   current_rolls: Dict[str, str]) -> List[ShiftSection]:
    """
    Score and rank sections per mill given mode and consumer coverage.

    Priority rules:
      1. Always protect H&T (≤1.5d cover → H&T sections to rank 1)
      2. Always protect CRS/Tube (≤1.0d → Tube sections to rank 1)
      3. Always protect OEM (≤1.0d → OEM sections next)
      4. Within same consumer group → oldest coils first
      5. If H&T covered (>1.5d) in H&T_PRIORITY mode → auto drop to Tube
      6. No-change roll sections get +10 score bonus (continuity)
    """
    CONSUMER_BASE = {"H&T Line": 100, "Tube Plant": 80, "OEM": 60}
    MODE_BOOST    = {
        "H&T_PRIORITY":  {"H&T Line": 40, "Tube Plant": 0, "OEM": 0},
        "TUBE_PRIORITY": {"H&T Line": 0,  "Tube Plant": 40, "OEM": 0},
        "OEM_PRIORITY":  {"H&T Line": 0,  "Tube Plant": 0,  "OEM": 40},
    }
    boosts = MODE_BOOST.get(mode, {})

    # Auto-drop H&T priority if covered
    ht_cov = coverage.get("H&T Line")
    if (mode == "H&T_PRIORITY" and ht_cov and
            ht_cov.coverage_days > ht_cov.warn_days):
        boosts = MODE_BOOST["TUBE_PRIORITY"]

    sec_map  = CONFIG["section_to_consumer"]
    roll_map = CONFIG["section_to_roll"]
    scored   = []

    for s in sections:
        sk       = s["section_key"]
        df       = s["coils_df"]
        consumer = sec_map.get(sk, "OEM")
        roll_t   = roll_map.get(sk, "Light Matt")
        is_ht_mat = (sk == "FIRST_ROLLING" and consumer == "H&T Line")
        life     = get_roll_life(roll_t, sk, is_ht_mat)
        mt       = round(float(df["Input Coil Weight"].sum()), 2)
        max_age  = float(df["Coil Age(# Days)"].fillna(0).max())

        # Base score
        cov_st   = coverage.get(consumer)
        cov_score = {"CRITICAL": 40, "WARNING": 30,
                     "WATCH": 15, "OK": 0}.get(
            cov_st.status if cov_st else "OK", 0)
        age_score = min(max_age / 21 * 30, 30)
        # No-change bonus
        cur_roll  = current_rolls.get(s["mill"], "")
        cont_bonus = 10 if roll_t == cur_roll else 0

        total = (CONSUMER_BASE.get(consumer, 50) +
                 boosts.get(consumer, 0) +
                 cov_score + age_score + cont_bonus)

        scored.append(ShiftSection(
            section_key=sk, mill=s["mill"], label=s["label"],
            roll_type=roll_t, roll_life_mt=life,
            n_coils=len(df), total_mt=mt,
            consumer=consumer, coils_df=df,
        ) )
        scored[-1]._score = total  # type: ignore[attr-defined]

    # Rank per mill
    for mill in ("CRM04", "CRM06"):
        mill_secs = [s for s in scored if s.mill == mill]
        mill_secs.sort(key=lambda x: -x._score)  # type: ignore[attr-defined]
        for rank, s in enumerate(mill_secs, 1):
            s.priority_rank = rank

    return scored


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — ROLL CAMPAIGN BUILDER
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class RollCampaign:
    roll_type:     str
    sections:      List[str]
    coils:         List[dict]
    total_mt:      float
    n_coils:       int
    preceded_by_change: bool  = False
    change_from:   str        = ""
    mt_used_start: float      = 0.0   # MT already on this roll at shift start
    mt_used_end:   float      = 0.0   # estimated MT used by end of campaign
    roll_life:     int        = 100
    exceeds_life:  bool       = False


def build_roll_campaigns(
    ordered_sections: List[ShiftSection],
    current_roll: str,
    mt_already_rolled: float,
    mill: str,
    shift_capacity_mt: float,
) -> Tuple[List[RollCampaign], List[str], int, int]:
    """
    Group selected sections into roll campaigns.
    Returns (campaigns, deferred_sections, n_changes, total_downtime_min)
    """
    roll_map = CONFIG["section_to_roll"]
    change_min = CONFIG["roll_change_min"]

    campaigns: List[RollCampaign] = []
    deferred:  List[str]          = []
    current    = current_roll
    mt_on_roll = mt_already_rolled
    remaining_cap = shift_capacity_mt
    n_changes  = 0

    for sec in ordered_sections:
        if sec.mill != mill:
            continue
        needed = roll_map.get(sec.section_key, "Light Matt")
        sec_mt = sec.total_mt

        if remaining_cap <= 0:
            deferred.append(sec.section_key)
            continue

        # Roll change needed?
        change_needed = (needed != current)
        if change_needed:
            # Deduct 45-min cost from remaining capacity
            # 45 min as fraction of 480-min shift × capacity
            change_cap_cost = shift_capacity_mt * (change_min / 480)
            remaining_cap  -= change_cap_cost
            n_changes      += 1
            mt_on_roll      = 0.0   # fresh roll
            current         = needed

        rollable  = min(sec_mt, remaining_cap)
        deferred_mt = sec_mt - rollable

        # Build coil list
        coil_list = []
        running   = 0.0
        for _, row in sec.coils_df.iterrows():
            cw = float(row.get("Input Coil Weight", 0) or 0)
            if running + cw > rollable + 0.05:
                break
            coil_list.append({
                "coil":     str(row.get("Coil Number", "")),
                "mt":       round(cw, 3),
                "width":    float(row.get("Actual Width", 0) or 0),
                "thick":    float(row.get("Actual Thick", 0) or 0),
                "rt":       float(row.get("Plan Rolling Thick 1", 0) or 0),
                "customer": str(row.get("Customer Desc", ""))[:18],
                "age":      float(row.get("Coil Age(# Days)", 0) or 0),
                "remark":   str(row.get("Planning Remark", ""))[:20],
                "section":  sec.section_key,
            })
            running += cw

        life = get_roll_life(needed, sec.section_key)
        mt_end = mt_on_roll + running
        exceeds = mt_end > life

        # Append to existing campaign or create new
        if (campaigns and campaigns[-1].roll_type == needed
                and not change_needed):
            c = campaigns[-1]
            c.coils.extend(coil_list)
            c.sections.append(sec.section_key)
            c.total_mt  = round(c.total_mt + running, 2)
            c.n_coils  += len(coil_list)
            c.mt_used_end = mt_end
            c.exceeds_life = c.exceeds_life or exceeds
        else:
            campaigns.append(RollCampaign(
                roll_type=needed,
                sections=[sec.section_key],
                coils=coil_list,
                total_mt=round(running, 2),
                n_coils=len(coil_list),
                preceded_by_change=change_needed,
                change_from=current_roll if change_needed else "",
                mt_used_start=mt_on_roll,
                mt_used_end=round(mt_end, 1),
                roll_life=life,
                exceeds_life=exceeds,
            ))

        mt_on_roll    = mt_end
        remaining_cap -= rollable
        if deferred_mt > 0.05:
            deferred.append(f"{sec.section_key} ({deferred_mt:.0f}MT deferred)")

    total_downtime = n_changes * change_min
    return campaigns, deferred, n_changes, total_downtime


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — ALTERNATE PLAN (MIN ROLL CHANGES)
# ══════════════════════════════════════════════════════════════════════════════

def build_alternate_order(
    sections: List[ShiftSection],
    current_roll: str,
    mill: str,
) -> List[ShiftSection]:
    """
    Group sections by roll type. Current roll first (zero cost).
    Within each group: keep priority order.
    Each subsequent group = 1 roll change.
    """
    roll_map = CONFIG["section_to_roll"]
    mill_secs = [s for s in sections if s.mill == mill]
    groups: Dict[str, List[ShiftSection]] = {}
    for s in mill_secs:
        rt = roll_map.get(s.section_key, "Light Matt")
        groups.setdefault(rt, []).append(s)

    order = []
    if current_roll in groups:
        order.append(current_roll)
    others = sorted(
        [rt for rt in groups if rt != current_roll],
        key=lambda rt: -max(s._score for s in groups[rt]))  # type: ignore
    order += others
    return [s for rt in order for s in groups[rt]]


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — CRS SEQUENCE OPTIMISER
# ══════════════════════════════════════════════════════════════════════════════

def _crs_cost(a: dict, b: dict) -> float:
    cc   = CONFIG["crs_cost"]
    cost = (abs(a["width"] - b["width"]) * cc["width_per_mm"] +
            abs(a["thick"] - b["thick"]) / 0.1 * cc["thick_per_0.1mm"])
    if a.get("product") != b.get("product"):
        cost += cc["product_change"]
    age_b = b.get("age", 0)
    cost  *= (0.70 if age_b > 21 else 0.85 if age_b > 14 else 1.0)
    return round(cost, 2)


def optimise_crs(sections: List[ShiftSection],
                 rolled_coils: Optional[set] = None) -> Dict:
    via = CONFIG["via_crs_sections"]
    rolled = rolled_coils or set()
    coils = []
    for s in sections:
        if s.section_key not in via:
            continue
        for c in (s.coils_df.iterrows() if hasattr(s.coils_df, 'iterrows')
                  else []):
            _, row = c
            cn = str(row.get("Coil Number", ""))
            if cn in rolled:
                continue
            coils.append({
                "coil_number": cn,
                "width":   float(row.get("Actual Width", 0)),
                "thick":   float(row.get("Plan Rolling Thick 1", 0)),
                "weight":  float(row.get("Input Coil Weight", 0)),
                "product": str(row.get("Product Code", "")),
                "customer":str(row.get("Customer Desc", ""))[:20],
                "section": s.section_key,
                "age":     float(row.get("Coil Age(# Days)", 0) or 0),
            })

    if not coils:
        return {"error": "No CRS coils in plan"}

    orig = sum(_crs_cost(coils[i], coils[i+1]) for i in range(len(coils)-1))

    def greedy(start):
        rem = coils[:]
        seq = [rem.pop(start)]
        while rem:
            last = seq[-1]
            nxt  = min(rem, key=lambda c: _crs_cost(last, c))
            seq.append(nxt); rem.remove(nxt)
        return seq

    best, best_cost = coils[:], orig
    for i in range(len(coils)):
        s = greedy(i)
        c = sum(_crs_cost(s[j], s[j+1]) for j in range(len(s)-1))
        if c < best_cost:
            best, best_cost = s, c

    improved = True
    while improved:
        improved = False
        for i in range(1, len(best)-1):
            for j in range(i+1, len(best)):
                ns = best[:i] + best[i:j+1][::-1] + best[j+1:]
                nc = sum(_crs_cost(ns[k], ns[k+1]) for k in range(len(ns)-1))
                if nc < best_cost - 0.01:
                    best, best_cost, improved = ns, nc, True

    def n_changes(seq):
        return sum(1 for i in range(len(seq)-1)
                   if (abs(seq[i]["width"]-seq[i+1]["width"]) > 2 or
                       abs(seq[i]["thick"]-seq[i+1]["thick"]) > 0.05 or
                       seq[i].get("product") != seq[i+1].get("product")))

    oc = n_changes(coils)
    bc = n_changes(best)

    events = []
    for i in range(len(best)-1):
        a, b   = best[i], best[i+1]
        evs    = []
        if abs(a["width"]-b["width"]) > 2:
            evs.append(f"Width {a['width']:.0f}→{b['width']:.0f}mm")
        if abs(a["thick"]-b["thick"]) > 0.05:
            evs.append(f"Thick {a['thick']:.2f}→{b['thick']:.2f}mm")
        if a.get("product") != b.get("product"):
            evs.append(f"Product {a['product']}→{b['product']} ⚠️")
        if evs:
            events.append({"position": i+1, "from": a["coil_number"],
                           "to": b["coil_number"], "changes": evs,
                           "major": a.get("product") != b.get("product")})

    recs = []
    if oc - bc > 0:
        recs.append(f"✅ {oc-bc} fewer changes ({oc}→{bc}) — ~{(oc-bc)*8:.0f} min saved at CRS")
    else:
        recs.append("✅ Sequence already optimal for CRS")
    if any(e["major"] for e in events):
        recs.append("⚠️ Product change(s) unavoidable — schedule at shift start")
    widths = [c["width"] for c in best]
    if any(widths[i] < widths[i+1]-2 for i in range(len(widths)-1)):
        recs.append("⚠️ Width step-up present — check roll edge condition")

    return {
        "optimised": best, "original_changes": oc,
        "optimised_changes": bc, "saved": oc-bc,
        "change_events": events, "recommendations": recs,
        "total_coils": len(best),
        "total_mt": round(sum(c["weight"] for c in best), 2),
    }


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — DEPLETION FORECAST (7-day)
# ══════════════════════════════════════════════════════════════════════════════

def forecast_depletion(wip_df: pd.DataFrame,
                       selected_sections: List[ShiftSection],
                       overrides: Optional[Dict] = None) -> Dict:
    coverage = build_coverage(wip_df, overrides)
    today    = datetime.now().date()
    result   = {}

    # MT flowing to each consumer from today's selected sections
    sec_map = CONFIG["section_to_consumer"]
    plan_feed: Dict[str, float] = {}
    for s in selected_sections:
        c = sec_map.get(s.section_key, "OEM")
        plan_feed[c] = plan_feed.get(c, 0.0) + s.total_mt

    for cname, cov in coverage.items():
        rate   = cov.daily_mt
        buf    = cov.buffer_mt + plan_feed.get(cname, 0.0)
        proj   = []
        level  = buf
        for d in range(8):
            if d > 0: level = max(level - rate, 0.0)
            proj.append({"day": d, "date": str(today + timedelta(days=d)),
                         "buffer_mt": round(level, 1)})
        days_empty = round(buf / rate, 1) if rate else 99
        result[cname] = {
            "buffer_mt": cov.buffer_mt, "plan_feed_mt": plan_feed.get(cname, 0),
            "consumption_rate": rate, "days_to_empty": days_empty,
            "status": cov.status, "projection": proj,
        }
    return result
