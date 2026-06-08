# Tata Steel CRM Sahibabad — Narrow Complex Mill Planner

An intelligent, self-learning daily rolling mill plan generator for the Narrow Complex at Tata Steel CRM Sahibabad. Converts a daily WIP (Work-In-Progress) coil staging export from SAP into a fully formatted, section-wise rolling plan — matching the manually prepared format exactly — and improves accuracy every day through machine learning and planner corrections.

---

## Live App

**[🚀 Open the app →]()**

---

## What it does

Upload today's WIP file from SAP → the system generates a complete rolling plan in seconds:

- **100 coils / 843 MT** matched against actual planner output
- **96.6%+ inclusion accuracy** — right coils, right sections
- **Width cascade enforced** — widest coils first within every section
- **Priority block generated** — numbered CRM-04 and CRM-06 at the bottom
- **Self-improving** — every planner correction teaches the model daily

---

## System Architecture

```
SAP WIP Export (.xlsx)
        │
        ▼
┌─────────────────────────────────────────────────────┐
│                   FILTER ENGINE                     │
│  Current Stage = ROLLING MILL only                  │
│  Applies 8 inclusion/exclusion rules                │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│               HYBRID ROUTING ENGINE                 │
│                                                     │
│  Layer 1: Coil-level overrides (always wins)        │
│  Layer 2: ML Ensemble (XGBoost + LightGBM +         │
│           CatBoost) — 97.1% CV accuracy             │
│  Layer 3: Learned grade rules (Supabase DB)         │
│  Layer 4: Rule engine (19-step decision tree)       │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│              SORT & SEQUENCE                        │
│  Width↓ → Thickness↓ → SO → Age↓ → Weight↓         │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│           FORMATTED EXCEL OUTPUT                    │
│  Section headers, subtotals, grand total,           │
│  priority planning block (CRM-04 / CRM-06)         │
└─────────────────────────────────────────────────────┘
```

---

## Material Flow — Downstream Consumers

```
ROLLING MILL
    │
    ├── TUBE FH (C09/TATFHC)    ──────► CRS ──────► Tube Plant
    │
    ├── CRCA FINISH (CRM04/06)  ──────► CRS ──────► OEM / LG Bala
    │
    ├── H&T FINISH (B28)        ──────────────────► H&T Line  (DIRECT)
    │
    ├── SKIN PASS (TATXXD/D012) ──────────────────► Skin Pass (DIRECT)
    │
    ├── RE-ROLLING (B28/BSW2)   ──► Annealing ──► CRS / H&T  (72h cycle)
    │
    └── FIRST ROLLING (LG Bala) ──► Annealing ──► CRS / LG Bala (72h cycle)
```

**CRS is the throughput bottleneck** — it receives Tube FH + CRCA directly from Rolling, plus material returning from annealing every 72 hours.

---

## Sections Generated

| Section | Roll Type | Mill | Next Destination |
|---|---|---|---|
| Rolling | Light Matt | CRM-04 / CRM-06 | Rewinding → Annealing |
| Rolling on Bright Rolls | Bright | CRM-04 | Rewinding → Chrome SPM |
| First Rolling | Light Matt | CRM-06 | Annealing → CRS/LG Bala |
| Re-Rolling | Light Matt | CRM-06 | Annealing → CRS/H&T |
| H&T Finish | Bright | CRM-04 | **H&T Line directly** |
| CRCA Finish | Bright | CRM-04 | CRS → OEM dispatch |
| CRCA Finish (LG Bala) | Bright | CRM-06 | CRS → LG Bala |
| Skin-Pass Super Bright | Super Bright | CRM-04 | **Skin Pass directly** |
| Skin-Pass Chrome Plated | Chrome Plated | CRM-04 | **Skin Pass directly** |
| Tube FH | Bright | CRM-04 / CRM-06 | CRS → Tube Plant |
| Skin-Pass Heavy Matt | Heavy Matt | CRM-06 | **Skin Pass directly** |

---

## Routing Logic — Detailed Rules

### Inclusion / Exclusion Filters

Only coils passing **all** these checks enter the plan:

