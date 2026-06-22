"""
outcome_logger.py — Daily Outcome Logger
=========================================
Captures what actually happened each day vs what was planned.
Stored in Supabase under key prefix "outcome_".

Feeds into:
  - Consumption rate calibration (replaces guessed 110/45/60 MT/day)
  - Roll life validation (replaces guessed 300/180/120 MT)
  - Roll change time validation (replaces guessed 45 min)
  - Starvation event history
  - Priority recommendation accuracy tracking
"""

from __future__ import annotations
from datetime import datetime
from typing import Optional
import pandas as pd


TABLE_PREFIX = "outcome_"


# ── Save / Load ───────────────────────────────────────────────────────────────

def save_outcome(outcome: dict) -> bool:
    try:
        from db import _get_client
        client = _get_client()
        if not client:
            return False
        key = (f"{TABLE_PREFIX}{outcome['log_date']}"
               f"_shift{outcome.get('shift_no', 0)}")
        client.table("learning_db").upsert({
            "key":        key,
            "data":       outcome,
            "updated_at": datetime.utcnow().isoformat(),
        }).execute()
        return True
    except Exception as e:
        print(f"[outcome_logger] save error: {e}")
        return False


def load_outcomes(days_back: int = 60) -> pd.DataFrame:
    try:
        from db import _get_client
        client = _get_client()
        if not client:
            return pd.DataFrame()
        resp = (client.table("learning_db")
                      .select("key,data,updated_at")
                      .like("key", f"{TABLE_PREFIX}%")
                      .execute())
        if not resp.data:
            return pd.DataFrame()
        rows = [r["data"] for r in resp.data if r.get("data")]
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        if "log_date" in df.columns:
            df["log_date"] = pd.to_datetime(df["log_date"])
            cutoff = pd.Timestamp.now() - pd.Timedelta(days=days_back)
            df = df[df["log_date"] >= cutoff].sort_values(
                "log_date", ascending=False).reset_index(drop=True)
        return df
    except Exception as e:
        print(f"[outcome_logger] load error: {e}")
        return pd.DataFrame()


def delete_outcome(log_date: str, shift_no: int) -> bool:
    try:
        from db import _get_client
        client = _get_client()
        if not client:
            return False
        key = f"{TABLE_PREFIX}{log_date}_shift{shift_no}"
        client.table("learning_db").delete().eq("key", key).execute()
        return True
    except Exception as e:
        print(f"[outcome_logger] delete error: {e}")
        return False


# ── Calibration engine ────────────────────────────────────────────────────────

def calibrate_from_outcomes(df: pd.DataFrame) -> dict:
    """
    Derive calibrated constants from the outcome log.
    Requires ≥5 days for meaningful numbers.
    Returns dict of measured values to replace CONFIG assumptions.
    """
    if df is None or len(df) < 3:
        n = len(df) if df is not None else 0
        return {"status": "insufficient_data",
                "message": f"Need ≥5 days of data. Have {n} so far.",
                "calibrated": {}}

    results = {"status": "ok", "n_days": len(df), "calibrated": {}}

    def _stats(series, label):
        vals = pd.to_numeric(series, errors="coerce").dropna()
        vals = vals[vals > 0]
        if len(vals) < 3:
            return None
        return {
            "mean":        round(float(vals.mean()), 1),
            "median":      round(float(vals.median()), 1),
            "p25":         round(float(vals.quantile(0.25)), 1),
            "p75":         round(float(vals.quantile(0.75)), 1),
            "min":         round(float(vals.min()), 1),
            "max":         round(float(vals.max()), 1),
            "n":           len(vals),
            "recommended": round(float(vals.median()), 1),
        }

    # Consumption rates per consumer
    for consumer, col in [
        ("CRS",        "crs_consumed_mt"),
        ("H&T Line",   "ht_consumed_mt"),
        ("Skin Pass",  "spm_consumed_mt"),
        ("Annealing",  "anneal_consumed_mt"),
        ("Tube Plant", "tube_consumed_mt"),
    ]:
        if col in df.columns:
            s = _stats(df[col], consumer)
            if s:
                results["calibrated"][f"{consumer}_daily_mt"] = s

    # Mill daily capacity
    for mill, col in [("CRM04", "crm04_mt_rolled"),
                      ("CRM06", "crm06_mt_rolled")]:
        if col in df.columns:
            s = _stats(df[col], mill)
            if s:
                results["calibrated"][f"{mill}_daily_capacity"] = s

    # Roll change time per change event
    for mill, n_col, t_col in [
        ("CRM04", "crm04_roll_changes", "crm04_change_min"),
        ("CRM06", "crm06_roll_changes", "crm06_change_min"),
    ]:
        if n_col in df.columns and t_col in df.columns:
            nc = pd.to_numeric(df[n_col], errors="coerce").fillna(0)
            tm = pd.to_numeric(df[t_col], errors="coerce").fillna(0)
            valid = nc > 0
            if valid.sum() >= 3:
                per_change = (tm[valid] / nc[valid]).dropna()
                results["calibrated"][f"{mill}_change_time_min"] = {
                    "mean":         round(float(per_change.mean()), 1),
                    "median":       round(float(per_change.median()), 1),
                    "n_events":     int(nc.sum()),
                    "recommended":  round(float(per_change.median()), 1),
                }

    # Starvation frequency
    for consumer, col in [
        ("CRS",        "crs_starved"),
        ("H&T Line",   "ht_starved"),
        ("Skin Pass",  "spm_starved"),
        ("Annealing",  "anneal_starved"),
        ("Tube Plant", "tube_starved"),
    ]:
        if col in df.columns:
            bools = df[col].map(
                lambda x: x if isinstance(x, bool)
                else str(x).lower() == "true")
            freq = round(float(bools.mean()) * 100, 1)
            results["calibrated"][f"{consumer}_starvation_pct"] = {
                "frequency_pct": freq,
                "n_events":      int(bools.sum()),
                "n_days":        len(bools),
                "risk":          ("HIGH"   if freq > 30 else
                                  "MEDIUM" if freq > 10 else "LOW"),
            }

    # Recommendation accuracy
    if ("recommendation_followed" in df.columns and
            "recommendation_accurate" in df.columns):
        def _tobool(x):
            return x if isinstance(x, bool) else str(x).lower() == "true"
        fol = df["recommendation_followed"].map(_tobool)
        acc = df["recommendation_accurate"].map(_tobool)
        results["calibrated"]["recommendation_accuracy"] = {
            "followed_pct":               round(fol.mean() * 100, 1),
            "accurate_when_followed_pct": (round(acc[fol].mean() * 100, 1)
                                           if fol.sum() > 0 else None),
            "n_days": len(df),
        }

    return results
