"""
crm/config.py — Single source of truth for all planning constants.
Tata Steel CRM Sahibabad · Narrow Complex
"""
from __future__ import annotations

# ══════════════════════════════════════════════════════════════════════════════
# CONSUMERS — downstream demand destinations
# ══════════════════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════════════════
# CR SLITTER SCOPE FILTER  (Requirements 1 & 5)
# Only these storage locations belong to our planning scope.
# RC14, RC11, NC14, R027 etc. are outside scope.
# ══════════════════════════════════════════════════════════════════════════════
CRS_SCOPE_LOCATIONS = {"RNM6", "R032", "R033"}


CONSUMERS = {
    "TUBE": {
        "label": "Tube Plant", "daily_mt": 210.0, "icon": "🔵",
        "target_days": 1.5, "warn_days": 1.0, "crit_days": 0.5,
        "via_crs": True,
    },
    "OEM": {
        "label": "OEM", "daily_mt": 50.0, "icon": "🟣",
        "target_days": 1.5, "warn_days": 1.0, "crit_days": 0.5,
        "via_crs": True,
    },
    "H&T": {
        "label": "H&T Line", "daily_mt": 35.0, "icon": "🟠",
        "target_days": 2.5, "warn_days": 1.5, "crit_days": 0.75,
        "via_crs": False,          # H&T goes direct from mill — no CRS
    },
}

# ══════════════════════════════════════════════════════════════════════════════
# PIPELINE STAGES — ordered, with nominal daily throughput & lead time
# ══════════════════════════════════════════════════════════════════════════════
STAGES = {
    "PICKLING":             {"order": 1,  "label": "Pickling",      "cap_mt": 250, "lead_days": 1},
    "HR SLITTER":           {"order": 2,  "label": "HR Slitter",    "cap_mt": 150, "lead_days": 1},
    "ROLLING MILL":         {"order": 3,  "label": "Rolling Mill",  "cap_mt": 290, "lead_days": 1},
    "ANB":                  {"order": 4,  "label": "ANB (Anneal Base)", "cap_mt": 200, "lead_days": 3},
    "ANNEALING":            {"order": 5,  "label": "Annealing",     "cap_mt": 200, "lead_days": 3},
    "FURNACE":              {"order": 6,  "label": "H&T Furnace",   "cap_mt": 40,  "lead_days": 2.5},
    "SPM":                  {"order": 7,  "label": "Skin Pass",     "cap_mt": 80,  "lead_days": 1},
    "REWINDING":            {"order": 8,  "label": "Rewinding",     "cap_mt": 120, "lead_days": 1},
    "C R SLITTER":          {"order": 9,  "label": "CR Slitter",    "cap_mt": 260, "lead_days": 1},
    "GRINDING":             {"order": 10, "label": "Grinding",      "cap_mt": 60,  "lead_days": 1},
    "EDGE ROUNDING":        {"order": 11, "label": "Edge Rounding", "cap_mt": 40,  "lead_days": 1},
    "COLOR TEMPERING":      {"order": 12, "label": "Colour Temper", "cap_mt": 50,  "lead_days": 1},
    "INSPECTION TABLE/CTL": {"order": 13, "label": "Inspection/CTL","cap_mt": 300, "lead_days": 1},
    "PALLETIZATION":        {"order": 14, "label": "Palletization", "cap_mt": 200, "lead_days": 0.5},
    "PACK":                 {"order": 15, "label": "Packing",       "cap_mt": 200, "lead_days": 0.5},
    "PENDING FOR PLAN":     {"order": 0,  "label": "Pending Plan",  "cap_mt": 0,   "lead_days": 99},
    "NC":                   {"order": 0,  "label": "Non-Conforming","cap_mt": 0,   "lead_days": 99},
}

# The stages the planner actively monitors (per Mill Planning Guidelines §1)
CORE_STAGES = ["C R SLITTER", "ROLLING MILL", "ANNEALING", "ANB",
               "REWINDING", "FURNACE", "PICKLING", "SPM"]

# ══════════════════════════════════════════════════════════════════════════════
# SECTION → CONSUMER / ROLL / CAPACITY-TYPE
# ══════════════════════════════════════════════════════════════════════════════
SECTION_CONSUMER = {
    "HT_FINISH":              "H&T",
    "TUBE_FH":                "TUBE",
    "FIRST_ROLLING":          "TUBE",   # feeds anneal → CRS → Tube
    "RE_ROLLING":             "TUBE",
    "CRCA_FINISH":            "OEM",
    "CRCA_FINISH_CRM06":      "OEM",
    "SKIN_PASS_SUPER_BRIGHT": "OEM",
    "SKIN_PASS_CHROME":       "OEM",
    "SKIN_PASS_HEAVY_MATT":   "OEM",
    "ROLLING_BRIGHT":         "OEM",
    "ROLLING":                "OEM",
}

SECTION_ROLL = {
    "FIRST_ROLLING":          "Light Matt",
    "RE_ROLLING":             "Light Matt",
    "ROLLING":                "Light Matt",
    "HT_FINISH":              "Bright",
    "TUBE_FH":                "Bright",
    "CRCA_FINISH":            "Bright",
    "CRCA_FINISH_CRM06":      "Bright",
    "SKIN_PASS_SUPER_BRIGHT": "Super Bright",
    "SKIN_PASS_CHROME":       "Chrome Plated",
    "ROLLING_BRIGHT":         "Chrome Plated",
    "SKIN_PASS_HEAVY_MATT":   "Heavy Matt",
}

