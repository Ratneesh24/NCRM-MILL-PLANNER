"""
priority_advisor.py — Simplified Planning Engine v3.0
======================================================
Tata Steel CRM Sahibabad — Narrow Complex

What it does:
  1. Scores sections for CRM-04 / CRM-06 based on consumer demand
  2. Tracks coil-level rolling confirmation (mark coil as rolled)
  3. Forecasts H&T and CRS buffer depletion (the two real bottlenecks)
  4. Optimises CRS coil sequence for minimum setting changes
  5. Suggests roll changes based on what's mounted and what's planned

Consumers (corrected):
  - H&T Line  — direct from Rolling, no CRS step, real bottleneck
  - Tube Plant — via CRS (C09/TATFHC only)
  - OEM        — everything else via CRS (CRCA, LG Bala, etc.)
  - Annealing  — pipeline (RE_ROLLING, FIRST_ROLLING feed annealing)

Planning modes (simplified to what actually matters):
  H&T_FIRST, TUBE_FIRST, OEM_FIRST, MAX_PRODUCTION,
  DISPATCH_RECOVERY, PIPELINE_PROTECTION
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import pandas as pd


# ══════════════════════════════════════════════════════════════════════════════
# CONFIG — all tunable numbers in one place
# ══════════════════════════════════════════════════════════════════════════════

CONFIG = {
    # Daily requirement per consumer (MT/day) — calibrated from Outcome Logger
    "consumers": {
        "H&T Line":    {"daily_mt": 45.0,  "priority": 10},
        "Tube Plant":  {"daily_mt": 80.0,  "priority": 9},
        "OEM":         {"daily_mt": 110.0, "priority": 7},   # CRS-bound, non-Tube
        "Annealing":   {"daily_mt": 130.0, "priority": 5},
    },

    # Which section feeds which consumer
    "section_to_consumer": {
        "TUBE_FH":                "Tube Plant",   # C09/TATFHC via CRS
        "HT_FINISH":              "H&T Line",     # B28/B29 direct — NO CRS
        "CRCA_FINISH":            "OEM",          # C09 CRCA via CRS
        "CRCA_FINISH_CRM06":      "OEM",          # LG Bala via CRS
        "SKIN_PASS_SUPER_BRIGHT": "OEM",          # via CRS
        "SKIN_PASS_CHROME":       "OEM",          # via CRS
        "SKIN_PASS_HEAVY_MATT":   "OEM",          # via CRS
        "RE_ROLLING":             "Annealing",    # 72h pipeline
        "FIRST_ROLLING":          "Annealing",    # 72h pipeline
        "ROLLING":                "Annealing",    # 72h pipeline
        "ROLLING_BRIGHT":         "OEM",          # via Chrome SPM then CRS
    },

    # WIP stages that count as live buffer for each consumer
    "consumer_buffer_stages": {
        "H&T Line":   ["FURNACE"],
        "Tube Plant": ["C R SLITTER"],     # TATFHC quality only
        "OEM":        ["C R SLITTER"],     # all non-Tube at CRS
        "Annealing":  ["ANB", "ANNEALING"],
    },

    # Stage → days until material is usable by consumer
    "stage_dispatch_days": {
        "09-QA": 0, "INSPECTION TABLE/CTL": 0, "PALLETIZATION": 0, "PACK": 0,
        "C R SLITTER": 1,
        "FURNACE": 2, "GRINDING": 2, "EDGE ROUNDING": 2, "COLOR TEMPERING": 2,
        "SPM": 1,
        "ANB": 3, "ANNEALING": 4,
        "REWINDING": 2, "HR SLITTER": 1, "PICKLING": 2,
        "ROLLING MILL": 1,
        "DEFAULT": 99,
    },

    # Sections that go directly to consumer (no CRS step)
    "direct_sections": {"HT_FINISH"},

    # Sections that pass through CRS before final consumer
    "via_crs_sections": {
        "TUBE_FH", "CRCA_FINISH", "CRCA_FINISH_CRM06",
        "SKIN_PASS_SUPER_BRIGHT", "SKIN_PASS_CHROME",
        "SKIN_PASS_HEAVY_MATT", "ROLLING_BRIGHT",
    },

    # Sections feeding annealing pipeline (72h return)
    "feeds_anneal_sections": {"RE_ROLLING", "FIRST_ROLLING", "ROLLING", "ROLLING_BRIGHT"},

    # Approx MT/hour per section (for MAX_PRODUCTION mode)
    "section_mt_per_hour": {
        "ROLLING": 18.0, "FIRST_ROLLING": 16.0, "RE_ROLLING": 16.0,
        "TUBE_FH": 15.0, "HT_FINISH": 13.0,
        "CRCA_FINISH": 12.0, "CRCA_FINISH_CRM06": 12.0,
        "SKIN_PASS_SUPER_BRIGHT": 11.0, "SKIN_PASS_CHROME": 10.0,
        "SKIN_PASS_HEAVY_MATT": 9.0, "ROLLING_BRIGHT": 14.0,
        "DEFAULT": 14.0,
    },

    # Age thresholds for urgency scoring
    "age_thresholds": [(21, 100), (14, 80), (7, 55), (3, 30), (0, 10)],

    # Coverage alert levels (days)
    "coverage_alerts": {"critical": 1.0, "warning": 2.0, "watch": 3.5},

    # CRS transition cost weights
    "crs_cost": {
        "width_per_mm": 0.5, "thick_per_0.1mm": 2.0,
        "product_change": 10.0, "customer_change": 1.0,
    },

    # Mode weights — 4 factors: consumer_urgency, age, dispatch_speed, throughput
    # Mode weights — 4 factors: consumer_urgency, age, dispatch_speed, throughput
    # SPEC v3.0: exactly 3 modes. H&T Urgent / Tube Urgent / Balanced.
    "mode_weights": {
        "H&T_URGENT": {
            "consumer_urgency": 0.60, "age": 0.15,
            "dispatch_speed":   0.20, "throughput": 0.05,
            "boost_consumer": "H&T Line",
        },
        "TUBE_URGENT": {
            "consumer_urgency": 0.55, "age": 0.15,
            "dispatch_speed":   0.20, "throughput": 0.10,
            "boost_consumer": "Tube Plant",
        },
        "BALANCED": {
            "consumer_urgency": 0.35, "age": 0.20,
            "dispatch_speed":   0.20, "throughput": 0.25,
            "boost_consumer": None,
        },
    },
}

MODES = {
    "H&T_URGENT":  "H&T Urgent — H&T Line is running short",
    "TUBE_URGENT": "Tube Urgent — Tube Plant is calling for material",
    "BALANCED":    "Balanced — normal day, no specific crisis",
}


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — CONSUMER COVERAGE BOARD
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class ConsumerCoverage:
    name:               str
    daily_requirement:  float
    priority:           int
    buffer_mt:          float = 0.0
    buffer_coils:       int   = 0
    incoming_today_mt:  float = 0.0   # from today's plan
    coverage_today:     float = 0.0   # days of cover
    status:             str   = "OK"  # OK / WATCH / WARNING / CRITICAL
    required_today_mt:  float = 0.0   # MT to roll today for 3-day cover
    days_to_empty:      float = 99.0


def _dispatch_days(stage: str) -> int:
    m = CONFIG["stage_dispatch_days"]
    s = str(stage).strip()
    if s in m: return m[s]
    for k, v in m.items():
        if k in s: return v
    return m["DEFAULT"]


def build_consumer_coverage(
    wip_df: pd.DataFrame,
    sections: List[Dict],
    overrides: Optional[Dict] = None,
) -> Dict[str, ConsumerCoverage]:
    cfg    = CONFIG["consumers"]
    alerts = CONFIG["coverage_alerts"]
    buf_st = CONFIG["consumer_buffer_stages"]

    eff = {k: dict(v) for k, v in cfg.items()}
    if overrides:
        for k, v in overrides.items():
            if k in eff: eff[k]["daily_mt"] = v

    wip = wip_df.copy()
    wip.columns = wip.columns.str.strip()
    wip["Input Coil Weight"] = pd.to_numeric(
        wip.get("Input Coil Weight", 0), errors="coerce").fillna(0)
    wip.loc[wip["Input Coil Weight"] > 100, "Input Coil Weight"] /= 1000.0

    # Today's plan feed per consumer
    plan_feed: Dict[str, float] = {}
    sec_map = CONFIG["section_to_consumer"]
    for s in sections:
        c = sec_map.get(s["section_key"])
        if c:
            plan_feed[c] = plan_feed.get(c, 0.0) + float(
                s["coils_df"]["Input Coil Weight"].sum())

    result = {}
    for consumer, ccfg in eff.items():
        stages   = buf_st.get(consumer, [])
        rate     = ccfg["daily_mt"]
        priority = ccfg["priority"]
        buf      = wip[wip["Current Stage"].isin(stages)]
        buf_mt   = round(float(buf["Input Coil Weight"].sum()), 1)
        inc      = round(plan_feed.get(consumer, 0.0), 1)
        total    = buf_mt + inc
        days     = round(total / rate, 1) if rate else 99
        req      = max(0, round(3 * rate - buf_mt, 1))

        if   days <= alerts["critical"]: status = "CRITICAL"
        elif days <= alerts["warning"]:  status = "WARNING"
        elif days <= alerts["watch"]:    status = "WATCH"
        else:                             status = "OK"

        result[consumer] = ConsumerCoverage(
            name=consumer, daily_requirement=rate, priority=priority,
            buffer_mt=buf_mt, buffer_coils=len(buf),
            incoming_today_mt=inc, coverage_today=round(days, 2),
            status=status, required_today_mt=req, days_to_empty=days)

    return result


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — SECTION SCORING
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class SectionScore:
    section_key:   str
    mill:          str
    label:         str
    n_coils:       int
    total_mt:      float
    avg_age:       float
    max_age:       float
    consumer:      str
    is_direct:     bool
    via_crs:       bool
    feeds_anneal:  bool
    mt_per_hour:   float

    # Scores
    consumer_score:  float = 0.0
    age_score:       float = 0.0
    dispatch_score:  float = 0.0
    throughput_score:float = 0.0
    total_score:     float = 0.0

    rank_crm04:  int = 0
    rank_crm06:  int = 0
    warnings:    List[str] = field(default_factory=list)
    explanation: str = ""


def _age_score(max_age: float) -> float:
    for t, s in CONFIG["age_thresholds"]:
        if max_age >= t: return float(s)
    return 10.0


def _score_section(
    sec: SectionScore,
    mode: str,
    coverage: Dict[str, ConsumerCoverage],
    shift_no: int,
) -> SectionScore:
    weights      = CONFIG["mode_weights"].get(mode, CONFIG["mode_weights"]["BALANCED"])
    boost_cons   = weights.get("boost_consumer")
    anneal_bonus = weights.get("anneal_bonus", 0.0)
    cov          = coverage.get(sec.consumer)

    # Consumer urgency score (0–100)
    if cov:
        base = {"CRITICAL": 100, "WARNING": 80, "WATCH": 55, "OK": 25}.get(cov.status, 25)
        # Extra boost if this is the boosted consumer in this mode
        if boost_cons and sec.consumer == boost_cons:
            base = min(100, base * 1.3)
    else:
        base = 20.0
    sec.consumer_score = round(base, 1)

    # Age score
    sec.age_score = round(_age_score(sec.max_age), 1)

    # Dispatch speed (how quickly does rolling → reach consumer?)
    if sec.is_direct:       sec.dispatch_score = 90.0   # same shift
    elif sec.via_crs:       sec.dispatch_score = 60.0   # next day via CRS
    elif sec.feeds_anneal:  sec.dispatch_score = 30.0 if shift_no <= 2 else 15.0
    else:                   sec.dispatch_score = 30.0

    # Throughput
    max_speed = max(CONFIG["section_mt_per_hour"].values())
    sec.throughput_score = round(sec.mt_per_hour / max_speed * 100, 1)

    # Annealing pipeline bonus (PIPELINE_PROTECTION mode)
    anneal_add = 0.0
    if anneal_bonus > 0 and sec.feeds_anneal:
        anneal_add = anneal_bonus * 100

    sec.total_score = round(
        sec.consumer_score   * weights["consumer_urgency"] +
        sec.age_score        * weights["age"] +
        sec.dispatch_score   * weights["dispatch_speed"] +
        sec.throughput_score * weights["throughput"] +
        anneal_add, 1)

    # Warnings
    if cov and cov.status == "CRITICAL":
        sec.warnings.append(f"🔴 {sec.consumer} CRITICAL — {cov.coverage_today:.1f}d cover")
    elif cov and cov.status == "WARNING":
        sec.warnings.append(f"🟠 {sec.consumer} WARNING — {cov.coverage_today:.1f}d cover")
    if sec.max_age >= 21:
        sec.warnings.append(f"⏰ {sec.max_age:.0f}-day coil — TDC risk")

    # Plain-language explanation
    reasons = []
    if cov and cov.status in ("CRITICAL", "WARNING"):
        reasons.append(f"{sec.consumer} has only {cov.coverage_today:.1f}d cover")
    if sec.max_age >= 14:
        reasons.append(f"oldest coil {sec.max_age:.0f} days")
    if sec.is_direct:
        reasons.append("direct to consumer this shift")
    sec.explanation = f"{sec.section_key}: " + ("; ".join(reasons) if reasons else "normal priority")

    return sec


def compute_priority(
    sections:          List[Dict],
    wip_df:            Optional[pd.DataFrame] = None,
    mode:              str = "H&T_FIRST",
    shift_no:          int = 1,
    downstream_demand: Optional[Dict] = None,
) -> Dict:
    coverage = (build_consumer_coverage(wip_df, sections, downstream_demand)
                if wip_df is not None else {})

    scored: List[SectionScore] = []
    for s in sections:
        sk  = s["section_key"]
        df  = s["coils_df"]
        mt  = float(df["Input Coil Weight"].sum())
        avg = float(df["Coil Age(# Days)"].fillna(0).mean())
        mx  = float(df["Coil Age(# Days)"].fillna(0).max())
        sec = SectionScore(
            section_key  = sk, mill=s["mill"], label=s["label"],
            n_coils=len(df), total_mt=round(mt, 2),
            avg_age=round(avg, 1), max_age=round(mx, 1),
            consumer     = CONFIG["section_to_consumer"].get(sk, "Unknown"),
            is_direct    = sk in CONFIG["direct_sections"],
            via_crs      = sk in CONFIG["via_crs_sections"],
            feeds_anneal = sk in CONFIG["feeds_anneal_sections"],
            mt_per_hour  = CONFIG["section_mt_per_hour"].get(
                sk, CONFIG["section_mt_per_hour"]["DEFAULT"]),
        )
        sec = _score_section(sec, mode, coverage, shift_no)
        scored.append(sec)

    crm04 = sorted([s for s in scored if s.mill == "CRM04"],
                   key=lambda x: x.total_score, reverse=True)
    crm06 = sorted([s for s in scored if s.mill == "CRM06"],
                   key=lambda x: x.total_score, reverse=True)
    for i, s in enumerate(crm04, 1): s.rank_crm04 = i
    for i, s in enumerate(crm06, 1): s.rank_crm06 = i

    kpis = {
        "total_mt":   round(sum(s.total_mt for s in scored), 1),
        "ht_mt":      round(sum(s.total_mt for s in scored if s.consumer == "H&T Line"), 1),
        "tube_mt":    round(sum(s.total_mt for s in scored if s.consumer == "Tube Plant"), 1),
        "oem_mt":     round(sum(s.total_mt for s in scored if s.consumer == "OEM"), 1),
        "anneal_mt":  round(sum(s.total_mt for s in scored if s.feeds_anneal), 1),
        "mode":       mode, "mode_desc": MODES.get(mode, mode), "shift_no": shift_no,
    }

    warnings = []
    for cname, cov in coverage.items():
        if cov.status == "CRITICAL":
            warnings.append(f"🔴 {cname} CRITICAL — only {cov.coverage_today:.1f}d cover")
        elif cov.status == "WARNING":
            warnings.append(f"🟠 {cname} WARNING — {cov.coverage_today:.1f}d cover")

    return {
        "crm04_sequence": crm04, "crm06_sequence": crm06,
        "coverage": coverage, "all_scores": scored,
        "warnings": warnings, "kpis": kpis,
    }


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — DEPLETION FORECAST (H&T and CRS only — the real bottlenecks)
# ══════════════════════════════════════════════════════════════════════════════

def forecast_depletion(
    wip_df:     pd.DataFrame,
    sections:   List[Dict],
    overrides:  Optional[Dict] = None,
    horizon:    int = 7,
) -> Dict:
    coverage = build_consumer_coverage(wip_df, sections, overrides)
    today    = datetime.now().date()
    result   = {}

    # Only forecast the two real bottlenecks
    for cname in ["H&T Line", "Tube Plant", "OEM", "Annealing"]:
        cov  = coverage.get(cname)
        if not cov: continue
        rate = cov.daily_requirement
        buf  = cov.buffer_mt + cov.incoming_today_mt

        projection = []
        level = buf
        for d in range(horizon + 1):
            if d > 0: level -= rate
            level = max(level, 0.0)
            projection.append({"day": d,
                                "date": str(today + timedelta(days=d)),
                                "buffer_mt": round(level, 1)})

        days_empty = round(buf / rate, 1) if rate else 99
        result[cname] = {
            "buffer_mt":         cov.buffer_mt,
            "incoming_today_mt": cov.incoming_today_mt,
            "consumption_rate":  rate,
            "days_to_empty":     days_empty,
            "empty_date":        (str(today + timedelta(days=int(days_empty)))
                                  if days_empty < horizon else None),
            "status":            cov.status,
            "required_today_mt": cov.required_today_mt,
            "projection":        projection,
        }
    return result


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — ROLLING SHEET BUILDER
# ══════════════════════════════════════════════════════════════════════════════

def build_rolling_sheet(
    sections: List[Dict],
    priority_result: Optional[Dict] = None,
    rolled_coils: Optional[set] = None,
) -> Dict:
    """
    Build ordered coil list per mill.
    rolled_coils: set of coil numbers already confirmed as rolled this shift.
    """
    rank_map = {}
    if priority_result:
        for s in priority_result.get("crm04_sequence", []):
            rank_map[("CRM04", s.section_key)] = s.rank_crm04
        for s in priority_result.get("crm06_sequence", []):
            rank_map[("CRM06", s.section_key)] = s.rank_crm06

    rolled = rolled_coils or set()
    sheets = {"CRM04": [], "CRM06": []}

    for mill in ("CRM04", "CRM06"):
        mill_secs = [s for s in sections if s["mill"] == mill]
        mill_secs.sort(key=lambda s: rank_map.get((mill, s["section_key"]), 99))
        seq = 0
        for rank_i, s in enumerate(mill_secs, 1):
            df = s["coils_df"]
            pending = [r for _, r in df.iterrows()
                       if str(r.get("Coil Number","")) not in rolled]
            done    = [r for _, r in df.iterrows()
                       if str(r.get("Coil Number","")) in rolled]
            sheets[mill].append({
                "type": "header",
                "priority": rank_i,
                "section":  s["section_key"],
                "label":    s["label"],
                "consumer": CONFIG["section_to_consumer"].get(s["section_key"], ""),
                "coil_count": len(df),
                "pending_count": len(pending),
                "done_count":    len(done),
                "total_mt": round(float(df["Input Coil Weight"].sum()), 1),
                "pending_mt": round(sum(float(r.get("Input Coil Weight",0) or 0)
                                        for r in pending), 1),
            })
            for r in pending:
                seq += 1
                sheets[mill].append({
                    "type": "coil", "seq": seq, "rolled": False,
                    "coil":    str(r.get("Coil Number", "")),
                    "width":   float(r.get("Actual Width", 0) or 0),
                    "thick":   float(r.get("Actual Thick", 0) or 0),
                    "rt":      float(r.get("Plan Rolling Thick 1", 0) or 0),
                    "weight":  round(float(r.get("Input Coil Weight", 0) or 0), 3),
                    "customer":str(r.get("Customer Desc", ""))[:18],
                    "remark":  str(r.get("Planning Remark", ""))[:25],
                    "age":     float(r.get("Coil Age(# Days)", 0) or 0),
                })
            for r in done:
                sheets[mill].append({
                    "type": "coil", "seq": 0, "rolled": True,
                    "coil":    str(r.get("Coil Number", "")),
                    "width":   float(r.get("Actual Width", 0) or 0),
                    "thick":   float(r.get("Actual Thick", 0) or 0),
                    "rt":      float(r.get("Plan Rolling Thick 1", 0) or 0),
                    "weight":  round(float(r.get("Input Coil Weight", 0) or 0), 3),
                    "customer":str(r.get("Customer Desc", ""))[:18],
                    "remark":  str(r.get("Planning Remark", ""))[:25],
                    "age":     float(r.get("Coil Age(# Days)", 0) or 0),
                })
    return sheets


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — CRS SETTING CHANGE OPTIMISER
# ══════════════════════════════════════════════════════════════════════════════

def _crs_cost(a: dict, b: dict, urgency_aware: bool = False) -> float:
    cc   = CONFIG["crs_cost"]
    cost = (abs(a["width"] - b["width"]) * cc["width_per_mm"] +
            abs(a["thick"] - b["thick"]) / 0.1 * cc["thick_per_0.1mm"])
    if a.get("product") != b.get("product"):
        cost += cc["product_change"]
    if a.get("customer") != b.get("customer"):
        cost += cc["customer_change"]
    if urgency_aware:
        age = b.get("age", 0)
        cost *= (0.70 if age > 21 else 0.85 if age > 14 else 1.0)
    return round(cost, 2)


def _count_changes(seq: list) -> int:
    return sum(
        1 for i in range(len(seq)-1)
        if (abs(seq[i]["width"] - seq[i+1]["width"]) > 2 or
            abs(seq[i]["thick"] - seq[i+1]["thick"]) > 0.05 or
            seq[i].get("product") != seq[i+1].get("product")))


def optimise_crs_sequence(
    sections:      List[Dict],
    urgency_aware: bool = False,
    rolled_coils:  Optional[set] = None,
) -> Dict:
    """Optimise CRS coil sequence. Excludes already-rolled coils."""
    via_crs = CONFIG["via_crs_sections"]
    rolled  = rolled_coils or set()
    coils   = []
    for s in sections:
        if s["section_key"] not in via_crs: continue
        for _, row in s["coils_df"].iterrows():
            cn = str(row.get("Coil Number", ""))
            if cn in rolled: continue
            coils.append({
                "coil_number": cn,
                "width":   float(row.get("Actual Width", 0)),
                "thick":   float(row.get("Plan Rolling Thick 1", 0)),
                "weight":  float(row.get("Input Coil Weight", 0)),
                "product": str(row.get("Product Code", "")),
                "customer":str(row.get("Customer Desc", ""))[:20],
                "section": s["section_key"],
                "age":     float(row.get("Coil Age(# Days)", 0) or 0),
            })

    if not coils:
        return {"error": "No CRS coils remaining"}

    orig_changes = _count_changes(coils)
    orig_cost    = sum(_crs_cost(coils[i], coils[i+1])
                       for i in range(len(coils)-1))

    # Greedy nearest-neighbour from every start
    def greedy(start):
        rem = coils[:]
        seq = [rem.pop(start)]
        while rem:
            last = seq[-1]
            nxt  = min(rem, key=lambda c: _crs_cost(last, c, urgency_aware))
            seq.append(nxt); rem.remove(nxt)
        return seq

    best, best_cost = coils[:], orig_cost
    for i in range(len(coils)):
        s = greedy(i)
        c = sum(_crs_cost(s[j], s[j+1], urgency_aware) for j in range(len(s)-1))
        if c < best_cost:
            best, best_cost = s, c

    # 2-opt
    improved = True
    while improved:
        improved = False
        for i in range(1, len(best)-1):
            for j in range(i+1, len(best)):
                ns = best[:i] + best[i:j+1][::-1] + best[j+1:]
                nc = sum(_crs_cost(ns[k], ns[k+1], urgency_aware)
                         for k in range(len(ns)-1))
                if nc < best_cost - 0.01:
                    best, best_cost, improved = ns, nc, True

    opt_changes = _count_changes(best)
    saved       = orig_changes - opt_changes

    change_events = []
    for i in range(len(best)-1):
        a, b   = best[i], best[i+1]
        events = []
        wdiff  = abs(a["width"] - b["width"])
        tdiff  = abs(a["thick"] - b["thick"])
        if wdiff > 2:
            events.append(f"Width {a['width']:.0f}→{b['width']:.0f}mm")
        if tdiff > 0.05:
            events.append(f"Thick {a['thick']:.2f}→{b['thick']:.2f}mm")
        if a.get("product") != b.get("product"):
            events.append(f"Product {a['product']}→{b['product']} ⚠️ MAJOR")
        if events:
            change_events.append({
                "position": i+1, "from_coil": a["coil_number"],
                "to_coil": b["coil_number"], "changes": events,
                "is_major": a.get("product") != b.get("product"),
            })

    recs = []
    if saved > 0:
        recs.append(f"✅ {saved} fewer changes ({orig_changes}→{opt_changes}) — ~{saved*8:.0f} min saved")
    else:
        recs.append("✅ Sequence already optimal")
    if any(e["is_major"] for e in change_events):
        recs.append("⚠️ Product change(s) unavoidable — schedule at shift start")
    widths = [c["width"] for c in best]
    if any(widths[i] < widths[i+1]-2 for i in range(len(widths)-1)):
        recs.append("⚠️ Width step-up present — check roll edge condition")

    return {
        "optimised_sequence": best, "original_changes": orig_changes,
        "optimised_changes": opt_changes, "changes_saved": saved,
        "change_events": change_events, "recommendations": recs,
        "total_coils": len(best),
        "total_mt": round(sum(c["weight"] for c in best), 2),
    }


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 7 — PRIORITY-SYNCED MILL PLAN + ROLL CHANGE OPTIMISATION  (SPEC §07.7/§08)
# ══════════════════════════════════════════════════════════════════════════════

SEC_TO_ROLL = {
    "ROLLING": "Light Matt", "FIRST_ROLLING": "Light Matt",
    "RE_ROLLING": "Light Matt",
    "HT_FINISH": "Bright", "CRCA_FINISH": "Bright",
    "CRCA_FINISH_CRM06": "Bright", "TUBE_FH": "Bright",
    "SKIN_PASS_SUPER_BRIGHT": "Super Bright",
    "SKIN_PASS_CHROME": "Chrome Plated", "ROLLING_BRIGHT": "Chrome Plated",
    "SKIN_PASS_HEAVY_MATT": "Heavy Matt",
}
ROLL_TYPES      = ["Light Matt", "Bright", "Super Bright",
                   "Chrome Plated", "Heavy Matt"]
ROLL_LIFE_MAX   = {"Light Matt": 300, "Bright": 180, "Super Bright": 120,
                   "Chrome Plated": 80, "Heavy Matt": 200}
ROLL_CHANGE_MIN = 45


def order_sections_by_priority(sections: List[Dict],
                               priority_result: Dict,
                               mill: str) -> List[Dict]:
    """Return this mill's sections ordered by their priority rank."""
    seq_key  = "crm04_sequence" if mill == "CRM04" else "crm06_sequence"
    rank_map = {s.section_key: (s.rank_crm04 if mill == "CRM04"
                                else s.rank_crm06)
                for s in priority_result.get(seq_key, [])}
    mill_secs = [s for s in sections if s["mill"] == mill]
    mill_secs.sort(key=lambda s: rank_map.get(s["section_key"], 99))
    return mill_secs


