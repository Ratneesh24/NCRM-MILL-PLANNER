"""
crm/planner.py — Orchestrator
==============================
Bridges the VALIDATED Mill Planner core (generator/sectioning/ML routing —
unchanged) with the new pipeline-aware planning engine.

    WIP → [Mill Planner: filter → route → section]      (unchanged, validated)
        → [enrich with pipeline attributes]
        → [health → score → campaign → twin]            (new engine)
        → explainable rolling plan + Excel
"""
from __future__ import annotations
import io
import os
import sys
import tempfile
from datetime import date
from typing import Dict, List, Optional, Tuple

import pandas as pd

from . import config as C
from . import pipeline as P
from . import health as H
from . import scoring as S
from . import campaign as CP
from . import twin as T

# The validated core lives one level up
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ══════════════════════════════════════════════════════════════════════════════
def build_candidates(path: str, db: Optional[dict] = None
                     ) -> Tuple[pd.DataFrame, List[Dict]]:
    """
    Run the validated Mill Planner routing, then enrich each section's
    coils with pipeline attributes (consumer, ages, quality risk).
    Returns (full_enriched_wip, sections)
    """
    from generator import (load_wip, filter_rolling_coils,
                           assign_all, build_sections)

    full = P.load_pipeline(path)                    # enriched full pipeline

    wip      = load_wip(path)                      # validated loader
    eligible = filter_rolling_coils(wip)           # validated filters
    assigned = assign_all(eligible, db)            # validated ML routing
    sections = build_sections(assigned, db)        # validated sectioning

    # Enrich each section's coils_df with the pipeline attributes.
    # A coil number can repeat in the WIP (multi-stage rows) — keep the
    # ROLLING MILL row, else the first occurrence.
    attr_cols = ["coil", "mt", "coil_age", "stage_age", "consumer",
                 "qual_risk", "qual_flags", "quality", "customer",
                 "width", "thick", "rt", "age_band"]
    attrs = (full.assign(_pref=(full["stage"] != "ROLLING MILL").astype(int))
                 .sort_values("_pref")
                 .drop_duplicates(subset="coil", keep="first")[attr_cols])

    for s in sections:
        df  = s["coils_df"].copy()
        df["coil"] = df["Coil Number"].astype(str).str.strip()
        # Drop any colliding names before the merge
        df = df.drop(columns=[c for c in attr_cols if c != "coil"
                              and c in df.columns], errors="ignore")
        df = df.merge(attrs, on="coil", how="left")

        # Fallbacks for coils not present in the enriched frame
        df["mt"] = df["mt"].fillna(
            pd.to_numeric(df.get("Input Coil Weight", 0), errors="coerce"))
        df.loc[df["mt"] > 100, "mt"] /= 1000.0
        for col, dflt in (("coil_age", 0.0), ("stage_age", 0.0),
                          ("qual_risk", 0.0), ("width", 0.0),
                          ("thick", 0.0), ("rt", 0.0)):
            df[col] = pd.to_numeric(df.get(col), errors="coerce").fillna(dflt)
        for col in ("consumer", "quality", "customer", "qual_flags", "age_band"):
            df[col] = df.get(col, "").fillna("")
        df["consumer"] = df["consumer"].replace("", "OEM")

        s["coils_df"] = df

    return full, sections


def plan_feed_from(scored: List[S.SectionScore]) -> Dict[str, float]:
    """MT that today's selected sections will deliver to each consumer."""
    feed: Dict[str, float] = {}
    for s in scored:
        feed[s.consumer] = feed.get(s.consumer, 0.0) + s.total_mt
    return feed


# ══════════════════════════════════════════════════════════════════════════════
def run_planning(
    path:          str,
    mode:          str,
    current_rolls: Dict[str, str],
    mt_on_rolls:   Dict[str, float],
    demand:        Optional[Dict[str, float]] = None,
    db:            Optional[dict] = None,
    selected:      Optional[List[str]] = None,
) -> Dict:
    """
    Full planning run. Returns everything the UI needs.
    `selected` = optional list of section_keys the planner ticked.
    """
    full, sections = build_candidates(path, db)

    if selected is not None:
        sections = [s for s in sections if s["section_key"] in selected]

    consumer_h = H.consumer_health(full, overrides=demand)
    stage_h    = H.stage_health(full)

    scored = S.score_sections(sections, consumer_h, stage_h,
                              mode, current_rolls)

    plans  = CP.compare_plans(scored, current_rolls, mt_on_rolls)

    # Coverage AFTER today's plan is executed
    feed       = plan_feed_from(scored)
    consumer_after = H.consumer_health(full, plan_feed=feed, overrides=demand)

    return {
        "wip":            full,
        "sections":       sections,
        "scored":         scored,
        "consumer_health": consumer_h,
        "consumer_after":  consumer_after,
        "stage_health":   stage_h,
        "alerts":         H.alerts(consumer_h, stage_h),
        "plans":          plans,
        "plan_feed":      feed,
    }


# ══════════════════════════════════════════════════════════════════════════════
# EXCEL EXPORT — reuses the validated write_sheet (standard Tata format)
# ══════════════════════════════════════════════════════════════════════════════
def export_excel(
    scored:   List[S.SectionScore],
    plans:    Dict[str, Dict],
    choice:   Dict[str, str],          # {"CRM04": "priority"|"alternate"}
    db:       Optional[dict] = None,
    plan_date: Optional[date] = None,
) -> bytes:
    """
    Emit the rolling plan in the standard Tata Steel format,
    ordered exactly as the chosen campaign sequence.
    CRM-04 and CRM-06 on the same sheet (as today's plan format).
    """
    from openpyxl import Workbook
    from generator import write_sheet

    sec_lookup = {(s.mill, s.section_key): s for s in scored}
    ordered: List[Dict] = []

    for mill in ("CRM04", "CRM06"):
        if mill not in plans:
            continue
        mp = plans[mill][choice.get(mill, "priority")]
        seen: List[str] = []
        for camp in mp.campaigns:
            for sk in camp.sections:
                if sk in seen:
                    continue
                seen.append(sk)
                s = sec_lookup.get((mill, sk))
                if s is None:
                    continue
                # Only the coils that actually made it into the campaign
                camp_coils = {c["coil"] for c in camp.coils
                              if c["section"] == sk}
                df = s.coils_df
                df = df[df["coil"].astype(str).isin(camp_coils)]
                if df.empty:
                    continue
                ordered.append({"section_key": sk, "mill": mill,
                                "label": s.label, "coils_df": df})

    wb = Workbook(); wb.remove(wb.active)
    write_sheet(wb, plan_date or date.today(), ordered, db)
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    return buf.getvalue()
