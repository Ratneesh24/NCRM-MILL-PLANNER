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
_H_STEP = re.compile(r"(?:^|[->\s(])H(?:[->\s)]|&|$)", re.I)


# ══════════════════════════════════════════════════════════════════════════════
# LOADER
# ══════════════════════════════════════════════════════════════════════════════
def load_pipeline(path_or_buf) -> pd.DataFrame:
    """Read WIP → validate → normalise → enrich (vectorised).

    Raises ValueError with a clear message when required columns are missing.
    """
    df = pd.read_excel(path_or_buf)
    df.columns = df.columns.str.strip()

    # ── Validation: required columns ─────────────────────────────────────
    required = ["Coil Number", "Current Stage", "Input Coil Weight"]
    missing  = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(
            f"WIP file is missing required column(s): {', '.join(missing)}. "
            f"Is this the correct coil-stage export?")

    # ── FIX 2: drop blank/duplicate rows ─────────────────────────────────
    df = df.dropna(subset=["Coil Number"]).reset_index(drop=True)
    if df.empty:
        raise ValueError("WIP file has no valid coil rows.")

    # ── Normalise weight to MT ───────────────────────────────────────────
    df["mt"] = pd.to_numeric(df["Input Coil Weight"], errors="coerce").fillna(0.0)
    df.loc[df["mt"] > 100, "mt"] /= 1000.0

    # ── Numerics ─────────────────────────────────────────────────────────
    for src_col, dst in [("Coil Age(# Days)",  "coil_age"),
                         ("Stage Age(# Days)", "stage_age"),
                         ("Order Age(# Days)", "order_age"),
                         ("Actual Thick",      "thick"),
                         ("Actual Width",      "width"),
                         ("Plan Rolling Thick 1", "rt"),
                         ("Yield Strength",    "ys")]:
        df[dst] = pd.to_numeric(df.get(src_col, 0), errors="coerce").fillna(0.0)

    # ── Strings (vectorised, NaN-safe) ───────────────────────────────────
    def _s(col):
        return df.get(col, pd.Series("", index=df.index)) \
                 .fillna("").astype(str).str.strip()
    df["stage"]    = _s("Current Stage")
    df["next"]     = _s("Next Stage")
    df["customer"] = _s("Customer Desc")
    df["coil"]     = _s("Coil Number")
    df["quality"]  = _s("Actual Quality")
    df["product"]  = _s("Product Code")
    df["storage"]  = _s("Storage Location")

    # ── FIX 1: CRS planning-scope flag (storage RNM6/R032/R033) ─────────
    df["in_scope_crs"] = (
        (df["stage"] == "C R SLITTER") &
        (df["storage"].isin(C.CRS_SCOPE_LOCATIONS)))

    # ── Consumer classification (vectorised) ─────────────────────────────
    route_u = _s("Process Route").str.upper()
    cust_u  = df["customer"].str.upper()
    is_tube = cust_u.str.contains("TUBE PLANT", na=False)
    is_ht   = route_u.str.contains("H&T", na=False) | \
              route_u.str.contains(_H_STEP.pattern, regex=True, na=False)
    df["consumer"] = "OEM"
    df.loc[is_ht,   "consumer"] = "H&T"
    df.loc[is_tube, "consumer"] = "TUBE"   # tube wins over H&T

    # ── Aging band + score (vectorised via cut) ──────────────────────────
    df["age_band"]  = df["coil_age"].apply(C.age_band)
    df["age_score"] = df["coil_age"].apply(C.age_score)

    # ── Quality risk (vectorised) ─────────────────────────────────────────
    surf_u = _s("Surface Finish").str.upper()
    q      = pd.Series(0, index=df.index, dtype=int)
    flags  = pd.Series([[] for _ in range(len(df))], index=df.index)

    m = surf_u.isin(C.SURFACE_CRITICAL)
    q  += m * 35
    flags[m] = flags[m].apply(lambda l: l + ["Surface critical"])

    tol = _s("Thickness Tolerance").str.extract(
        r"([\d.]+)\s*-\s*([\d.]+)").astype(float)
    band_um = (tol[1] - tol[0]) * 1000
    m = (band_um > 0) & (band_um <= C.TIGHT_TOL_UM)
    q  += m.fillna(False) * 35
    flags[m.fillna(False)] = flags[m.fillna(False)].apply(
        lambda l: l + ["Tight tolerance"])

    crit_pat = "|".join(C.OEM_CRITICAL_CUST)
    m = cust_u.str.contains(crit_pat, na=False, regex=True)
    q  += m * 20
    flags[m] = flags[m].apply(lambda l: l + ["OEM critical customer"])

    m = df["ys"] > 400
    q  += m * 10
    flags[m] = flags[m].apply(lambda l: l + ["High yield strength"])

    df["qual_risk"]  = q.clip(upper=100)
    df["qual_flags"] = flags.apply(" · ".join)

    # ── Stage order (for flow diagrams) ───────────────────────────────────
    df["stage_order"] = df["stage"].map(
        lambda s: C.STAGES.get(s, {}).get("order", 99))

    return df


