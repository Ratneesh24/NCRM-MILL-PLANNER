"""
crm/twin.py — Digital Twin: Forward Pipeline Simulation  (Guideline §8)
=======================================================================
Moves planning from reactive ("what is the WIP?") to proactive
("what will the pipeline look like if today's plan is executed?").

Simulates material flowing stage → stage over N days, applying each
stage's throughput capacity, and predicts:
  • starvation dates
  • bottleneck build-up
  • dispatch readiness per consumer
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, List, Optional

import pandas as pd

from . import config as C
from .campaign import MillPlan


# Simplified downstream chain used by the simulator
CHAIN = {
    "TUBE": ["ROLLING MILL", "ANB", "C R SLITTER", "DISPATCH"],
    "OEM":  ["ROLLING MILL", "ANB", "C R SLITTER", "DISPATCH"],
    "H&T":  ["ROLLING MILL", "FURNACE", "DISPATCH"],
}
# Lead time (days) to traverse each stage
TRANSIT = {"ROLLING MILL": 1, "ANB": 3, "FURNACE": 2.5,
           "C R SLITTER": 1, "DISPATCH": 0}


@dataclass
class TwinResult:
    horizon:        int
    dates:          List[str]
    consumer_buf:   Dict[str, List[float]]      # buffer MT per day
    stage_wip:      Dict[str, List[float]]      # WIP MT per day
    starvation:     Dict[str, Optional[str]]    # consumer → date
    bottlenecks:    List[str]
    dispatch_ready: Dict[str, List[float]]      # MT arriving at consumer/day


def simulate(
    df:            pd.DataFrame,
    plans:         Optional[Dict[str, MillPlan]] = None,
    horizon:       int = 7,
    overrides:     Optional[Dict[str, float]] = None,
    repeat_plan:   bool = True,
) -> TwinResult:
    """
    df       : full WIP pipeline (from pipeline.load_pipeline)
    plans    : today's chosen MillPlan per mill (adds new material)
    horizon  : days to project
    repeat_plan : assume a similar plan runs every day (steady state)
    """
    today = date.today()
    dates = [str(today + timedelta(days=d)) for d in range(horizon + 1)]

    # ── Starting WIP per (consumer, stage) ─────────────────────────────────
    wip: Dict[str, Dict[str, float]] = {}
    for cons in C.CONSUMERS:
        wip[cons] = {}
        for stg in set(sum(CHAIN.values(), [])):
            wip[cons][stg] = float(
                df[(df["consumer"] == cons) & (df["stage"] == stg)]["mt"].sum())

    # H&T buffer lives at FURNACE; TUBE/OEM at C R SLITTER
    def buffer_of(cons: str) -> float:
        return wip[cons]["FURNACE"] if cons == "H&T" \
               else wip[cons]["C R SLITTER"]

    # ── What today's plan injects into ROLLING MILL output ────────────────
    inject: Dict[str, float] = {c: 0.0 for c in C.CONSUMERS}
    if plans:
        for mp in plans.values():
            for camp in mp.campaigns:
                for c in camp.coils:
                    sk   = c["section"]
                    cons = C.SECTION_CONSUMER.get(sk, "OEM")
                    inject[cons] += c["mt"]

    consumer_buf   = {c: [] for c in C.CONSUMERS}
    dispatch_ready = {c: [] for c in C.CONSUMERS}
    stage_wip      = {s: [] for s in ("ROLLING MILL", "ANB",
                                       "FURNACE", "C R SLITTER")}
    starvation: Dict[str, Optional[str]] = {c: None for c in C.CONSUMERS}

    # In-transit pipeline: list of (arrive_day, consumer, stage, mt)
    transit: List[tuple] = []

    for day in range(horizon + 1):
        # 1. Rolled material enters the chain (day 0 = today's plan)
        if day == 0 or repeat_plan:
            for cons, mt in inject.items():
                if mt <= 0:
                    continue
                nxt = "FURNACE" if cons == "H&T" else "ANB"
                lead = TRANSIT["ROLLING MILL"] + TRANSIT[nxt]
                transit.append((day + lead, cons,
                                "FURNACE" if cons == "H&T"
                                else "C R SLITTER", mt))

        # 2. Existing ANB / ROLLING WIP flows down over its lead time
        if day == 0:
            for cons in C.CONSUMERS:
                anb = wip[cons].get("ANB", 0.0)
                if anb > 0:
                    transit.append((day + TRANSIT["ANB"], cons,
                                    "C R SLITTER", anb))
                    wip[cons]["ANB"] = 0.0
                rm = wip[cons].get("ROLLING MILL", 0.0)
                if rm > 0:
                    tgt  = "FURNACE" if cons == "H&T" else "C R SLITTER"
                    lead = TRANSIT["ROLLING MILL"] + \
                           (TRANSIT["FURNACE"] if cons == "H&T"
                            else TRANSIT["ANB"])
                    transit.append((day + lead, cons, tgt, rm))
                    wip[cons]["ROLLING MILL"] = 0.0

        # 3. Arrivals land in the buffer
        arrived = {c: 0.0 for c in C.CONSUMERS}
        for t in [x for x in transit if int(x[0]) == day]:
            _, cons, stg, mt = t
            wip[cons][stg] = wip[cons].get(stg, 0.0) + mt
            arrived[cons] += mt
        transit = [x for x in transit if int(x[0]) > day]

        # 4. Consumers eat their daily ask
        for cons, cfg in C.CONSUMERS.items():
            rate = (overrides or {}).get(cons, cfg["daily_mt"])
            stg  = "FURNACE" if cons == "H&T" else "C R SLITTER"
            if day > 0:
                wip[cons][stg] = max(wip[cons][stg] - rate, 0.0)
            buf = round(wip[cons][stg], 1)
            consumer_buf[cons].append(buf)
            dispatch_ready[cons].append(round(arrived[cons], 1))
            if buf <= 0 and starvation[cons] is None and day > 0:
                starvation[cons] = dates[day]

        for stg in stage_wip:
            stage_wip[stg].append(
                round(sum(wip[c].get(stg, 0.0) for c in C.CONSUMERS), 1))

    # ── Bottleneck detection ──────────────────────────────────────────────
    bottlenecks: List[str] = []
    for stg, series in stage_wip.items():
        cap = C.STAGES.get(stg, {}).get("cap_mt", 100)
        if series and series[-1] > cap * C.HEALTH["overload_days"]:
            bottlenecks.append(
                f"🟠 {C.STAGES.get(stg,{}).get('label',stg)} builds to "
                f"{series[-1]:.0f} MT by {dates[-1]} — congestion risk")
        if series and series[-1] < cap * 0.5 and series[0] > series[-1]:
            bottlenecks.append(
                f"🔴 {C.STAGES.get(stg,{}).get('label',stg)} drains to "
                f"{series[-1]:.0f} MT by {dates[-1]} — starvation risk")

    for cons, dt in starvation.items():
        if dt:
            bottlenecks.append(
                f"🔴 {C.CONSUMERS[cons]['label']} starves on {dt}")

    return TwinResult(
        horizon=horizon, dates=dates, consumer_buf=consumer_buf,
        stage_wip=stage_wip, starvation=starvation,
        bottlenecks=bottlenecks, dispatch_ready=dispatch_ready,
    )


def twin_frame(t: TwinResult) -> pd.DataFrame:
    """Consumer buffer projection as a chart-ready DataFrame."""
    data = {C.CONSUMERS[c]["label"]: t.consumer_buf[c] for c in t.consumer_buf}
    return pd.DataFrame(data, index=t.dates)


def stage_frame(t: TwinResult) -> pd.DataFrame:
    data = {C.STAGES.get(s, {}).get("label", s): t.stage_wip[s]
            for s in t.stage_wip}
    return pd.DataFrame(data, index=t.dates)
