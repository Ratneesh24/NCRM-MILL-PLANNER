"""
priority_advisor.py  —  Customer-Feeding Planning & Priority Engine  v2.0
=========================================================================
Tata Steel CRM Sahibabad — Narrow Complex

Redesigned from a section-priority tool into a full customer-feeding
planning advisor.  The engine answers:

  1. Which downstream consumer is at starvation risk?
  2. What material can feed that consumer, and when?
  3. Which section rolled NOW creates the best business impact?
  4. How to sequence CRS coils for minimum setting changes?

All business logic lives in CONFIG — no hard-coded weights in scoring code.
The shift-in-charge remains the final decision-maker.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import pandas as pd
import numpy as np


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — CONFIGURATION  (tune here, not in scoring code)
# ══════════════════════════════════════════════════════════════════════════════

CONFIG = {

    # ── Downstream consumers and their daily requirement (MT/day) ────────────
    "consumers": {
        "Tube Plant":  {"daily_mt": 80.0,  "priority": 10, "color": "red"},
        "H&T Line":    {"daily_mt": 45.0,  "priority": 9,  "color": "orange"},
        "CRS":         {"daily_mt": 110.0, "priority": 7,  "color": "yellow"},
        "Skin Pass":   {"daily_mt": 60.0,  "priority": 6,  "color": "blue"},
        "Annealing":   {"daily_mt": 130.0, "priority": 5,  "color": "green"},
    },

    # ── Which Rolling Mill sections feed which consumer ───────────────────────
    "section_to_consumer": {
        "TUBE_FH":                "Tube Plant",
        "HT_FINISH":              "H&T Line",
        "CRCA_FINISH":            "CRS",
        "CRCA_FINISH_CRM06":      "CRS",
        "SKIN_PASS_SUPER_BRIGHT": "Skin Pass",
        "SKIN_PASS_CHROME":       "Skin Pass",
        "SKIN_PASS_HEAVY_MATT":   "Skin Pass",
        "RE_ROLLING":             "Annealing",
        "FIRST_ROLLING":          "Annealing",
        "ROLLING":                "Annealing",
        "ROLLING_BRIGHT":         "Skin Pass",
    },

    # ── Which WIP Current Stages count as buffer for each consumer ────────────
    "consumer_buffer_stages": {
        "Tube Plant":  ["C R SLITTER"],          # TATFHC at CRS ready for Tube
        "H&T Line":    ["FURNACE"],              # material in H&T furnace
        "CRS":         ["C R SLITTER"],          # general CRS queue
        "Skin Pass":   ["SPM"],
        "Annealing":   ["ANB", "ANNEALING"],
    },

    # ── Stage → days until dispatchable (lead-time model) ────────────────────
    "stage_dispatch_days": {
        "DISPATCH_READY":         0,
        "09-QA":                  0,
        "INSPECTION TABLE/CTL":   0,
        "PALLETIZATION":          0,
        "PACK":                   0,
        "C R SLITTER":            1,
        "SPM":                    1,
        "FURNACE":                2,
        "GRINDING":               2,
        "EDGE ROUNDING":          2,
        "COLOR TEMPERING":        2,
        "ANB":                    3,     # annealing → out in ~72h
        "ANNEALING":              4,
        "REWINDING":              2,
        "ROLLING MILL":           1,     # if already at mill, rolled this shift
        "HR SLITTER":             1,
        "PICKLING":               2,
        "NC":                     5,
        "PENDING FOR PLAN":       99,    # not yet released
        "DEFAULT":                99,
    },

    # ── Whether a section is direct-dispatch (no further major processing) ────
    "direct_dispatch_sections": {
        "HT_FINISH", "SKIN_PASS_SUPER_BRIGHT",
        "SKIN_PASS_CHROME", "SKIN_PASS_HEAVY_MATT",
    },

    # ── Sections that go via CRS before final dispatch ────────────────────────
    "via_crs_sections": {
        "TUBE_FH", "CRCA_FINISH", "CRCA_FINISH_CRM06",
    },

    # ── Sections that feed annealing (72h pipeline) ───────────────────────────
    "feeds_anneal_sections": {
        "RE_ROLLING", "FIRST_ROLLING", "ROLLING", "ROLLING_BRIGHT",
    },

    # ── Approximate MT/hour per section ───────────────────────────────────────
    "section_mt_per_hour": {
        "ROLLING":                18.0,
        "FIRST_ROLLING":          16.0,
        "RE_ROLLING":             16.0,
        "TUBE_FH":                15.0,
        "CRCA_FINISH":            12.0,
        "CRCA_FINISH_CRM06":      12.0,
        "HT_FINISH":              13.0,
        "SKIN_PASS_SUPER_BRIGHT": 11.0,
        "SKIN_PASS_CHROME":       10.0,
        "SKIN_PASS_HEAVY_MATT":    9.0,
        "ROLLING_BRIGHT":         14.0,
        "DEFAULT":                14.0,
    },

    # ── Age score thresholds (days → score 0-100) ─────────────────────────────
    "age_thresholds": [
        (21, 100), (14, 80), (7, 55), (3, 30), (0, 10),
    ],

    # ── Alert levels for coverage days ───────────────────────────────────────
    "coverage_alerts": {
        "critical": 1.0,
        "warning":  2.0,
        "watch":    3.5,
        "ok":       99,
    },

    # ── Planning mode weight sets  (7 factors A-G) ───────────────────────────
    # Keys: starvation, customer, age, dispatch, pipeline, efficiency, setup
    "mode_weights": {
        "BALANCED": {
            "starvation": 0.28, "customer": 0.20, "age": 0.15,
            "dispatch":   0.15, "pipeline": 0.12, "efficiency": 0.07,
            "setup":      0.03,
        },
        "TUBE_URGENT": {
            "starvation": 0.50, "customer": 0.25, "age": 0.08,
            "dispatch":   0.10, "pipeline": 0.05, "efficiency": 0.02,
            "setup":      0.00,
        },
        "HT_URGENT": {
            "starvation": 0.45, "customer": 0.22, "age": 0.10,
            "dispatch":   0.15, "pipeline": 0.06, "efficiency": 0.02,
            "setup":      0.00,
        },
        "CRS_URGENT": {
            "starvation": 0.42, "customer": 0.20, "age": 0.10,
            "dispatch":   0.15, "pipeline": 0.08, "efficiency": 0.03,
            "setup":      0.02,
        },
        "MAX_PROD": {
            "starvation": 0.10, "customer": 0.08, "age": 0.05,
            "dispatch":   0.05, "pipeline": 0.05, "efficiency": 0.60,
            "setup":      0.07,
        },
        "CLEAR_BACKLOG": {
            "starvation": 0.12, "customer": 0.10, "age": 0.58,
            "dispatch":   0.08, "pipeline": 0.07, "efficiency": 0.03,
            "setup":      0.02,
        },
        "FEED_ANNEAL": {
            "starvation": 0.15, "customer": 0.10, "age": 0.12,
            "dispatch":   0.08, "pipeline": 0.48, "efficiency": 0.05,
            "setup":      0.02,
        },
        "DISPATCH_RECOVERY": {
            "starvation": 0.25, "customer": 0.20, "age": 0.10,
            "dispatch":   0.35, "pipeline": 0.05, "efficiency": 0.03,
            "setup":      0.02,
        },
        "PIPELINE_PROTECTION": {
            "starvation": 0.20, "customer": 0.15, "age": 0.10,
            "dispatch":   0.10, "pipeline": 0.38, "efficiency": 0.05,
            "setup":      0.02,
        },
    },

    # ── CRS transition cost weights ───────────────────────────────────────────
    "crs_cost": {
        "width_per_mm":     0.5,
        "thick_per_0.1mm":  2.0,
        "product_change":  10.0,
        "customer_change":  1.0,
    },

    # ── Customer urgency by name keyword ─────────────────────────────────────
    "customer_priority": {
        "TUBE PLANT":  10, "TUBE":    10,
        "TMA":          8, "BANDSAW":  7,
        "LG BALA":      5, "CALLIDA":  6,
        "VAISH":        6, "SFC":      5,
        "DEFAULT":      4,
    },
}

# Human-readable mode descriptions
MODES = {
    "BALANCED":          "Balanced — all factors equal",
    "TUBE_URGENT":       "Tube Urgent — Tube Plant at starvation risk",
    "HT_URGENT":         "H&T Urgent — H&T Line running out",
    "CRS_URGENT":        "CRS Urgent — CRS queue running low",
    "MAX_PROD":          "Max Production — highest MT/hour first",
    "CLEAR_BACKLOG":     "Clear Backlog — oldest coils / TDC expiry risk",
    "FEED_ANNEAL":       "Feed Annealing — protect 72h pipeline",
    "DISPATCH_RECOVERY": "Dispatch Recovery — fastest route to customer",
    "PIPELINE_PROTECTION":"Pipeline Protection — secure next 3-5 days",
}


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — STAGE-WISE AVAILABILITY ESTIMATOR
# ══════════════════════════════════════════════════════════════════════════════

def estimate_dispatch_days(current_stage: str) -> int:
    """
    Return estimated days until a coil at `current_stage` becomes
    dispatchable to its downstream consumer.
    Uses CONFIG['stage_dispatch_days'] — fully configurable.
    """
    stage = str(current_stage).strip()
    mapping = CONFIG["stage_dispatch_days"]
    # Try exact match first
    if stage in mapping:
        return mapping[stage]
    # Try partial match
    for k, v in mapping.items():
        if k in stage or stage in k:
            return v
    return mapping["DEFAULT"]


def classify_availability(days: int) -> str:
    """Bucket estimated dispatch days into availability class."""
    if days == 0:   return "today"
    if days == 1:   return "1_day"
    if days <= 3:   return "3_days"
    if days <= 5:   return "5_days"
    return "beyond_5"


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — CUSTOMER DEMAND COVERAGE BOARD
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class ConsumerCoverage:
    """Live demand coverage for one downstream consumer."""
    name:                  str
    daily_requirement:     float
    priority:              int

    # Buffer by availability horizon
    ready_today_mt:        float = 0.0
    ready_1day_mt:         float = 0.0
    ready_3day_mt:         float = 0.0
    ready_5day_mt:         float = 0.0
    ready_coils:           int   = 0

    # Today's plan adds to pipeline
    incoming_today_mt:     float = 0.0

    # Derived
    coverage_today:        float = 0.0
    coverage_3day:         float = 0.0
    coverage_5day:         float = 0.0
    projected_shortfall:   float = 0.0
    status:                str   = "OK"        # OK/WATCH/WARNING/CRITICAL
    days_to_empty:         float = 99.0
    required_today_mt:     float = 0.0         # MT needed today for 3-day cover
    stuck_coils:           int   = 0
    stuck_mt:              float = 0.0

    # Stage-wise breakdown
    stage_breakdown:       Dict  = field(default_factory=dict)


def build_consumer_coverage(
    wip_df: pd.DataFrame,
    sections: List[Dict],
    consumption_overrides: Optional[Dict] = None,
) -> Dict[str, ConsumerCoverage]:
    """
    Build the Customer Demand Coverage Board from full WIP DataFrame.
    Returns {consumer_name: ConsumerCoverage}
    """
    cfg    = CONFIG["consumers"]
    buf_st = CONFIG["consumer_buffer_stages"]
    alerts = CONFIG["coverage_alerts"]

    # Apply consumption overrides
    effective_cfg = {k: dict(v) for k, v in cfg.items()}
    if consumption_overrides:
        for k, v in consumption_overrides.items():
            if k in effective_cfg:
                effective_cfg[k]["daily_mt"] = v

    wip = wip_df.copy()
    wip.columns = wip.columns.str.strip()
    for col in ["Input Coil Weight", "Stage Age(# Days)", "Coil Age(# Days)"]:
        wip[col] = pd.to_numeric(wip.get(col, 0), errors="coerce").fillna(0)
    wip.loc[wip["Input Coil Weight"] > 100, "Input Coil Weight"] /= 1000.0

    # Today's plan feed per consumer
    plan_feed: Dict[str, float] = defaultdict(float)
    sec_map = CONFIG["section_to_consumer"]
    for s in sections:
        consumer = sec_map.get(s["section_key"], None)
        if consumer:
            plan_feed[consumer] += float(
                s["coils_df"]["Input Coil Weight"].sum())

    result = {}
    for consumer, ccfg in effective_cfg.items():
        stages     = buf_st.get(consumer, [])
        daily_req  = ccfg["daily_mt"]
        priority   = ccfg["priority"]

        buf = wip[wip["Current Stage"].isin(stages)].copy()
        buf["dispatch_days"] = buf["Current Stage"].apply(estimate_dispatch_days)
        buf["avail_class"]   = buf["dispatch_days"].apply(classify_availability)

        by_class = buf.groupby("avail_class")["Input Coil Weight"].sum()
        today_mt = float(by_class.get("today", 0) + by_class.get("1_day", 0))
        d3_mt    = today_mt + float(by_class.get("3_days", 0))
        d5_mt    = d3_mt    + float(by_class.get("5_days", 0))

        inc_today = float(plan_feed.get(consumer, 0))
        total_buf  = today_mt + inc_today

        days_empty  = round(total_buf / daily_req, 1) if daily_req else 99
        cov_today   = round(total_buf / daily_req, 2) if daily_req else 99
        cov_3d      = round((d3_mt + inc_today) / daily_req, 2) if daily_req else 99
        cov_5d      = round((d5_mt + inc_today) / daily_req, 2) if daily_req else 99
        shortfall   = max(0, round(3 * daily_req - (d3_mt + inc_today), 1))
        req_today   = max(0, round(3 * daily_req - today_mt, 1))

        stuck = buf[buf["Stage Age(# Days)"] > 21]

        # Status
        if   cov_today <= alerts["critical"]: status = "CRITICAL"
        elif cov_today <= alerts["warning"]:  status = "WARNING"
        elif cov_today <= alerts["watch"]:    status = "WATCH"
        else:                                  status = "OK"

        stage_bd = {}
        for stage in stages:
            s_coils = buf[buf["Current Stage"] == stage]
            if len(s_coils):
                stage_bd[stage] = {
                    "coils": len(s_coils),
                    "mt":    round(float(s_coils["Input Coil Weight"].sum()), 1),
                    "dispatch_days": estimate_dispatch_days(stage),
                }

        result[consumer] = ConsumerCoverage(
            name                = consumer,
            daily_requirement   = daily_req,
            priority            = priority,
            ready_today_mt      = round(today_mt, 1),
            ready_1day_mt       = round(float(by_class.get("1_day", 0)), 1),
            ready_3day_mt       = round(d3_mt, 1),
            ready_5day_mt       = round(d5_mt, 1),
            ready_coils         = len(buf),
            incoming_today_mt   = round(inc_today, 1),
            coverage_today      = cov_today,
            coverage_3day       = cov_3d,
            coverage_5day       = cov_5d,
            projected_shortfall = shortfall,
            status              = status,
            days_to_empty       = days_empty,
            required_today_mt   = req_today,
            stuck_coils         = len(stuck),
            stuck_mt            = round(float(stuck["Input Coil Weight"].sum()), 1),
            stage_breakdown     = stage_bd,
        )

    return result


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — 7-FACTOR SECTION SCORING ENGINE
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class SectionScore:
    # Identity
    section_key:     str
    mill:            str
    label:           str
    n_coils:         int
    total_mt:        float
    avg_age:         float
    max_age:         float
    consumer:        str
    feeds_anneal:    bool
    is_direct:       bool
    via_crs:         bool
    mt_per_hour:     float

    # Factor A–G scores (0-100)
    A_starvation:    float = 0.0   # downstream starvation risk
    B_customer:      float = 0.0   # customer business priority
    C_age:           float = 0.0   # coil ageing / TDC risk
    D_dispatch:      float = 0.0   # dispatch readiness / pipeline usefulness
    E_pipeline:      float = 0.0   # future annealing / pipeline protection
    F_efficiency:    float = 0.0   # production throughput
    G_setup:         float = 0.0   # CRS / setup continuity bonus

    total_score:     float = 0.0
    rank_crm04:      int   = 0
    rank_crm06:      int   = 0

    warnings:        List[str]  = field(default_factory=list)
    explanation:     str        = ""


def _age_score(max_age: float) -> float:
    for threshold, score in CONFIG["age_thresholds"]:
        if max_age >= threshold:
            return float(score)
    return 10.0


def _customer_score(coils_df: pd.DataFrame) -> float:
    cp = CONFIG["customer_priority"]
    def _score_row(cust: str) -> int:
        cu = str(cust).upper()
        for k, v in cp.items():
            if k in cu:
                return v
        return cp["DEFAULT"]
    scores = coils_df["Customer Desc"].apply(_score_row)
    return float(scores.mean()) * 10.0


def _compute_section_score(
    sec:        SectionScore,
    mode:       str,
    coverage:   Dict[str, ConsumerCoverage],
    shift_no:   int,
) -> SectionScore:
    """Score one section across all 7 factors using coverage board data."""

    alerts = CONFIG["coverage_alerts"]
    cov    = coverage.get(sec.consumer)

    # ── Factor A: Downstream starvation risk ──────────────────────────────
    if cov:
        if   cov.status == "CRITICAL": A = 100.0
        elif cov.status == "WARNING":  A = 80.0
        elif cov.status == "WATCH":    A = 55.0
        else:
            # Linearly grade by coverage days (10 = plenty, 0 = at max)
            A = max(0, min(40, (5.0 - cov.coverage_today) * 10))
        # Boost if this section is the ONLY feeder for a starved consumer
        if cov.status in ("CRITICAL", "WARNING"):
            A = min(100, A * 1.2)
    else:
        A = 20.0
    sec.A_starvation = round(A, 1)

    # ── Factor B: Customer business priority ──────────────────────────────
    consumer_priority = CONFIG["consumers"].get(sec.consumer, {}).get("priority", 5)
    B = float(consumer_priority) * 10.0
    sec.B_customer = round(min(100, B), 1)

    # ── Factor C: Coil ageing / TDC risk ─────────────────────────────────
    sec.C_age = round(_age_score(sec.max_age), 1)

    # ── Factor D: Dispatch readiness ──────────────────────────────────────
    if sec.is_direct:
        D = 90.0   # rolls today → reaches consumer this shift
    elif sec.via_crs:
        D = 65.0   # rolls today → CRS tomorrow → consumer day after
    elif sec.feeds_anneal:
        # Annealing pipeline: useful in 72h
        D = 40.0 if shift_no <= 2 else 25.0
    else:
        D = 30.0
    # Bonus if consumer is starved AND this section is direct
    if cov and cov.status in ("CRITICAL", "WARNING") and sec.is_direct:
        D = min(100, D + 20)
    sec.D_dispatch = round(D, 1)

    # ── Factor E: Future pipeline protection ──────────────────────────────
    if sec.feeds_anneal:
        base = 70.0 if shift_no <= 2 else 40.0
        # Extra urgency if annealing consumer is at risk
        ann_cov = coverage.get("Annealing")
        if ann_cov and ann_cov.status in ("CRITICAL", "WARNING"):
            base = min(100, base * 1.3)
        E = base
    elif sec.via_crs:
        # CRS feed protects Tube Plant / OEM pipeline
        tp_cov = coverage.get("Tube Plant")
        E = 60.0 if (tp_cov and tp_cov.status != "OK") else 40.0
    else:
        E = 20.0
    sec.E_pipeline = round(E, 1)

    # ── Factor F: Production efficiency ──────────────────────────────────
    max_speed = max(CONFIG["section_mt_per_hour"].values())
    sec.F_efficiency = round(sec.mt_per_hour / max_speed * 100, 1)

    # ── Factor G: Setup continuity (CRS) ─────────────────────────────────
    # Sections heading to CRS get a setup-synergy bonus
    # (actual CRS sequencing handled separately in optimiser)
    sec.G_setup = 60.0 if sec.via_crs else 30.0

    # ── Weighted total ────────────────────────────────────────────────────
    weights = CONFIG["mode_weights"].get(
        mode, CONFIG["mode_weights"]["BALANCED"])
    total = (
        sec.A_starvation * weights["starvation"] +
        sec.B_customer   * weights["customer"]   +
        sec.C_age        * weights["age"]        +
        sec.D_dispatch   * weights["dispatch"]   +
        sec.E_pipeline   * weights["pipeline"]   +
        sec.F_efficiency * weights["efficiency"] +
        sec.G_setup      * weights["setup"]
    )
    sec.total_score = round(total, 1)

    # ── Warnings ──────────────────────────────────────────────────────────
    if cov and cov.status == "CRITICAL":
        sec.warnings.append(
            f"🔴 {sec.consumer} CRITICAL — only {cov.coverage_today:.1f} day(s) cover")
    if cov and cov.status == "WARNING":
        sec.warnings.append(
            f"🟠 {sec.consumer} WARNING — {cov.coverage_today:.1f} day(s) cover")
    if sec.max_age >= 21:
        sec.warnings.append(
            f"⏰ {sec.max_age:.0f}-day coil — TDC expiry risk")
    if sec.feeds_anneal and shift_no == 1:
        sec.warnings.append(
            "🔄 Rolls now → annealing → returns in ~72h")

    # ── Natural-language explanation ──────────────────────────────────────
    reasons = []
    if A >= 70:
        reasons.append(
            f"{sec.consumer} has only {cov.coverage_today:.1f}d cover")
    if sec.C_age >= 80:
        reasons.append(f"oldest coil is {sec.max_age:.0f} days old")
    if sec.is_direct:
        reasons.append(
            f"rolling creates {sec.total_mt:.1f} MT direct feed this shift")
    if sec.feeds_anneal:
        reasons.append("supports 72h annealing pipeline")
    if cov and cov.required_today_mt > 0 and sec.is_direct:
        reasons.append(
            f"{cov.required_today_mt:.0f} MT needed today for 3-day cover")
    sec.explanation = (
        f"{sec.section_key} scored {sec.total_score:.0f} because: " +
        ("; ".join(reasons) if reasons else "balanced factors.")
    )

    return sec


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — MAIN COMPUTE ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def compute_priority(
    sections:             List[Dict],
    wip_df:               Optional[pd.DataFrame] = None,
    mode:                 str = "BALANCED",
    shift_no:             int = 1,
    downstream_demand:    Optional[Dict[str, float]] = None,
) -> Dict:
    """
    Main entry point.

    Parameters
    ----------
    sections          : from generator.build_sections()
    wip_df            : FULL raw WIP DataFrame (all stages — for coverage board)
    mode              : planning mode key
    shift_no          : 1, 2, or 3
    downstream_demand : optional {consumer: daily_mt} overrides

    Returns
    -------
    dict with crm04_sequence, crm06_sequence, coverage, all_scores,
    warnings, briefing, kpis, mode_comparison
    """
    cfg = CONFIG

    # ── Build coverage board ──────────────────────────────────────────────
    if wip_df is not None:
        coverage = build_consumer_coverage(
            wip_df, sections, downstream_demand)
    else:
        # Fallback: build empty coverage so scoring still works
        coverage = {
            c: ConsumerCoverage(
                name=c, daily_requirement=cfg["consumers"][c]["daily_mt"],
                priority=cfg["consumers"][c]["priority"])
            for c in cfg["consumers"]
        }

    # ── Score every section ───────────────────────────────────────────────
    scored: List[SectionScore] = []
    for s in sections:
        sk  = s["section_key"]
        df  = s["coils_df"]
        mt  = float(df["Input Coil Weight"].sum())
        n   = len(df)
        avg = float(df["Coil Age(# Days)"].fillna(0).mean())
        mx  = float(df["Coil Age(# Days)"].fillna(0).max())

        sec = SectionScore(
            section_key  = sk,
            mill         = s["mill"],
            label        = s["label"],
            n_coils      = n,
            total_mt     = round(mt, 2),
            avg_age      = round(avg, 1),
            max_age      = round(mx, 1),
            consumer     = cfg["section_to_consumer"].get(sk, "Unknown"),
            feeds_anneal = sk in cfg["feeds_anneal_sections"],
            is_direct    = sk in cfg["direct_dispatch_sections"],
            via_crs      = sk in cfg["via_crs_sections"],
            mt_per_hour  = cfg["section_mt_per_hour"].get(
                sk, cfg["section_mt_per_hour"]["DEFAULT"]),
        )
        sec = _compute_section_score(sec, mode, coverage, shift_no)
        scored.append(sec)

    # Separate and rank by mill
    crm04 = sorted([s for s in scored if s.mill == "CRM04"],
                   key=lambda x: x.total_score, reverse=True)
    crm06 = sorted([s for s in scored if s.mill == "CRM06"],
                   key=lambda x: x.total_score, reverse=True)
    for i, s in enumerate(crm04, 1): s.rank_crm04 = i
    for i, s in enumerate(crm06, 1): s.rank_crm06 = i

    # ── KPIs ──────────────────────────────────────────────────────────────
    total_mt   = sum(s.total_mt for s in scored)
    direct_mt  = sum(s.total_mt for s in scored if s.is_direct)
    crs_mt     = sum(s.total_mt for s in scored if s.via_crs)
    anneal_mt  = sum(s.total_mt for s in scored if s.feeds_anneal)
    tube_mt    = sum(s.total_mt for s in scored if "Tube" in s.consumer)
    ht_mt      = sum(s.total_mt for s in scored if "H&T" in s.consumer)

    kpis = {
        "total_mt":    round(total_mt, 1),
        "direct_mt":   round(direct_mt, 1),
        "via_crs_mt":  round(crs_mt, 1),
        "anneal_mt":   round(anneal_mt, 1),
        "tube_mt":     round(tube_mt, 1),
        "ht_mt":       round(ht_mt, 1),
        "mode":        mode,
        "mode_desc":   MODES.get(mode, mode),
        "shift_no":    shift_no,
    }

    # ── Global warnings ───────────────────────────────────────────────────
    global_warnings = []
    for cname, cov in coverage.items():
        if cov.status == "CRITICAL":
            global_warnings.append(
                f"🔴 CRITICAL: {cname} only {cov.coverage_today:.1f}d cover — "
                f"needs {cov.required_today_mt:.0f} MT rolled today")
        elif cov.status == "WARNING":
            global_warnings.append(
                f"🟠 WARNING: {cname} has {cov.coverage_today:.1f}d cover")
        if cov.stuck_coils > 5:
            global_warnings.append(
                f"⏰ {cov.stuck_coils} coils stuck >21 days at {cname} buffer")

    # ── Shift briefing ────────────────────────────────────────────────────
    briefing = _generate_briefing(
        crm04, crm06, kpis, coverage, global_warnings, mode, shift_no)

    return {
        "crm04_sequence": crm04,
        "crm06_sequence": crm06,
        "coverage":       coverage,
        "all_scores":     scored,
        "warnings":       global_warnings,
        "briefing":       briefing,
        "kpis":           kpis,
    }


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — SHIFT BRIEFING GENERATOR
# ══════════════════════════════════════════════════════════════════════════════

def _generate_briefing(
    crm04, crm06, kpis, coverage, warnings, mode, shift_no
) -> str:
    shift_names = {1: "Shift 1 (06:00–14:00)",
                   2: "Shift 2 (14:00–22:00)",
                   3: "Shift 3 (22:00–06:00)"}
    STATUS_ICON = {"CRITICAL":"🔴","WARNING":"🟠","WATCH":"🟡","OK":"🟢"}
    lines = [
        "🏭 *CRM NARROW COMPLEX — SHIFT PLAN*",
        f"📅 {shift_names.get(shift_no, f'Shift {shift_no}')}",
        f"⚙️  Mode: {MODES.get(mode, mode)}",
        "",
        "━━━ A. CUSTOMER DEMAND COVERAGE ━━━",
    ]
    for cname, cov in sorted(coverage.items(),
                              key=lambda x: x[1].priority, reverse=True):
        icon = STATUS_ICON[cov.status]
        lines.append(
            f"  {icon} {cname:12s}: {cov.coverage_today:.1f}d cover  "
            f"[Buffer {cov.ready_today_mt:.0f}MT + Plan {cov.incoming_today_mt:.0f}MT"
            f" / Need {cov.daily_requirement:.0f}MT/day]"
        )
        if cov.required_today_mt > 0:
            lines.append(
                f"       ⚡ Roll {cov.required_today_mt:.0f}MT today for 3-day cover")

    lines += [
        "",
        "━━━ B. TODAY'S PLAN SUMMARY ━━━",
        f"  Total planned     : {kpis['total_mt']} MT",
        f"  Via CRS → dispatch: {kpis['via_crs_mt']} MT  (Tube + CRCA)",
        f"  Direct dispatch   : {kpis['direct_mt']} MT  (H&T + Skin Pass)",
        f"  → Annealing (72h) : {kpis['anneal_mt']} MT",
        "",
        "━━━ C. CRM-04 PRIORITY SEQUENCE ━━━",
    ]
    for s in crm04[:6]:
        lines.append(
            f"  {s.rank_crm04}. {s.section_key.replace('_',' '):28s}"
            f" {s.total_mt:6.1f}MT  Score:{s.total_score:.0f}  [{s.consumer}]")
        if s.warnings:
            lines.append(f"     {s.warnings[0]}")

    lines += ["", "━━━ D. CRM-06 PRIORITY SEQUENCE ━━━"]
    for s in crm06[:6]:
        lines.append(
            f"  {s.rank_crm06}. {s.section_key.replace('_',' '):28s}"
            f" {s.total_mt:6.1f}MT  Score:{s.total_score:.0f}  [{s.consumer}]")
        if s.warnings:
            lines.append(f"     {s.warnings[0]}")

    if warnings:
        lines += ["", "━━━ E. WARNINGS ━━━"]
        for w in warnings:
            lines.append(f"  {w}")

    # Suggested actions
    actions = []
    for s in crm04[:2] + crm06[:2]:
        if s.warnings:
            a = f"Run {s.mill} {s.section_key.replace('_',' ')} first"
            if "CRITICAL" in (s.warnings[0] if s.warnings else ""):
                a += f" — {s.consumer} starvation risk"
            elif s.max_age >= 21:
                a += f" — clear {s.max_age:.0f}-day coils"
            actions.append(a)

    if actions:
        lines += ["", "━━━ F. SUGGESTED ACTIONS ━━━"]
        for a in actions[:4]:
            lines.append(f"  → {a}")

    lines += [
        "",
        "📌 Recommendation only — shift in-charge has final call.",
    ]
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 7 — DEPLETION FORECASTER (7-day projection)
# ══════════════════════════════════════════════════════════════════════════════

def forecast_depletion(
    wip_df:     pd.DataFrame,
    sections:   List[Dict],
    consumption: Optional[Dict] = None,
    horizon_days: int = 7,
) -> Dict:
    """Forecast when each downstream consumer's buffer runs out."""
    coverage = build_consumer_coverage(wip_df, sections, consumption)
    today    = datetime.now().date()
    result   = {}

    for cname, cov in coverage.items():
        rate    = cov.daily_requirement
        buffer  = cov.ready_today_mt + cov.incoming_today_mt

        projection = []
        level = buffer
        empty_day = None
        for d in range(horizon_days + 1):
            if d > 0:
                level -= rate
            level = max(level, 0.0)
            projection.append({
                "day":       d,
                "date":      str(today + timedelta(days=d)),
                "buffer_mt": round(level, 1),
            })
            if empty_day is None and level <= 0:
                empty_day = d

        days_to_empty = round(buffer / rate, 1) if rate else 99

        result[cname] = {
            "buffer_mt":         cov.ready_today_mt,
            "buffer_coils":      cov.ready_coils,
            "incoming_today_mt": cov.incoming_today_mt,
            "consumption_rate":  rate,
            "days_to_empty":     days_to_empty,
            "empty_date":        (str(today + timedelta(days=int(days_to_empty)))
                                  if days_to_empty < horizon_days else None),
            "status":            cov.status,
            "required_today_mt": cov.required_today_mt,
            "projection":        projection,
            "stage_breakdown":   cov.stage_breakdown,
        }
    return result


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 8 — ROLLING SHEET BUILDER
# ══════════════════════════════════════════════════════════════════════════════