def scoped(df: pd.DataFrame) -> pd.DataFrame:
    """The planning-scope view of the WIP (Requirement 6 — consistency).

    Removes CRS coils whose storage location is outside RNM6/R032/R033.
    Every module that aggregates 'the pipeline' should use this view so that
    Pipeline Overview, Stage Health, Consumer Health, Digital Twin, Alerts
    and Plan Builder all agree on the same numbers.
    """
    if "in_scope_crs" not in df.columns:
        return df
    return df[(df["stage"] != "C R SLITTER") | df["in_scope_crs"]]


# ══════════════════════════════════════════════════════════════════════════════
# STAGE-WISE BREAKUP  (Guideline §1)
# ══════════════════════════════════════════════════════════════════════════════
def stage_breakup(df: pd.DataFrame,
                  stages: Optional[List[str]] = None) -> pd.DataFrame:
    """Stage × Consumer inventory matrix with totals.
    For C R SLITTER, only counts in-scope storage locations (RNM6/R032/R033).
    """
    if stages:
        # For non-CRS stages: normal filter
        # For CRS: apply scope filter
        non_crs = df[(df["stage"].isin(stages)) & (df["stage"] != "C R SLITTER")]
        crs_in  = df[df["in_scope_crs"]] if "in_scope_crs" in df.columns else                   df[df["stage"] == "C R SLITTER"]
        if "C R SLITTER" in stages:
            d = pd.concat([non_crs, crs_in])
        else:
            d = non_crs
    else:
        non_crs = df[df["stage"] != "C R SLITTER"]
        crs_in  = df[df["in_scope_crs"]] if "in_scope_crs" in df.columns else                   df[df["stage"] == "C R SLITTER"]
        d = pd.concat([non_crs, crs_in])
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
    """Customer-wise drill-down for a consumer (Guideline §1).
    FIX 2: stage is now a clean string (NaN rows dropped in loader).
    FIX 1: CRS stage uses in_scope_crs filter.
    """
    d = df[df["consumer"] == consumer].copy()
    if stage and stage != "— all stages —":
        if stage == "C R SLITTER" and "in_scope_crs" in d.columns:
            d = d[d["in_scope_crs"]]
        else:
            d = d[d["stage"] == stage]
    out = (d.groupby("customer")
             .agg(coils=("coil", "count"), mt=("mt", "sum"),
                  avg_age=("coil_age", "mean"),
                  max_age=("coil_age", "max"),
                  qual_risk=("qual_risk", "mean"))
             .round(1).sort_values("mt", ascending=False))
    return out


def aging_profile(df: pd.DataFrame) -> pd.DataFrame:
    """WIP tonnage by age band × consumer (Guideline §3).
    Uses the planning-scope view (out-of-scope CRS excluded)."""
    df = scoped(df)
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
    d = scoped(df)
    d = d[(d["stage"] != "") & (d["next"] != "")]
    e = (d.groupby(["stage", "next", "consumer"])
           .agg(mt=("mt", "sum"), coils=("coil", "count"))
           .reset_index())
    e = e[e["mt"] >= min_mt]
    e["mt"] = e["mt"].round(1)
    return e.sort_values("mt", ascending=False)


def stuck_wip(df: pd.DataFrame, days: int = 21) -> pd.DataFrame:
    """Coils sitting at one stage beyond `days` — inventory rotation risk."""
    d = scoped(df)
    d = d[d["stage_age"] >= days].copy()
    return (d.groupby(["stage", "consumer"])
              .agg(coils=("coil", "count"), mt=("mt", "sum"),
                   oldest=("stage_age", "max"))
              .round(1).sort_values("mt", ascending=False))
