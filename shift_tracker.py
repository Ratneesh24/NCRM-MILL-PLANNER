"""
shift_tracker.py — Live Shift Execution Tracker
================================================
Manages the shift execution state: which coils are done, skipped, or pending.
Persists everything to Supabase so it survives page refreshes.

Data stored per shift:
  - shift_id        : "YYYY-MM-DD_SHIFT1/2/3"
  - plan_date       : date string
  - shift_number    : 1 / 2 / 3
  - mill            : CRM04 / CRM06
  - coils           : list of coil execution records
  - roll_type       : roll type at start of shift
  - mt_rolled       : running MT confirmed rolled
  - mt_target       : total MT planned for this mill
  - started_at      : ISO timestamp
  - last_updated    : ISO timestamp
  - status          : ACTIVE / COMPLETED / PAUSED
"""

import json
import os
from datetime import datetime, date
from typing import List, Dict, Optional

_SHIFT_KEY_PREFIX      = "shift_exec_"
_SHIFT_KEY_PREFIX_TEST = "TEST_shift_exec_"
_LOCAL_SHIFT_DIR       = "/tmp/shifts"

def _shift_prefix() -> str:
    try:
        from db import is_test_mode
        return _SHIFT_KEY_PREFIX_TEST if is_test_mode() else _SHIFT_KEY_PREFIX
    except Exception:
        return _SHIFT_KEY_PREFIX

SHIFT_NAMES = {1: "Shift 1 (06:00–14:00)",
               2: "Shift 2 (14:00–22:00)",
               3: "Shift 3 (22:00–06:00)"}

COIL_STATUS = {
    "PENDING":    "⏳ Pending",
    "ROLLED":     "✅ Rolled",
    "SKIPPED":    "⏭️ Skipped",
    "ON_HOLD":    "🔴 Hold",
    "PARTIAL":    "⚠️ Partial",
}


def _shift_key(plan_date: str, mill: str, shift_no: int) -> str:
    return f"{_shift_prefix()}{plan_date}_{mill}_S{shift_no}"


def _local_path(key: str) -> str:
    os.makedirs(_LOCAL_SHIFT_DIR, exist_ok=True)
    return os.path.join(_LOCAL_SHIFT_DIR, f"{key}.json")

def _get_client():
    """Reuse client from db.py."""
    try:
        from db import _get_client as _db_client
        return _db_client()
    except Exception:
        return None


def build_shift_record(plan_date: str, shift_no: int,
                       mill: str, sections: List[Dict],
                       roll_type: str) -> Dict:
    """
    Create a fresh shift record from the generated plan sections for one mill.
    """
    coils = []
    for sec in sections:
        if sec["mill"] != mill and not (
                mill in ("CRM04", "CRM06") and sec["mill"] == f"{mill}"):
            continue
        for _, row in sec["coils_df"].iterrows():
            coils.append({
                "coil_number":   str(row.get("Coil Number", "")),
                "so_no":         str(row.get("SO No", "")),
                "section_key":   sec["section_key"],
                "mill":          mill,
                "width":         float(row.get("Actual Width", 0)),
                "thick":         float(row.get("Actual Thick", 0)),
                "rt":            float(row.get("Plan Rolling Thick 1", 0)),
                "weight":        float(row.get("Input Coil Weight", 0)),
                "customer":      str(row.get("Customer Desc", "")),
                "quality":       str(row.get("Actual Quality", "")),
                "tdc":           str(row.get("Cust TDC", "")),
                "remark":        str(row.get("Planning Remark", "")),
                "storage":       str(row.get("Storage Location", "")),
                "roll_type":     roll_type,
                "status":        "PENDING",
                "actual_weight": None,   # filled when confirmed
                "confirmed_at":  None,
                "confirmed_by":  None,
                "notes":         "",
                "speed_mpm":     None,   # optional: actual rolling speed
                "thickness_achieved": None,  # actual output thickness
            })

    return {
        "shift_id":       _shift_key(plan_date, mill, shift_no),
        "plan_date":      plan_date,
        "shift_no":       shift_no,
        "shift_name":     SHIFT_NAMES.get(shift_no, f"Shift {shift_no}"),
        "mill":           mill,
        "roll_type":      roll_type,
        "coils":          coils,
        "mt_rolled":      0.0,
        "mt_target":      round(sum(c["weight"] for c in coils), 2),
        "coils_rolled":   0,
        "coils_skipped":  0,
        "coils_on_hold":  0,
        "total_coils":    len(coils),
        "started_at":     None,
        "last_updated":   datetime.utcnow().isoformat(),
        "status":         "ACTIVE",
        "operator_notes": "",
    }


# ── Persistence ─────────────────────────────────────────────────────────────

def save_shift(shift: Dict) -> bool:
    shift["last_updated"] = datetime.utcnow().isoformat()
    key = shift["shift_id"]
    success = False

    client = _get_client()
    if client:
        try:
            client.table("learning_db").upsert({
                "key":        key,
                "data":       shift,
                "updated_at": shift["last_updated"],
            }).execute()
            success = True
        except Exception as e:
            print(f"[Supabase] shift save error: {e}")

    try:
        local = _local_path(key)
        tmp   = local + ".tmp"
        with open(tmp, "w") as f:
            json.dump(shift, f, indent=2, default=str)
        os.replace(tmp, local)
        if not success:
            success = True
    except Exception:
        pass

    return success