def build_rolling_sheet(
    sections: List[Dict],
    priority_result: Optional[Dict] = None,
) -> Dict:
    """Build ordered coil-by-coil rolling list per mill with section headers."""
    rank_map = {}
    if priority_result:
        for s in priority_result.get("crm04_sequence", []):
            rank_map[("CRM04", s.section_key)] = s.rank_crm04
        for s in priority_result.get("crm06_sequence", []):
            rank_map[("CRM06", s.section_key)] = s.rank_crm06

    sheets = {"CRM04": [], "CRM06": []}
    for mill in ("CRM04", "CRM06"):
        mill_secs = [s for s in sections if s["mill"] == mill]
        mill_secs.sort(key=lambda s: rank_map.get((mill, s["section_key"]), 99))
        running_no = 0
        for rank_i, s in enumerate(mill_secs, 1):
            df = s["coils_df"]
            sheets[mill].append({
                "type":       "header",
                "priority":   rank_i,
                "section":    s["section_key"],
                "label":      s["label"],
                "consumer":   CONFIG["section_to_consumer"].get(s["section_key"], ""),
                "coil_count": len(df),
                "total_mt":   round(float(df["Input Coil Weight"].sum()), 1),
            })
            for _, r in df.iterrows():
                running_no += 1
                sheets[mill].append({
                    "type":     "coil",
                    "seq":      running_no,
                    "coil":     str(r.get("Coil Number", "")),
                    "width":    float(r.get("Actual Width", 0) or 0),
                    "thick":    float(r.get("Actual Thick", 0) or 0),
                    "rt":       float(r.get("Plan Rolling Thick 1", 0) or 0),
                    "weight":   round(float(r.get("Input Coil Weight", 0) or 0), 3),
                    "customer": str(r.get("Customer Desc", ""))[:18],
                    "remark":   str(r.get("Planning Remark", ""))[:25],
                    "age":      float(r.get("Coil Age(# Days)", 0) or 0),
                })
    return sheets


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 9 — CRS SETTING CHANGE OPTIMISER (unchanged, integrated)
# ══════════════════════════════════════════════════════════════════════════════