| Rule | Detail |
|---|---|
| **Current Stage** | Must be exactly `ROLLING MILL` in SAP |
| **Weight** | ≥ 0.5 MT (excludes SAP continuation stubs) |
| **HC80 old** | Excluded if Age ≥ 20 days AND Actual Thick < Plan RT (deferred) |
| **TATFHC/RC01** | Excluded unless Planning Remark contains "FH" (new arrival, not in campaign) |
| **PP-PENDING** | Excluded unless Quality = TATFHC (campaign coil with SAP lag) |
| **TATXXD→S-SPM** | Excluded (already at target, waiting for Skin Pass) |
| **TSBH62 far below target** | Excluded if RT − Actual Thick > 0.8mm (planner defers) |
| **Standalone HOLD** | Excluded if "hold" appears as standalone word in remark |
| **RT = 0** | Excluded unless TATFHC (planner assigns RT on floor) |

### Section Assignment Decision Tree (19 steps)

The rule engine evaluates each coil in order — first matching rule wins:

```
 0. NULL/EMPTY NEXT STAGE → route by quality + last stage heuristic

 1. TUBE FULL HARD
    Quality=TATFHC AND Product=C09 → TUBE_FH
    Mill: R032/RP01/RP02 storage → CRM04, else CRM06

 2. SKIN-PASS HEAVY MATT
    Quality=TATBID OR TDC=BD01 AND Next=S-SPM → SKIN_PASS_HEAVY_MATT @ CRM06
    Also triggered by storage: NC04, NC07, NC12, NC13, NC14, RC07

 3. ROLLING ON BRIGHT ROLLS (D012 first pass)
    TDC=D012 AND Thick≥3.8mm AND RT≤2.05mm AND Next=RW-REWINDING
    → ROLLING_BRIGHT @ CRM04

 4. SKIN-PASS CHROME PLATED
    TDC=D012 AND 2.00≤Thick≤2.05mm AND RT≤1.93mm
    → SKIN_PASS_CHROME @ CRM04

 5. SKIN-PASS SUPER BRIGHT
    Quality=TATT01 or TDC=MJ01 (Munjal Auto) → CRM04
    Product=C01 AND TDC∈{T012,VI01} AND Next=R-C R SLITTER → CRM04
    Quality=TATXXD AND Next=R-C R SLITTER → CRM04
    TDC=D012 AND 2.05<Thick≤2.15 AND 1.95≤RT≤2.00 → CRM04

 6. CRCA FINISH CRM04
    Product=C09 AND Quality contains TSBF (except JL12/TSBF75)
    → CRCA_FINISH @ CRM04

 7b. H&T FINISH for TSBH62 with "final" remark
    Product=B28 AND Quality=TSBH62 AND TDC∈{C162,C462,C176}
    AND remark contains "FINAL" → HT_FINISH @ CRM04

 7. LG BALA / TSBM41 ROUTING (storage-based — key rule from planner)
    Quality=TSBM41 AND TDC=LG01:
      Storage=R034 → CRCA_FINISH_CRM06 @ CRM06 (in finishing area)
      Storage=R037 → FIRST_ROLLING @ CRM06 (still in rolling queue)
      Fallback: Next=B-ANNEALING → FIRST_ROLLING
                Next=R-C R SLITTER → CRCA_FINISH_CRM06

 8. H&T GRADES (B28/B29)
    Next=R-C R SLITTER → HT_FINISH @ CRM04 (always CRM04, never CRM06)
    Next=H-FURNACE or B-ANNEALING:
      Storage=R116 or Thick≥2.5mm → FIRST_ROLLING @ CRM06
      else → RE_ROLLING @ CRM06
    Next=RW-REWINDING → RE_ROLLING @ CRM06
    Multi-pass route with M-ROLLING → RE_ROLLING @ CRM06

 9. FIRST ROLLING (C55 HCCR grades)
    TDC∈{HC84,JL20} AND Quality=TSBCLA (InSafe/Karam Safety)
    → FIRST_ROLLING @ CRM06

10. FIRST ROLLING (B28 heavy gauge via R116 to annealing)
    Product=B28 AND Next=B-ANNEALING AND Storage=R116
    → FIRST_ROLLING @ CRM06

11a. BSW2/BSW1/BSW4 ROUTING (position-in-sequence rule — from planner):
    Parse first thickness from Planning Remark (e.g. "3.2>>2.9>>2.3>>1.4>>1.0 final")
    Actual Thick ≈ first remark thickness → FIRST_ROLLING @ CRM06
    Remark contains "FINAL" AND Actual Thick ≈ Plan RT → HT_FINISH @ CRM04
    Otherwise → RE_ROLLING @ CRM06

11. RE-ROLLING
    TDC=JL12 or Quality=TSBF75 → RE_ROLLING @ CRM06
    Quality=TSBH80 AND TDC=HC80 → RE_ROLLING @ CRM06

12. GENERAL ROLLING
    Product=C01 AND TDC∈{AH12,TE17,JL06,JL07} → ROLLING @ CRM04/CRM06
    TDC=D012 AND Thick≥3.5mm → ROLLING @ CRM04/CRM06
    Quality=TSBM55 → ROLLING @ CRM04

13. BOX STRAP
    Remark contains "BOX STRAP" → ROLLING @ CRM04

14. TATXXD/T012 first pass (going to rewind/anneal)
    Product=C01 AND Quality=TATXXD AND TDC∈{T012,AH12}
    → ROLLING @ CRM04/CRM06

15. TATD12/D012 general pass
    Product=C01 AND TDC=D012 AND Quality=TATD12
    → ROLLING @ CRM04/CRM06
```