def load_shift(plan_date: str, mill: str, shift_no: int) -> Optional[Dict]:
    key = _shift_key(plan_date, mill, shift_no)

    client = _get_client()
    if client:
        try:
            resp = (client.table("learning_db")
                          .select("data")
                          .eq("key", key)
                          .execute())
            if resp.data:
                return resp.data[0]["data"]
        except Exception:
            pass

    local = _local_path(key)
    if os.path.exists(local):
        try:
            with open(local) as f:
                return json.load(f)
        except Exception:
            pass

    return None


def list_recent_shifts(days_back: int = 7) -> List[Dict]:
    """Return all shift records from the last N days (summary only)."""
    results = []
    client  = _get_client()
    if client:
        try:
            resp = (client.table("learning_db")
                          .select("key,data,updated_at")
                          .like("key", f"{_shift_prefix()}%")
                          .order("updated_at", desc=True)
                          .limit(50)
                          .execute())
            for row in resp.data:
                d = row["data"]
                results.append({
                    "shift_id":     d.get("shift_id",""),
                    "plan_date":    d.get("plan_date",""),
                    "shift_name":   d.get("shift_name",""),
                    "mill":         d.get("mill",""),
                    "roll_type":    d.get("roll_type",""),
                    "mt_rolled":    d.get("mt_rolled",0),
                    "mt_target":    d.get("mt_target",0),
                    "coils_rolled": d.get("coils_rolled",0),
                    "total_coils":  d.get("total_coils",0),
                    "status":       d.get("status",""),
                    "last_updated": d.get("last_updated",""),
                    "adherence_pct": round(
                        d.get("mt_rolled",0) /
                        max(d.get("mt_target",1), 0.01) * 100, 1),
                })
        except Exception:
            pass

    return results


def confirm_coil(shift: Dict, coil_number: str,
                 status: str = "ROLLED",
                 actual_weight: float = None,
                 thickness_achieved: float = None,
                 notes: str = "",
                 confirmed_by: str = "") -> Dict:
    """
    Mark a coil as rolled/skipped/on-hold and update shift totals.
    Returns updated shift dict (caller must call save_shift).
    """
    now = datetime.utcnow().isoformat()
    for c in shift["coils"]:
        if c["coil_number"] == coil_number:
            old_status = c["status"]
            c["status"]             = status
            c["confirmed_at"]       = now
            c["confirmed_by"]       = confirmed_by
            c["notes"]              = notes
            c["actual_weight"]      = actual_weight or c["weight"]
            c["thickness_achieved"] = thickness_achieved
            break

    # Recompute shift totals
    shift["mt_rolled"]   = round(sum(
        c["actual_weight"] or c["weight"]
        for c in shift["coils"] if c["status"] == "ROLLED"), 2)
    shift["coils_rolled"]  = sum(1 for c in shift["coils"] if c["status"] == "ROLLED")
    shift["coils_skipped"] = sum(1 for c in shift["coils"] if c["status"] == "SKIPPED")
    shift["coils_on_hold"] = sum(1 for c in shift["coils"] if c["status"] == "ON_HOLD")

    if not shift.get("started_at"):
        shift["started_at"] = now

    return shift


def get_shift_analytics(shifts: List[Dict]) -> Dict:
    """Compute analytics across multiple shift records."""
    if not shifts:
        return {}

    total_mt     = sum(s.get("mt_rolled", 0) for s in shifts)
    total_target = sum(s.get("mt_target", 0) for s in shifts)
    total_coils  = sum(s.get("coils_rolled", 0) for s in shifts)
    total_planned= sum(s.get("total_coils", 0) for s in shifts)

    # Roll type breakdown
    roll_mt = {}
    for s in shifts:
        rt = s.get("roll_type", "UNKNOWN")
        roll_mt[rt] = roll_mt.get(rt, 0) + s.get("mt_rolled", 0)

    # Section breakdown
    section_mt = {}
    for s in shifts:
        for c in s.get("coils", []):
            if c.get("status") == "ROLLED":
                sk = c.get("section_key", "UNKNOWN")
                section_mt[sk] = section_mt.get(sk, 0) + (c.get("actual_weight") or 0)

    return {
        "total_mt_rolled":  round(total_mt, 1),
        "total_mt_target":  round(total_target, 1),
        "plan_adherence":   round(total_mt / max(total_target, 0.01) * 100, 1),
        "coils_rolled":     total_coils,
        "coils_planned":    total_planned,
        "coil_adherence":   round(total_coils / max(total_planned, 1) * 100, 1),
        "roll_type_mt":     {k: round(v, 1) for k, v in roll_mt.items()},
        "section_mt":       {k: round(v, 1) for k, v in section_mt.items()},
        "n_shifts":         len(shifts),
    }
