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