### Mill Assignment for ROLLING Section

When both mills run ROLLING on the same day, coils are split by:
1. Work Center code from SAP (most reliable)
2. Thickness heuristic: heavier gauge → CRM04, lighter → CRM06
3. No fixed rule — planner distributes by load judgment

### Coil Ordering Within Each Section

```
1. Width ↓        (widest first — protects roll edges from step marks)
2. Thickness ↓    (thickest input first — steady hydraulic load)
3. SO Number ↑    (same order together — no operator re-setup)
4. Age ↓          (oldest first — clears backlog, prevents TDC expiry)
5. Weight ↓       (heaviest first — maximises MT per hour)
```

---

## ML Model — Advanced Ensemble

### Architecture

Three-model ensemble that votes on every coil routing decision:

| Model | Strength |
|---|---|
| XGBoost | Handles sparse features, robust to noise |
| LightGBM | Better calibrated probabilities, fast retraining |
| CatBoost | Native categorical handling, good on small datasets |

Final prediction = weighted average of all three probability outputs (LightGBM weight 1.2×, CatBoost 1.1×, XGBoost 1.0×).

### Features (70+ per coil)

**Numerical:**
- `actual_thick`, `plan_rt`, `plan_rt2`, `plan_rt3` — current and multi-pass targets
- `thick_rt_diff` — positive = above target, negative = below target
- `rt_over_thick` — ratio of target to current thickness
- `reduction_ratio` — percentage reduction this pass
- `actual_width`, `width_thick_ratio`
- `input_weight`, `coil_age`, `age_bucket`
- `passes_remaining` — how many RT targets still ahead
- `yield_strength`, `uts` — mechanical properties
- `tol_lo`, `tol_hi`, `tol_range` — thickness tolerance band

**Route features (parsed from Process Route string):**
- `route_has_anneal`, `route_has_rewind`, `route_has_spm`
- `route_is_tube_fh` — detects FH NARROW / M>R>QA>PACK patterns
- `route_is_ht` — detects H&T in route
- `route_is_hccr` — HC80 CRCA multi-pass
- `route_n_mill_passes` — total rolling passes in sequence
- `route_n_total_ops` — total operations in route

**Remark features (parsed from Planning Remark):**
- `rmk_has_fh`, `rmk_has_tube`, `rmk_has_final`, `rmk_has_lgbala`
- `rmk_n_steps` — number of `>>` separators (pass count)
- `rmk_target_thick` — smallest number in remark = final target

**Binary flags:** 30+ for specific quality/TDC/product/storage combinations

### Training

- Actual planner labels get **3× sample weight** over synthetic
- SMOTE oversampling ensures rare sections get enough samples
- Uncertainty penalty: when top-2 class probabilities are within 15%, confidence is penalised 15% and rule engine fallback is triggered
- Cross-validated accuracy on actual labels: **97.1%** (day 1), grows with more data

### Prediction priority chain

