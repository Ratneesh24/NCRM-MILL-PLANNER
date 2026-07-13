"""
crm/scoring.py — Explainable Multi-Factor Priority Engine
=========================================================
Guideline §2 (demand vs WIP) · §3 (aging) · §4 (stage health)
          · §7 (quality risk) · §10 (explainable output)

Seven factors, each 0-100, weighted per planning mode:
  starvation  — is the consumer this section feeds about to starve?
  demand      — share of daily demand this consumer represents
  aging       — how old is the material in this section?
  pipeline    — does rolling this protect a downstream stage?
  quality     — quality-critical material gets stable-condition priority
  throughput  — MT/shift this section can deliver
  continuity  — is it already on the mounted roll? (no changeover)
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pandas as pd

from . import config as C
from .health import Health


# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class SectionScore:
    section_key:  str
    mill:         str
    label:        str
    consumer:     str
    roll_type:    str
    sec_type:     str
    n_coils:      int
    total_mt:     float
    avg_age:      float
    max_age:      float
    qual_risk:    float
    roll_life_mt: int
    shift_cap_mt: float
    coils_df:     object = None

    # factors
    f_starvation: float = 0.0
    f_demand:     float = 0.0
    f_aging:      float = 0.0
    f_pipeline:   float = 0.0
    f_quality:    float = 0.0
    f_throughput: float = 0.0
    f_continuity: float = 0.0
    score:        float = 0.0
    rank:         int   = 0

    reasons:      List[str] = field(default_factory=list)
    warnings:     List[str] = field(default_factory=list)

    @property
    def explanation(self) -> str:
        return (f"**{self.section_key.replace('_',' ').title()}** "
                f"scored **{self.score:.0f}/100** — "
                + ("; ".join(self.reasons) if self.reasons
                   else "no dominant driver"))


# ══════════════════════════════════════════════════════════════════════════════
def score_sections(
    sections:      List[Dict],           # from generator.build_sections()
    consumer_h:    Dict[str, Health],
    stage_h:       Dict[str, Health],
    mode:          str,
    current_rolls: Dict[str, str],       # {"CRM04": "Bright", ...}
    auto_drop:     bool = True,
) -> List[SectionScore]:
    """
    Score every candidate section. Ranks are per-mill.
    auto_drop: if the boosted consumer is already healthy, the boost
               transfers to the most starved consumer instead.
    """
    weights = dict(C.MODE_WEIGHTS.get(mode, C.MODE_WEIGHTS["BALANCED"]))
    boost   = C.MODE_BOOST_CONSUMER.get(mode)

    # ── Auto-drop: don't force-feed a consumer that is already covered ──────
    if auto_drop and boost:
        bh = consumer_h.get(boost)
        if bh and bh.status in ("HEALTHY", "EXCESS"):
            starved = [h for h in consumer_h.values()
                       if h.status in ("CRITICAL", "ATTENTION")]
            if starved:
                boost = min(starved, key=lambda h: h.days_cover).name

    total_demand = sum(h.daily_rate for h in consumer_h.values()) or 1.0
    max_speed    = max(max(v.values()) for v in C.SHIFT_CAPACITY.values())

    scored: List[SectionScore] = []
    for s in sections:
        sk       = s["section_key"]
        mill     = s["mill"]
        df       = s["coils_df"]
        consumer = C.SECTION_CONSUMER.get(sk, "OEM")
        roll     = C.SECTION_ROLL.get(sk, "Light Matt")
        stype    = C.section_type(sk)
        life     = C.roll_life(roll, sk, consumer)
        cap      = C.SHIFT_CAPACITY.get(mill, {}).get(stype, 60)

        mt       = round(float(df["mt"].sum()), 2) if "mt" in df.columns \
                   else round(float(df["Input Coil Weight"].sum()), 2)
        ages     = df["coil_age"] if "coil_age" in df.columns \
                   else df.get("Coil Age(# Days)", pd.Series([0]))
        avg_age  = float(pd.to_numeric(ages, errors="coerce").fillna(0).mean())
        max_age  = float(pd.to_numeric(ages, errors="coerce").fillna(0).max())
        qrisk    = float(df["qual_risk"].mean()) if "qual_risk" in df.columns else 0.0

        sc = SectionScore(
            section_key=sk, mill=mill, label=s["label"], consumer=consumer,
            roll_type=roll, sec_type=stype, n_coils=len(df), total_mt=mt,
            avg_age=round(avg_age, 1), max_age=round(max_age, 1),
            qual_risk=round(qrisk, 1), roll_life_mt=life, shift_cap_mt=cap,
            coils_df=df,
        )

        ch = consumer_h.get(consumer)

        # ── F1 Starvation (§4) ─────────────────────────────────────────────
        sc.f_starvation = {"CRITICAL": 100, "ATTENTION": 70,
                           "HEALTHY": 25, "EXCESS": 5}.get(
                               ch.status if ch else "HEALTHY", 25)
        if ch and ch.status == "CRITICAL":
            sc.reasons.append(f"{ch.label} starves {ch.starvation_date}")
            sc.warnings.append(f"🔴 {ch.label} only {ch.days_cover:.1f}d cover")
        elif ch and ch.status == "ATTENTION":
            sc.reasons.append(f"{ch.label} low ({ch.days_cover:.1f}d)")

        # ── F2 Demand share (§2) ───────────────────────────────────────────
        sc.f_demand = round((ch.daily_rate / total_demand * 100)
                            if ch else 20.0, 1)
        if boost and consumer == boost:
            sc.f_demand = min(100.0, sc.f_demand + 45)
            sc.reasons.append(f"{mode.replace('_',' ').lower()} boost")

        # ── F3 Aging (§3) ──────────────────────────────────────────────────
        sc.f_aging = C.age_score(max_age)
        if max_age >= 21:
            sc.reasons.append(f"oldest coil {max_age:.0f}d")
            sc.warnings.append(f"⏰ {max_age:.0f}-day coil — rotate now")

        # ── F4 Pipeline protection (§2) ────────────────────────────────────
        # Does rolling this section relieve a starving downstream stage?
        nxt = {"first_rolling": "ANB", "re_rolling": "ANB",
               "finishing": "C R SLITTER"}.get(stype, "C R SLITTER")
        if consumer == "H&T":
            nxt = "FURNACE"
        nh = stage_h.get(nxt)
        if nh and nh.status == "CRITICAL":
            sc.f_pipeline = 100.0
            sc.reasons.append(f"{nh.label} starving")
        elif nh and nh.status == "ATTENTION":
            sc.f_pipeline = 70.0
        elif nh and nh.status == "EXCESS":
            sc.f_pipeline = 10.0
            sc.warnings.append(f"🟠 {nh.label} already congested "
                               f"({nh.days_cover:.1f}d)")
        else:
            sc.f_pipeline = 40.0

        # ── F5 Quality risk (§7) ───────────────────────────────────────────
        sc.f_quality = qrisk
        if qrisk >= 60:
            sc.reasons.append("quality-critical material")
            sc.warnings.append("⚠️ Quality-critical — roll under stable "
                               "conditions, avoid after roll change")

        # ── F6 Throughput (§5) ─────────────────────────────────────────────
        sc.f_throughput = round(cap / max_speed * 100, 1)

        # ── F7 Roll continuity (§6) ────────────────────────────────────────
        if roll == current_rolls.get(mill):
            sc.f_continuity = 100.0
            sc.reasons.append("no roll change needed")
        else:
            sc.f_continuity = 0.0

        # ── Weighted total ─────────────────────────────────────────────────
        sc.score = round(
            sc.f_starvation * weights["starvation"] +
            sc.f_demand     * weights["demand"] +
            sc.f_aging      * weights["aging"] +
            sc.f_pipeline   * weights["pipeline"] +
            sc.f_quality    * weights["quality"] +
            sc.f_throughput * weights["throughput"] +
            sc.f_continuity * weights["continuity"], 1)

        scored.append(sc)

    # Rank per mill
    for mill in ("CRM04", "CRM06"):
        ms = sorted([x for x in scored if x.mill == mill],
                    key=lambda x: -x.score)
        for i, x in enumerate(ms, 1):
            x.rank = i

    return scored


def factor_table(sc: SectionScore) -> pd.DataFrame:
    return pd.DataFrame([
        {"Factor": "Starvation risk",  "Score": sc.f_starvation},
        {"Factor": "Demand share",     "Score": sc.f_demand},
        {"Factor": "Coil aging",       "Score": sc.f_aging},
        {"Factor": "Pipeline protect", "Score": sc.f_pipeline},
        {"Factor": "Quality risk",     "Score": sc.f_quality},
        {"Factor": "Throughput",       "Score": sc.f_throughput},
        {"Factor": "Roll continuity",  "Score": sc.f_continuity},
    ])
