"""
crm/health.py — Stage Health Index & Consumer Coverage  (Guideline §4)
======================================================================
For every stage and every consumer:
    current inventory · daily consumption · days of cover
    expected starvation date · overload flag · RAG status
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional

import pandas as pd

from . import config as C


# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class Health:
    name:            str
    label:           str
    inventory_mt:    float
    coils:           int
    daily_rate:      float
    days_cover:      float
    status:          str        # CRITICAL / ATTENTION / HEALTHY / EXCESS
    icon:            str
    starvation_date: Optional[str] = None
    overload:        bool  = False
    shortfall_mt:    float = 0.0   # MT needed to reach target cover
    excess_mt:       float = 0.0   # MT above overload threshold
    avg_age:         float = 0.0
    stuck_coils:     int   = 0
    note:            str   = ""
    # FIX 4: coil-level detail for alert drill-down
    coil_detail:     Optional[object] = None  # pd.DataFrame slice


_ICON = {"CRITICAL": "🔴", "ATTENTION": "🟡",
         "HEALTHY": "🟢", "EXCESS": "🟠"}


def _classify(days: float, target: float) -> str:
    if days < C.HEALTH["starved_days"]:    return "CRITICAL"
    if days < C.HEALTH["attention_days"]:  return "ATTENTION"
    if days > C.HEALTH["overload_days"]:   return "EXCESS"
    return "HEALTHY"


# ══════════════════════════════════════════════════════════════════════════════
# CONSUMER HEALTH  — the demand side
# ══════════════════════════════════════════════════════════════════════════════
def consumer_health(df: pd.DataFrame,
                    plan_feed: Optional[Dict[str, float]] = None,
                    overrides: Optional[Dict[str, float]] = None,
                    ) -> Dict[str, Health]:
    """
    Buffer for each consumer = material sitting at the stage that feeds it.
      H&T  → FURNACE  (direct, no CRS)
      TUBE → C R SLITTER (TUBE-classified)
      OEM  → C R SLITTER (OEM-classified)
    `plan_feed` = MT today's rolling plan adds to each consumer.
    """
    plan_feed = plan_feed or {}
    today     = date.today()
    out: Dict[str, Health] = {}

    for key, cfg in C.CONSUMERS.items():
        rate = (overrides or {}).get(key, cfg["daily_mt"])
        buf_stage = "FURNACE" if key == "H&T" else "C R SLITTER"
        if key == "H&T":
            # H&T buffer = FURNACE, no storage filter
            d = df[(df["stage"] == buf_stage) & (df["consumer"] == key)]
        else:
            # FIX 5: CRS buffer = only storage RNM6/R032/R033 (in_scope_crs)
            d = df[df["in_scope_crs"] & (df["consumer"] == key)]

        inv    = round(float(d["mt"].sum()), 1)
        feed   = round(float(plan_feed.get(key, 0.0)), 1)
        total  = inv + feed
        days   = round(total / rate, 2) if rate else 99.0
        status = _classify(days, cfg["target_days"])

        starve = None
        if days < 7:
            starve = str(today + timedelta(days=int(days)))

        short  = max(0.0, round(cfg["target_days"] * rate - total, 1))
        excess = max(0.0, round(total - C.HEALTH["overload_days"] * rate, 1))

        out[key] = Health(
            name=key, label=cfg["label"], inventory_mt=inv, coils=len(d),
            daily_rate=rate, days_cover=days, status=status,
            icon=_ICON[status], starvation_date=starve,
            overload=(status == "EXCESS"), shortfall_mt=short,
            excess_mt=excess,
            avg_age=round(float(d["coil_age"].mean()), 1) if len(d) else 0.0,
            stuck_coils=int((d["stage_age"] > 21).sum()),
            note=(f"Plan adds {feed:.0f} MT today" if feed else ""),
            coil_detail=d.copy() if len(d) else None,
        )
    return out


# ══════════════════════════════════════════════════════════════════════════════
# STAGE HEALTH — the process side
# ══════════════════════════════════════════════════════════════════════════════
def stage_health(df: pd.DataFrame,
                 stages: Optional[List[str]] = None) -> Dict[str, Health]:
    """
    Health of every process stage.
    Consumption rate = the stage's own daily throughput capacity.
    Days of cover    = how long the stage can keep running on current WIP.
    Low cover = starvation risk. High cover = overload / excess WIP.
    """
    stages = stages or C.CORE_STAGES
    today  = date.today()
    out: Dict[str, Health] = {}

    for s in stages:
        cfg  = C.STAGES.get(s, {"label": s, "cap_mt": 100, "order": 99})
        rate = float(cfg.get("cap_mt", 100)) or 1.0
        d    = df[df["stage"] == s]

        inv    = round(float(d["mt"].sum()), 1)
        days   = round(inv / rate, 2)
        status = _classify(days, 2.0)

        starve = str(today + timedelta(days=int(days))) if days < 7 else None
        short  = max(0.0, round(C.HEALTH["attention_days"] * rate - inv, 1))
        excess = max(0.0, round(inv - C.HEALTH["overload_days"] * rate, 1))

        out[s] = Health(
            name=s, label=cfg.get("label", s), inventory_mt=inv,
            coils=len(d), daily_rate=rate, days_cover=days,
            status=status, icon=_ICON[status], starvation_date=starve,
            overload=(status == "EXCESS"), shortfall_mt=short,
            excess_mt=excess,
            avg_age=round(float(d["coil_age"].mean()), 1) if len(d) else 0.0,
            stuck_coils=int((d["stage_age"] > 21).sum()),
            coil_detail=d.copy() if len(d) else None,
        )
    return out


def health_table(h: Dict[str, Health]) -> pd.DataFrame:
    return pd.DataFrame([{
        "": x.icon,
        "Stage / Consumer": x.label,
        "Inventory MT":     x.inventory_mt,
        "Coils":            x.coils,
        "Daily rate MT":    x.daily_rate,
        "Days cover":       x.days_cover,
        "Status":           x.status,
        "Starves on":       x.starvation_date or "—",
        "Shortfall MT":     x.shortfall_mt or "—",
        "Excess MT":        x.excess_mt or "—",
        "Avg age (d)":      x.avg_age,
        "Stuck >21d":       x.stuck_coils,
    } for x in h.values()])


# ── Detail columns shown in alert drill-down ─────────────────────────────────
ALERT_COIL_COLS = [
    "coil", "customer", "consumer", "mt", "stage", "storage",
    "coil_age", "stage_age", "thick", "width", "rt",
    "quality", "qual_flags", "age_band",
]


def _coil_table(df, sort_col="coil_age") -> Optional["pd.DataFrame"]:
    """Subset and sort coil detail for alert display."""
    import pandas as pd
    if df is None or len(df) == 0:
        return None
    cols = [c for c in ALERT_COIL_COLS if c in df.columns]
    return (df[cols].sort_values(sort_col, ascending=False)
              .round({"mt": 3, "coil_age": 0, "stage_age": 0,
                      "thick": 2, "width": 0})
              .reset_index(drop=True))


def alerts(consumer_h: Dict[str, Health],
           stage_h:    Dict[str, Health]) -> List[dict]:
    """
    Ranked alert list. Each alert is a dict with:
      msg       : human-readable alert string
      level     : CRITICAL / ATTENTION / EXCESS / STUCK
      coil_df   : pd.DataFrame of responsible coils (or None)
    """
    a: List[dict] = []
    for x in consumer_h.values():
        coils_sorted = _coil_table(x.coil_detail)
        if x.status == "CRITICAL":
            a.append({"level": "CRITICAL", "coil_df": coils_sorted,
                "msg": (f"🔴 {x.label} CRITICAL — {x.days_cover:.1f}d cover, "
                        f"starves {x.starvation_date}. "
                        f"Roll {x.shortfall_mt:.0f} MT today.")})
        elif x.status == "ATTENTION":
            a.append({"level": "ATTENTION", "coil_df": coils_sorted,
                "msg": (f"🟡 {x.label} low — {x.days_cover:.1f}d cover. "
                        f"Needs {x.shortfall_mt:.0f} MT.")})
        elif x.status == "EXCESS":
            a.append({"level": "EXCESS", "coil_df": coils_sorted,
                "msg": (f"🟠 {x.label} overloaded — {x.days_cover:.1f}d cover, "
                        f"{x.excess_mt:.0f} MT excess. Ease off.")})

    for x in stage_h.values():
        coils_sorted = _coil_table(x.coil_detail)
        if x.status == "CRITICAL" and x.inventory_mt > 0:
            a.append({"level": "CRITICAL", "coil_df": coils_sorted,
                "msg": (f"🔴 {x.label} starving — only {x.days_cover:.1f}d WIP.")})
        elif x.status == "EXCESS":
            a.append({"level": "EXCESS", "coil_df": coils_sorted,
                "msg": (f"🟠 {x.label} congested — {x.inventory_mt:.0f} MT "
                        f"({x.days_cover:.1f}d). Bottleneck risk.")})
        if x.stuck_coils >= 5:
            stuck = _coil_table(
                x.coil_detail[x.coil_detail["stage_age"] >= 21]
                if x.coil_detail is not None else None)
            a.append({"level": "STUCK", "coil_df": stuck,
                "msg": (f"⏰ {x.label}: {x.stuck_coils} coils stuck >21 days.")})
    return a