def count_roll_changes(ordered_secs: List[Dict], current_roll: str) -> int:
    """Count roll changes for a given section order and starting roll."""
    changes = 0
    roll = current_roll
    for s in ordered_secs:
        needed = SEC_TO_ROLL.get(s["section_key"], "Light Matt")
        if needed != roll:
            changes += 1
            roll = needed
    return changes


def build_alternate_order(ordered_secs: List[Dict],
                          current_roll: str,
                          priority_result: Dict,
                          mill: str) -> List[Dict]:
    """
    SPEC §07.7 Step 3 — Alternate Plan:
      1. Group sections by roll type
      2. Current roll group first (zero change cost)
      3. Within each group: priority order preserved
      4. Remaining groups ordered by their most urgent section
      5. Each subsequent group = 1 roll change
    """
    seq_key   = "crm04_sequence" if mill == "CRM04" else "crm06_sequence"
    score_map = {s.section_key: s.total_score
                 for s in priority_result.get(seq_key, [])}

    groups: Dict[str, List[Dict]] = {}
    for s in ordered_secs:                       # already in priority order
        rt = SEC_TO_ROLL.get(s["section_key"], "Light Matt")
        groups.setdefault(rt, []).append(s)

    ordered_groups = []
    if current_roll in groups:
        ordered_groups.append(current_roll)
    others = [rt for rt in groups if rt != current_roll]
    # Most urgent remaining group next (highest top-section score)
    others.sort(key=lambda rt: -max(
        score_map.get(s["section_key"], 0) for s in groups[rt]))
    ordered_groups += others

    return [s for rt in ordered_groups for s in groups[rt]]