# Rolling "type" drives both capacity and roll life
def section_type(section_key: str) -> str:
    if section_key == "FIRST_ROLLING":            return "first_rolling"
    if section_key in ("RE_ROLLING", "ROLLING"):  return "re_rolling"
    return "finishing"

ROLL_TYPES = ["Light Matt", "Bright", "Super Bright", "Chrome Plated", "Heavy Matt"]

# ── Roll life (MT before dressing) — confirmed by planner ─────────────────────
ROLL_LIFE = {
    ("Light Matt",    "first_rolling", "H&T"):   200,
    ("Light Matt",    "first_rolling", "OTHER"): 100,
    ("Light Matt",    "re_rolling",    "*"):     100,
    ("Light Matt",    "finishing",     "*"):     100,
    ("Bright",        "finishing",     "*"):     100,
    ("Super Bright",  "finishing",     "*"):     300,
    ("Chrome Plated", "finishing",     "*"):     300,
    ("Heavy Matt",    "finishing",     "*"):     200,
}

def roll_life(roll: str, section_key: str, consumer: str = "OTHER") -> int:
    st = section_type(section_key)
    if st == "first_rolling":
        key = (roll, st, "H&T" if consumer == "H&T" else "OTHER")
        if key in ROLL_LIFE: return ROLL_LIFE[key]
    for k in ((roll, st, "*"), (roll, "finishing", "*")):
        if k in ROLL_LIFE: return ROLL_LIFE[k]
    return 100

ROLL_CHANGE_MIN = 45          # minutes lost per roll change
SHIFT_MINUTES   = 480         # 8-hour shift

# ── Mill shift capacity (MT) by rolling type — confirmed by planner ──────────
SHIFT_CAPACITY = {
    "CRM04": {"first_rolling":   0, "re_rolling": 80, "finishing": 50},
    "CRM06": {"first_rolling": 120, "re_rolling": 95, "finishing": 60},
}

# ══════════════════════════════════════════════════════════════════════════════
# AGING BANDS (Guideline §3)
# ══════════════════════════════════════════════════════════════════════════════
AGE_BANDS = [
    (30, 100, "🔴 >30d"),
    (21,  85, "🔴 21-30d"),
    (14,  70, "🟠 14-21d"),
    (7,   45, "🟡 7-14d"),
    (3,   25, "🟢 3-7d"),
    (0,   10, "🟢 <3d"),
]

def age_score(days: float) -> float:
    for threshold, score, _ in AGE_BANDS:
        if days >= threshold: return float(score)
    return 10.0

def age_band(days: float) -> str:
    for threshold, _, label in AGE_BANDS:
        if days >= threshold: return label
    return "🟢 <3d"

# ══════════════════════════════════════════════════════════════════════════════
# QUALITY RISK (Guideline §7)
# ══════════════════════════════════════════════════════════════════════════════
SURFACE_CRITICAL   = {"BRIGHT", "MATT", "M"}     # surface-sensitive finishes
TIGHT_TOL_UM       = 40                           # ≤40 µm band = thickness critical
OEM_CRITICAL_CUST  = ["L.G BALAKRISHNAN", "SFC SOLUTIONS", "CALLIDA",
                      "TMA INTERNATIONAL"]

# ══════════════════════════════════════════════════════════════════════════════
# SCORING WEIGHTS per planning mode (Guideline §2 + §3 + §4)
# ══════════════════════════════════════════════════════════════════════════════
MODES = {
    "BALANCED":      "Balanced — protect every stage & consumer",
    "TUBE_PRIORITY": "Tube Priority — Tube Plant demand first",
    "OEM_PRIORITY":  "OEM Priority — OEM dispatch first",
    "HT_PRIORITY":   "H&T Priority — H&T Line first",
    "CLEAR_AGING":   "Clear Aging — oldest WIP first",
    "MAX_THROUGHPUT":"Max Throughput — highest MT/shift",
}

# factors: starvation, demand, aging, pipeline, quality, throughput, continuity
MODE_WEIGHTS = {
    "BALANCED":       {"starvation":.28,"demand":.20,"aging":.18,"pipeline":.14,"quality":.06,"throughput":.08,"continuity":.06},
    "TUBE_PRIORITY":  {"starvation":.25,"demand":.38,"aging":.12,"pipeline":.12,"quality":.04,"throughput":.05,"continuity":.04},
    "OEM_PRIORITY":   {"starvation":.25,"demand":.38,"aging":.12,"pipeline":.12,"quality":.04,"throughput":.05,"continuity":.04},
    "HT_PRIORITY":    {"starvation":.25,"demand":.38,"aging":.12,"pipeline":.12,"quality":.04,"throughput":.05,"continuity":.04},
    "CLEAR_AGING":    {"starvation":.15,"demand":.10,"aging":.52,"pipeline":.08,"quality":.04,"throughput":.05,"continuity":.06},
    "MAX_THROUGHPUT": {"starvation":.10,"demand":.10,"aging":.05,"pipeline":.05,"quality":.03,"throughput":.55,"continuity":.12},
}
MODE_BOOST_CONSUMER = {
    "TUBE_PRIORITY": "TUBE", "OEM_PRIORITY": "OEM", "HT_PRIORITY": "H&T",
}

# ── Health thresholds (Guideline §4) ─────────────────────────────────────────
HEALTH = {
    "starved_days":   1.0,     # < 1 day cover → 🔴 Critical
    "attention_days": 2.0,     # < 2 days      → 🟡 Attention
    "overload_days":  7.0,     # > 7 days      → 🟡 Excess inventory
}