```
1. Coil-level override (learning DB)    → always wins
2. ML ensemble (confidence ≥ 65%)       → used when confident
3. Learned grade rules (confidence ≥ 3) → from daily corrections
4. Base rule engine (19-step tree)      → fallback
```

---

## Self-Learning Loop

```
Daily WIP → Generate Plan (draft)
                  ↓
         Planner reviews & corrects
                  ↓
    Upload 3 files to Learn page:
    WIP + Generated + Corrected
                  ↓
    ┌──────────────────────────────────┐
    │  Diff engine extracts corrections│
    │  Rule DB updated in Supabase     │
    │  ML ensemble retrained (~5 sec)  │
    │  Model saved to Supabase         │
    └──────────────────────────────────┘
                  ↓
         Next generation uses
         improved model automatically
```

**Correction types detected:**
- Section assignment (coil in wrong section)
- Mill assignment (CRM04 vs CRM06)
- Coil ordering (wrong position within section)
- Coil excluded (planner removed it)
- Coil added (planner included something we missed)
- RT value changed
- Customer abbreviation
- Header wording

**Confidence thresholds:**
- `1` → Observation only, not applied
- `2` → Soft rule, applied with flag
- `3+` → Hard rule, overrides base engine
- `10+` → Locked, requires manual intervention

---

## Priority Advisor

### What it solves

The shift-in-charge currently decides section priority by experience. The Priority Advisor replaces intuition with a scored recommendation that accounts for all constraints simultaneously, then lets the human make the final call.

### Scoring Model

Each section gets a score 0–100 across 5 factors:

| Factor | What it measures | Notes |
|---|---|---|
| **Downstream demand** | How starved is that consumer | Tube Plant=10, H&T Line=9, Skin Pass=8, CRS=7 |
| **Customer urgency** | Priority of customer receiving this material | Tube Plant=10, TMA=8, Bandsaw=7, LG Bala=5 |
| **Coil age** | Age of oldest coil in section | >21 days=100, >14=80, >7=55, >3=30, <3=10 |
| **Annealing pipeline** | Does rolling today feed 72h return? | Shift 1/2 = urgent; Shift 3 = less critical |
| **Production efficiency** | MT/hour for this section type | Rolling=18, Tube FH=15, H&T=13, Skin Pass=9–11 |

### Planning Modes (6 modes)

| Mode | When to use | Key weight change |
|---|---|---|
| **BALANCED** | Normal day, no crisis | All factors equal |
| **TUBE_URGENT** | Tube Plant calling for material | Downstream weight → 80% for Tube sections |
| **HT_URGENT** | H&T Line idle or running low | Downstream weight → 80% for H&T; RE_ROLLING anneal boosted |
| **MAX_PROD** | Management wants maximum MT | Production weight → 75% |
| **CLEAR_BACKLOG** | Old coils piling up, TDC expiry risk | Age weight → 70% |
| **FEED_ANNEAL** | Annealing furnace starved | Anneal weight → 70% for First/Re-Rolling |

### Mode Comparison

The page includes a "Compare all modes" expander that runs all 6 modes simultaneously and shows how the #1 priority changes under each — helping the shift-in-charge see the trade-offs before committing.

### Shift Briefing Output

Generates a WhatsApp-ready text block with:
- Shift number and planning mode
- MT breakdown by downstream consumer (CRS load, direct H&T, direct Skin Pass, annealing feed)
- Priority sequence for CRM-04 and CRM-06 with scores
- Warnings (TDC expiry, starved consumers, annealing pipeline)

---

## CRS Setting Change Optimiser

### What counts as a setting change at CRS

| Change type | Adjustment needed | Downtime |
|---|---|---|
| Width > 2mm | Guide/fence adjustment | ~5 min |
| Thickness > 0.05mm | Pressure/tension reset | ~8 min |
| Product C09 ↔ C01 | Full setup change | ~15 min |
| Customer change | Label/inspection change | ~2 min |

### Algorithm

**Step 1 — Extract CRS coils:** All coils from TUBE_FH, CRCA_FINISH, and CRCA_FINISH_CRM06 sections.