def build_plan_comparison(sections: List[Dict],
                          priority_result: Dict,
                          current_rt04: str,
                          current_rt06: str) -> Dict:
    """
    Build Priority Plan + (if >1 change on either mill) Alternate Plan.
    Returns orders, change counts, downtime, savings and coverage warnings.
    """
    prio04 = order_sections_by_priority(sections, priority_result, "CRM04")
    prio06 = order_sections_by_priority(sections, priority_result, "CRM06")
    pc04   = count_roll_changes(prio04, current_rt04)
    pc06   = count_roll_changes(prio06, current_rt06)

    result = {
        "priority": {"CRM04": prio04, "CRM06": prio06,
                     "changes04": pc04, "changes06": pc06,
                     "total_changes": pc04 + pc06,
                     "downtime_min": (pc04 + pc06) * ROLL_CHANGE_MIN},
        "alternate": None,
        "warnings": [],
    }

    if pc04 > 1 or pc06 > 1:
        alt04 = build_alternate_order(prio04, current_rt04,
                                      priority_result, "CRM04")
        alt06 = build_alternate_order(prio06, current_rt06,
                                      priority_result, "CRM06")
        ac04  = count_roll_changes(alt04, current_rt04)
        ac06  = count_roll_changes(alt06, current_rt06)
        result["alternate"] = {
            "CRM04": alt04, "CRM06": alt06,
            "changes04": ac04, "changes06": ac06,
            "total_changes": ac04 + ac06,
            "downtime_min": (ac04 + ac06) * ROLL_CHANGE_MIN,
            "savings_min": (pc04 + pc06 - ac04 - ac06) * ROLL_CHANGE_MIN,
        }

        # Auto-warning: does alternate delay a CRITICAL/WARNING consumer?
        coverage = priority_result.get("coverage", {})
        for cname in ("H&T Line", "Tube Plant"):
            cov = coverage.get(cname)
            if not cov or cov.status not in ("CRITICAL", "WARNING"):
                continue
            for mill, prio, alt in (("CRM-04", prio04, alt04),
                                    ("CRM-06", prio06, alt06)):
                p_pos = next((i for i, s in enumerate(prio)
                              if CONFIG["section_to_consumer"].get(
                                  s["section_key"]) == cname), None)
                a_pos = next((i for i, s in enumerate(alt)
                              if CONFIG["section_to_consumer"].get(
                                  s["section_key"]) == cname), None)
                if p_pos is not None and a_pos is not None and a_pos > p_pos:
                    result["warnings"].append(
                        f"⚠️ {cname} is {cov.status} "
                        f"({cov.coverage_today:.1f}d cover). Alternate Plan "
                        f"delays its section on {mill}. "
                        f"Priority Plan recommended.")
    return result