CRS_CHANGE_COST = CONFIG["crs_cost"]


def _crs_change_cost(a: dict, b: dict, urgency_aware: bool = False) -> float:
    w_diff = abs(a["width"] - b["width"])
    t_diff = abs(a["thick"] - b["thick"])
    cost   = (w_diff * CRS_CHANGE_COST["width_per_mm"] +
              (t_diff / 0.1) * CRS_CHANGE_COST["thick_per_0.1mm"])
    if a.get("product") != b.get("product"):
        cost += CRS_CHANGE_COST["product_change"]
    if a.get("customer") != b.get("customer"):
        cost += CRS_CHANGE_COST["customer_change"]
    # Urgency-aware: reduce cost for high-age coils running next
    if urgency_aware:
        age_b = b.get("age", 0)
        if age_b > 21:
            cost *= 0.7   # prefer running old coils even if slight cost
        elif age_b > 14:
            cost *= 0.85
    return round(cost, 2)


def _count_changes(seq: list) -> int:
    changes = 0
    for i in range(len(seq) - 1):
        a, b = seq[i], seq[i + 1]
        if (abs(a["width"] - b["width"]) > 2 or
                abs(a["thick"] - b["thick"]) > 0.05 or
                a.get("product") != b.get("product")):
            changes += 1
    return changes