**Step 2 — Compute change cost matrix:** For every pair of coils, calculate a weighted cost score:
```
cost = (width_diff_mm × 0.5) +
       (thick_diff / 0.1mm × 2.0) +
       (product_change × 10.0) +
       (customer_change × 1.0)
```

**Step 3 — Greedy nearest-neighbour:** Starting from each possible coil, always pick the next coil with the lowest transition cost. Run from all starting points, keep the best sequence.

**Step 4 — 2-opt improvement:** Try reversing every sub-sequence of the greedy result. If a reversal reduces total cost, accept it. Repeat until no improvement found.

**Today's result on actual data:**

| | Before | After |
|---|---|---|
| Setting changes | 9 | 6 |
| Weighted cost score | 264.9 | 126.3 |
| Time saved | — | ~24 min |

**Optimised sequence principle:** Group all coils by width band first (470mm → 433mm → 425mm → 410mm), then by thickness within each band. The one unavoidable change is C01→C09 (LG Bala to Tube) — different products require full CRS setup regardless of sequence.

### Key rule

Width step-ups (narrow→wide) are flagged with a warning — roll edges are stressed by this transition. The system recommends placing these at shift start or after a break.

---

## Roll Life Tracker

### Tracking logic

Each roll has a total MT life before dressing/replacement. The tracker:

1. Reads current roll state from Supabase (pre-filled from last session)
2. Simulates the planned MT through each section campaign
3. Detects if any roll will exhaust mid-section

### Severity levels

| Status | Condition | Action |
|---|---|---|
| 🔴 CRITICAL | Roll will exhaust during plan | Insert roll change mid-section |
| 🟡 WARNING | Roll hits 80%+ life by end of shift | Schedule dressing before next campaign |
| 🟠 MONITOR | Roll at 60–80% | Watch, no immediate action |
| 🟢 OK | Under 60% used | Continue |

### Default roll life values

| Mill | Roll Type | Life (MT) |
|---|---|---|
| CRM04 | Light Matt | 300 |
| CRM04 | Bright | 180 |
| CRM04 | Super Bright | 120 |
| CRM04 | Chrome Plated | 80 |
| CRM06 | Light Matt | 280 |
| CRM06 | Bright | 160 |
| CRM06 | Heavy Matt | 200 |

### Persistence

Roll state (type, MT used, roll number) saved to Supabase after every session. Next session pre-fills all fields — no manual re-entry. Roll change button resets MT counter and logs the change event with history.

---

## Width Programme Optimiser

### Transition scoring

Every consecutive section pair gets a score 0–100:

```
total_score = roll_change_cost_normalised (0–50)
            + width_gap_penalty (0–50)
            + thickness_step_stress (0–30)
```

- **Width gap penalty:** 0mm gap = 0 points, 50mm gap = 15 points, 200mm gap = 50 points
- **Thickness step stress:** Thinner-next is worse than thicker-next (chatter risk)
- **Width direction:** STEP_UP (narrow→wide) flagged as higher risk

### What it identifies

- **POOR transitions** (score >60): Recommends reordering
- **Merge opportunities**: Sections sharing same width band AND roll type — could run as one continuous programme
- **Cascade violations**: Step-ups within a section that break width-descending order

---

## Shift Execution Tracker

### Live confirmation

Each coil in the shift plan has three action buttons:

| Button | Action | Side effect |
|---|---|---|
| ✅ | Mark rolled | Adds coil weight to shift MT + roll life tracker |
| ⏭️ | Skip | Logged as skipped, no MT added |
| 🔴 | Hold | Logged as on hold, flagged for next shift |
| ↩️ | Undo | Reverses confirmation, deducts MT from roll life |

### Persistence

Every button tap saves immediately to Supabase — no data lost if phone locks or browser closes. Shift status survives server restarts.

### Analytics

- Day-wise MT trend
- MT by roll type (which roll type consuming most capacity)
- MT by section (which sections consistently underperform)
- Plan adherence % per shift

---

## Test Mode

Toggle in sidebar. When ON:
- All saves go to `TEST_` prefixed keys in Supabase
- Real production data completely untouched
- Stats page shows test record count with one-click delete
- Useful for training new planners or testing new WIP formats

---

## File Structure

