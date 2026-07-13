"""
crm/pipeline.py — WIP Pipeline Model  (Guideline §1, §8)
=========================================================
Loads the raw WIP, classifies every coil by downstream consumer,
computes stage-wise inventory with full drill-down capability,
and models material flow between stages.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pandas as pd

from . import config as C


# ══════════════════════════════════════════════════════════════════════════════
# CONSUMER CLASSIFICATION
# ══════════════════════════════════════════════════════════════════════════════
_H_STEP = re.compile(r"(^|[->\s(])H([->\s)]|&|$)", re.I)


def classify_consumer(row) -> str:
    """
    TUBE : customer is Sahibabad Tube Plant
    H&T  : process route contains an H (heat-treatment) step
    OEM  : everything else
    """
    cust  = str(row.get("Customer Desc", "")).upper()
    route = str(row.get("Process Route", "")).upper()
    if "TUBE PLANT" in cust:
        return "TUBE"
    if "H&T" in route or _H_STEP.search(route):
        return "H&T"
    return "OEM"


# ══════════════════════════════════════════════════════════════════════════════
# LOADER
# ══════════════════════════════════════════════════════════════════════════════
def load_pipeline(path_or_buf) -> pd.DataFrame:
    """Read WIP → normalise → enrich with consumer, ages, quality risk."""
    df = pd.read_excel(path_or_buf)
    df.columns = df.columns.str.strip()

    # Normalise weight to MT
    df["mt"] = pd.to_numeric(df.get("Input Coil Weight", 0),
                             errors="coerce").fillna(0.0)
    df.loc[df["mt"] > 100, "mt"] /= 1000.0

    # Numerics
    for src, dst in [("Coil Age(# Days)",  "coil_age"),
                     ("Stage Age(# Days)", "stage_age"),
                     ("Order Age(# Days)", "order_age"),
                     ("Actual Thick",      "thick"),
                     ("Actual Width",      "width"),
                     ("Plan Rolling Thick 1", "rt"),
                     ("Yield Strength",    "ys")]:
        df[dst] = pd.to_numeric(df.get(src, 0), errors="coerce").fillna(0.0)

    df["stage"]    = df.get("Current Stage", "").astype(str).str.strip()
    df["next"]     = df.get("Next Stage", "").astype(str).str.strip()
    df["customer"] = df.get("Customer Desc", "").astype(str).str.strip()
    df["coil"]     = df.get("Coil Number", "").astype(str).str.strip()
    df["quality"]  = df.get("Actual Quality", "").astype(str).str.strip()
    df["product"]  = df.get("Product Code", "").astype(str).str.strip()

    # Consumer
    df["consumer"] = df.apply(classify_consumer, axis=1)

    # Aging band + quality risk
    df["age_band"]   = df["coil_age"].apply(C.age_band)
    df["age_score"]  = df["coil_age"].apply(C.age_score)
    df["qual_risk"], df["qual_flags"] = zip(*df.apply(_quality_risk, axis=1))

    # Stage order (for flow diagrams)
    df["stage_order"] = df["stage"].map(
        lambda s: C.STAGES.get(s, {}).get("order", 99))

    return df


def _quality_risk(row):
    """Quality criticality score 0-100 + list of reasons (Guideline §7)."""
    score, flags = 0, []
    surf = str(row.get("Surface Finish", "")).upper().strip()
    if surf in C.SURFACE_CRITICAL:
        score += 35; flags.append("Surface critical")

    tol = str(row.get("Thickness Tolerance", ""))
    m = re.match(r"([\d.]+)\s*-\s*([\d.]+)", tol)
    if m:
        band_um = (float(m.group(2)) - float(m.group(1))) * 1000
        if 0 < band_um <= C.TIGHT_TOL_UM:
            score += 35; flags.append(f"Tight tol ±{band_um/2:.0f}µm")

    cust = str(row.get("Customer Desc", "")).upper()
    if any(k in cust for k in C.OEM_CRITICAL_CUST):
        score += 20; flags.append("OEM critical customer")

    if float(row.get("ys", 0) or 0) > 400:
        score += 10; flags.append("High yield strength")

    return min(score, 100), " · ".join(flags)


# ══════════════════════════════════════════════════════════════════════════════
# STAGE-WISE BREAKUP  (Guideline §1)
# ══════════════════════════════════════════════════════════════════════════════
def stage_breakup(df: pd.DataFrame,
                  stages: Optional[List[str]] = None) -> pd.DataFrame:
    """Stage × Consumer inventory matrix with totals."""
    d = df[df["stage"].isin(stages)] if stages else df
    piv = d.pivot_table(index="stage", columns="consumer",
                        values="mt", aggfunc="sum").fillna(0.0).round(1)
    for c in ("TUBE", "OEM", "H&T"):
        if c not in piv.columns: piv[c] = 0.0
    piv = piv[["TUBE", "OEM", "H&T"]]
    piv["TOTAL"] = piv.sum(axis=1).round(1)
    cnt = d.groupby("stage")["coil"].count().rename("COILS")
    age = d.groupby("stage")["coil_age"].mean().round(1).rename("AVG AGE")
    out = piv.join(cnt).join(age)
    out["_ord"] = [C.STAGES.get(s, {}).get("order", 99) for s in out.index]
    return out.sort_values("_ord").drop(columns="_ord")


def customer_breakup(df: pd.DataFrame, consumer: str = "OEM",
                     stage: Optional[str] = None) -> pd.DataFrame:
    """Customer-wise drill-down for a consumer (Guideline §1)."""
    d = df[df["consumer"] == consumer]
    if stage:
        d = d[d["stage"] == stage]
    out = (d.groupby("customer")
             .agg(coils=("coil", "count"), mt=("mt", "sum"),
                  avg_age=("coil_age", "mean"),
                  max_age=("coil_age", "max"),
                  qual_risk=("qual_risk", "mean"))
             .round(1).sort_values("mt", ascending=False))
    return out


def aging_profile(df: pd.DataFrame) -> pd.DataFrame:
    """WIP tonnage by age band × consumer (Guideline §3)."""
    order = [b[2] for b in C.AGE_BANDS]
    piv = df.pivot_table(index="age_band", columns="consumer",
                         values="mt", aggfunc="sum").fillna(0.0).round(1)
    for c in ("TUBE", "OEM", "H&T"):
        if c not in piv.columns: piv[c] = 0.0
    piv = piv[["TUBE", "OEM", "H&T"]]
    piv["TOTAL"] = piv.sum(axis=1).round(1)
    piv = piv.reindex([b for b in order if b in piv.index])
    return piv


def flow_edges(df: pd.DataFrame, min_mt: float = 5.0) -> pd.DataFrame:
    """
    Material movement edges (current stage → next stage) for Sankey.
    Guideline §9.
    """
    d = df[(df["stage"] != "") & (df["next"] != "")]
    e = (d.groupby(["stage", "next", "consumer"])
           .agg(mt=("mt", "sum"), coils=("coil", "count"))
           .reset_index())
    e = e[e["mt"] >= min_mt]
    e["mt"] = e["mt"].round(1)
    return e.sort_values("mt", ascending=False)


def stuck_wip(df: pd.DataFrame, days: int = 21) -> pd.DataFrame:
    """Coils sitting at one stage beyond `days` — inventory rotation risk."""
    d = df[df["stage_age"] >= days].copy()
    return (d.groupby(["stage", "consumer"])
              .agg(coils=("coil", "count"), mt=("mt", "sum"),
                   oldest=("stage_age", "max"))
              .round(1).sort_values("mt", ascending=False))