def optimise_crs_sequence(
    sections:       List[Dict],
    urgency_aware:  bool = False,
    coverage:       Optional[Dict] = None,
) -> Dict:
    """
    Find CRS coil sequence minimising setting changes.
    urgency_aware=True biases toward running urgent/aged coils first.
    """
    via_crs = CONFIG["via_crs_sections"]
    crs_coils = []
    for s in sections:
        if s["section_key"] not in via_crs:
            continue
        for _, row in s["coils_df"].iterrows():
            crs_coils.append({
                "coil_number": str(row.get("Coil Number", "")),
                "width":       float(row.get("Actual Width", 0)),
                "thick":       float(row.get("Plan Rolling Thick 1", 0)),
                "weight":      float(row.get("Input Coil Weight", 0)),
                "product":     str(row.get("Product Code", "")),
                "customer":    str(row.get("Customer Desc", ""))[:20],
                "section":     s["section_key"],
                "age":         float(row.get("Coil Age(# Days)", 0) or 0),
            })

    if not crs_coils:
        return {"error": "No CRS coils in plan"}

    orig_cost    = sum(_crs_change_cost(crs_coils[i], crs_coils[i+1])
                       for i in range(len(crs_coils)-1))
    orig_changes = _count_changes(crs_coils)

    # Greedy nearest-neighbour from every start point
    def greedy(start):
        rem = crs_coils[:]
        seq = [rem.pop(start)]
        while rem:
            last = seq[-1]
            nxt  = min(rem, key=lambda c: _crs_change_cost(
                last, c, urgency_aware))
            seq.append(nxt); rem.remove(nxt)
        return seq

    best_seq  = crs_coils[:]
    best_cost = orig_cost
    for i in range(len(crs_coils)):
        s    = greedy(i)
        cost = sum(_crs_change_cost(s[j], s[j+1], urgency_aware)
                   for j in range(len(s)-1))
        if cost < best_cost:
            best_cost, best_seq = cost, s

    # 2-opt improvement
    improved = True
    while improved:
        improved = False
        for i in range(1, len(best_seq)-1):
            for j in range(i+1, len(best_seq)):
                ns   = best_seq[:i] + best_seq[i:j+1][::-1] + best_seq[j+1:]
                nc   = sum(_crs_change_cost(ns[k], ns[k+1], urgency_aware)
                           for k in range(len(ns)-1))
                if nc < best_cost - 0.01:
                    best_seq, best_cost, improved = ns, nc, True

    opt_changes = _count_changes(best_seq)
    saved       = orig_changes - opt_changes

    # Build change events list
    change_events = []
    for i in range(len(best_seq)-1):
        a, b   = best_seq[i], best_seq[i+1]
        events = []
        w_diff = abs(a["width"] - b["width"])
        t_diff = abs(a["thick"] - b["thick"])
        if w_diff > 2:
            events.append(
                f"Width: {a['width']:.0f}→{b['width']:.0f}mm (Δ{w_diff:.0f}mm)")
        if t_diff > 0.05:
            events.append(
                f"Thickness: {a['thick']:.2f}→{b['thick']:.2f}mm")
        if a.get("product") != b.get("product"):
            events.append(f"Product: {a['product']}→{b['product']} ⚠️ MAJOR")
        if events:
            change_events.append({
                "position":    i + 1,
                "from_coil":   a["coil_number"],
                "to_coil":     b["coil_number"],
                "changes":     events,
                "change_cost": _crs_change_cost(a, b),
                "is_major":    a.get("product") != b.get("product"),
            })

    # Recommendations
    recs = []
    if saved > 0:
        recs.append(
            f"✅ Resequencing saves {saved} change(s) "
            f"({orig_changes}→{opt_changes}) — ~{saved*8:.0f} min less downtime")
    else:
        recs.append("✅ Current sequence is already optimal for CRS")
    major = [e for e in change_events if e["is_major"]]
    if major:
        recs.append(
            f"⚠️ {len(major)} product change(s) unavoidable — "
            f"schedule at shift start or after a break")
    widths = [c["width"] for c in best_seq]
    if not all(widths[i] >= widths[i+1]-2 for i in range(len(widths)-1)):
        recs.append("⚠️ Width step-ups present — check roll edge condition")
    if urgency_aware:
        recs.append("ℹ️ Urgency-aware mode: aged/urgent coils biased to front")

    return {
        "original_sequence":    crs_coils,
        "optimised_sequence":   best_seq,
        "original_changes":     orig_changes,
        "optimised_changes":    opt_changes,
        "changes_saved":        saved,
        "total_cost_original":  round(orig_cost, 1),
        "total_cost_optimised": round(best_cost, 1),
        "change_events":        change_events,
        "recommendations":      recs,
        "total_coils":          len(crs_coils),
        "total_mt":             round(sum(c["weight"] for c in crs_coils), 2),
        "urgency_aware":        urgency_aware,
    }