```
mill_planner/
├── app.py               # Streamlit web UI (8 pages)
├── mill_planner.py      # CLI entry point
├── generator.py         # WIP filter → assign → sort → Excel writer
├── sectioning.py        # Hybrid routing: ML → learned rules → rule engine
├── ml_classifier.py     # Ensemble classifier (XGBoost+LightGBM+CatBoost)
├── ml_trainer.py        # Training pipeline and CLI
├── learner.py           # Diff engine, pattern extraction, accuracy metrics
├── parser.py            # Reads formatted plan .xlsx back for learning
├── constants.py         # Section labels, colours, customer abbreviations
├── db.py                # Supabase persistence (rules, roll state, ML model)
├── optimiser.py         # Roll change minimisation (section sequencing)
├── roll_life.py         # Roll campaign simulator and life tracker
├── width_programme.py   # Width transition scoring and merge analysis
├── shift_tracker.py     # Live shift execution tracker
├── priority_advisor.py  # Production priority scoring + CRS optimiser
├── requirements.txt
├── models/
│   └── section_clf.pkl  # Trained ML model (also stored in Supabase)
└── README.md
```

---

## App Pages

| Page | Purpose |
|---|---|
| 📋 Generate Plan | Upload WIP → download formatted Excel plan |
| 🧠 Learn from Corrections | Upload WIP+generated+corrected → retrain ML + update rules |
| 📊 Stats & Rules | Accuracy trend, rule table, ML model status, DB management |
| ⚙️ Roll Optimiser | Minimise roll changes between sections (section sequencing) |
| 🔩 Roll Life Tracker | Track roll MT usage, warn before exhaustion |
| 📐 Width Programme | Score width transitions, find merge opportunities |
| 🏭 Shift Execution | Live coil-by-coil confirmation during rolling |
| 🎯 Priority Advisor | Score sections by urgency + CRS setting change optimiser |

---

## CLI Usage

```bash
# Generate a plan
python mill_planner.py generate \
    --wip  Narrow_Data_Coil_Stage.xlsx \
    --date 2026-05-25 \
    --out  mill_plan_25-05-2026.xlsx

# Learn from corrections
python mill_planner.py learn \
    --generated mill_plan_25-05-2026.xlsx \
    --actual    mill_plan_25-05-2026_corrected.xlsx \
    --db        learning_db.json

# Train ML model
python ml_trainer.py train \
    --wip    Narrow_Data_Coil_Stage.xlsx \
    --actual mill_plan_25-05-2026_Actual.xlsx \
    --model  models/section_clf.pkl

# Retrain with new data (incremental)
python ml_trainer.py retrain \
    --wip    Narrow_Data_Coil_Stage.xlsx \
    --actual mill_plan_26-05-2026_Actual.xlsx \
    --model  models/section_clf.pkl

# Check model report
python ml_trainer.py report --model models/section_clf.pkl

# Check accuracy stats
python mill_planner.py stats --db learning_db.json

# Manually fix a routing rule
python mill_planner.py rule-add \
    --db         learning_db.json \
    --key        "TATXXD|AH12|C01|RW-REWINDING" \
    --section    ROLLING \
    --mill       CRM04 \
    --confidence 5
```

---

## Local Setup

```bash
git clone https://github.com/your-username/crm-mill-planner.git
cd crm-mill-planner
pip install -r requirements.txt
streamlit run app.py
```

---

## Supabase Setup

1. Create project at [supabase.com](https://supabase.com)
2. Run in SQL Editor:
```sql
CREATE TABLE learning_db (
    key        TEXT PRIMARY KEY,
    data       JSONB,
    updated_at TIMESTAMPTZ DEFAULT now()
);
```
3. Add to Streamlit secrets:
```toml
SUPABASE_URL = "https://your-project.supabase.co"
SUPABASE_KEY = "sb_secret_..."
```
4. Go to **Stats & Rules → ML Model Status → Upload local model to Supabase**

---

## Data Privacy

- WIP files processed in memory only — never stored on server
- Learning DB stores routing rules (grade + TDC), not individual coil records
- ML model stores learned weights, not raw coil data
- Test Mode keeps all test data under `TEST_` prefix, separate from production

---

*Tata Steel CRM Sahibabad — Narrow Complex Planning Automation*
*Built with Streamlit · XGBoost · LightGBM · CatBoost · Supabase*
