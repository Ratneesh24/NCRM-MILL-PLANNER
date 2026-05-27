# Tata Steel CRM Sahibabad — Narrow Complex Mill Planner

An intelligent, self-learning daily rolling mill plan generator for the Narrow Complex at Tata Steel CRM Sahibabad. Converts a daily WIP (Work-In-Progress) coil staging export from SAP into a fully formatted, section-wise rolling plan — matching the manually prepared format exactly — and improves accuracy every day through machine learning and planner corrections.

---

## Live App

**[🚀 Open the app →]**

---

## What it does

Upload today's WIP file from SAP → the system generates a complete rolling plan in seconds:

- **100 coils / 843 MT** matched against actual planner output on first run
- **96.6% inclusion accuracy** — right coils, right sections
- **Width cascade enforced** — widest coils first within every section, protecting roll edges
- **Priority block generated** — numbered CRM-04 and CRM-06 priority order at the bottom
- **Self-improving** — every planner correction teaches the model, accuracy grows daily

---

## System Architecture

```
SAP WIP Export (.xlsx)
        │
        ▼
┌──────────────────────────────────────────────────┐
│                  FILTER ENGINE                   │
│  Current Stage = ROLLING MILL only               │
│  Exclusions: HC80 old, RC01 new arrivals,        │
│  PP-PENDING (except Tube FH), TATXXD→SPM         │
└──────────────────────┬───────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────┐
│              HYBRID ROUTING ENGINE               │
│                                                  │
│  Layer 1: Coil-level overrides (always wins)     │
│  Layer 2: ML Ensemble (XGBoost + LightGBM +      │
│           CatBoost) — 97.1% CV accuracy          │
│  Layer 3: Learned grade rules (Supabase DB)      │
│  Layer 4: Rule engine (19-step decision tree)    │
└──────────────────────┬───────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────┐
│               SORT & SEQUENCE                    │
│  Width ↓ → Thickness ↓ → SO → Age ↓ → Weight ↓  │
└──────────────────────┬───────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────┐
│            FORMATTED EXCEL OUTPUT                │
│  Tata Steel branded, section headers, subtotals  │
│  Grand total, priority planning block            │
└──────────────────────────────────────────────────┘
```

---

## Sections Generated

| Section | Roll Type | Mill | Oil Instruction |
|---|---|---|---|
| Rolling | Light Matt | CRM-04 / CRM-06 | — |
| Rolling on Bright Rolls | Bright | CRM-04 | — |
| First Rolling | Light Matt | CRM-06 | — |
| Re-Rolling | Light Matt | CRM-06 | — |
| H&T Finish | Bright | CRM-04 | **DO NOT apply R.P.Oil** |
| CRCA Finish | Bright | CRM-04 | Apply R.P.Oil |
| CRCA Finish (LG Bala) | Bright | CRM-06 | Apply R.P.Oil |
| Skin-Pass Super Bright | Super Bright | CRM-04 | Apply R.P.Oil |
| Skin-Pass Chrome Plated | Chrome Plated | CRM-04 | Apply R.P.Oil |
| Tube FH | Bright | CRM-04 / CRM-06 | Apply R.P.Oil |
| Skin-Pass Heavy Matt | Heavy Matt | CRM-06 | Apply R.P.Oil |

---

## ML Model — Advanced Ensemble

The routing engine uses a 3-model ensemble that grows smarter every day:

| Component | Detail |
|---|---|
| **Models** | XGBoost + LightGBM + CatBoost (weighted vote) |
| **Features** | 70+ per coil — Process Route, multi-pass RT targets, thickness tolerance, yield strength, remark parsing |
| **Training** | Actual plan labels (3× weight) + rule-engine synthetic labels |
| **Class balancing** | SMOTE oversampling for minority sections |
| **Confidence** | Uncertainty-penalised — falls back to rule engine when unsure |
| **Explainability** | SHAP values — shows why each coil was routed |
| **CV Accuracy** | 97.1% (day 1, grows with more data) |
| **Persistence** | Model stored compressed in Supabase — survives server restarts |

**Accuracy growth with more data:**

| Data available | Expected CV accuracy |
|---|---|
| 1 day (current) | 97.1% |
| 7 days | ~98.5% |
| 30 days | ~99%+ |

---

## App Pages

### 📋 Generate Plan
Upload WIP file → select date → download formatted `.xlsx` plan. Applies ML model + learned rules automatically.

### 🧠 Learn from Corrections
Upload 3 files: WIP + generated plan + corrected plan. One click:
- Diffs generated vs corrected, extracts every routing correction
- Updates rule-based learning DB in Supabase
- Retrains ML ensemble with new actual labels
- Reports updated accuracy metrics

### 📊 Stats & Rules
- Accuracy trend chart across sessions
- Grade routing rules table (with confidence levels)
- ML model status — local vs Supabase, last trained date
- Conflict resolution for ambiguous routing rules
- Export / import learning DB

### ⚙️ Roll Optimiser
Analyses the section sequence and finds the ordering that minimises roll changes:
- Brute-force optimal for ≤7 sections, greedy for larger plans
- Hard constraints respected (Chrome Plated first, Heavy Matt last)
- Reports downtime saved (minutes) and extra MT possible

### 🔩 Roll Life Tracker
Tracks roll usage across shifts:
- Pre-fills from last session — no manual re-entry
- Warns when roll will exhaust mid-section
- Roll change button resets counter with history logged
- Persistent in Supabase — survives restarts

### 📐 Width Programme Optimiser
Analyses width transitions between sections:
- Transition score (0–100) for each section handoff
- Flags poor transitions causing roll edge stress
- Identifies cross-section merge opportunities
- Width profile box chart per section

### 🏭 Shift Execution Tracker
Live coil-by-coil confirmation during rolling:
- ✅ Roll → ⏭️ Skip → 🔴 Hold buttons per coil
- Each confirmation instantly updates roll life tracker
- Progress bar, MT rolled vs target
- Shift history and analytics (MT by roll type, adherence %)
- Test Mode — keeps test data separate from production

---

## Self-Learning Loop

```
Daily WIP → Generate Plan (draft)
                  ↓
         Planner reviews & corrects
                  ↓
         Upload both to Learn page
                  ↓
    ┌─────────────────────────────┐
    │  Rule DB updated (Supabase) │
    │  ML model retrained         │
    │  Model saved to Supabase    │
    └─────────────────────────────┘
                  ↓
         Next generation uses
         improved model automatically
```

Confidence thresholds for learned rules:
- `1` — Observation only, not applied
- `2` — Soft rule, applied with flag
- `3+` — Hard rule, overrides base engine
- `10+` — Locked, requires manual override

---

## Filtering Rules (Inclusion Criteria)

Only coils meeting **all** of these are included in the plan:

| Rule | Detail |
|---|---|
| Current Stage | Must be exactly `ROLLING MILL` |
| Weight | ≥ 0.5 MT (excludes SAP continuation stubs) |
| HC80 coils | Included only if Age ≤ 20 days AND Actual Thick ≥ Plan RT |
| TATFHC/RC01 | Excluded unless Planning Remark contains "FH" |
| PP-PENDING | Excluded unless TATFHC quality (campaign coils) |
| TATXXD→S-SPM | Excluded (already at target, waiting for Skin Pass) |
| TSBH62 below target | Excluded if RT − Actual Thick > 0.8mm |
| Hold | Excluded if "hold" appears as standalone word in remark |
| RT = 0 | Excluded unless TATFHC (planner assigns RT on floor) |

---

## File Structure

```
mill_planner/
├── app.py              # Streamlit web UI (7 pages)
├── mill_planner.py     # CLI entry point
├── generator.py        # WIP load → filter → assign → sort → Excel writer
├── sectioning.py       # Hybrid routing: ML → learned rules → rule engine
├── ml_classifier.py    # Advanced ensemble classifier (XGBoost+LightGBM+CatBoost)
├── ml_trainer.py       # Training pipeline and CLI
├── learner.py          # Diff engine, pattern extraction, accuracy metrics
├── parser.py           # Reads formatted plan .xlsx back for learning
├── constants.py        # Section labels, colours, customer abbreviations
├── db.py               # Supabase persistence (rules, roll state, ML model)
├── optimiser.py        # Roll change minimisation engine
├── roll_life.py        # Roll campaign simulator and life tracker
├── width_programme.py  # Width transition analysis
├── shift_tracker.py    # Live shift execution tracker
├── requirements.txt
├── models/
│   └── section_clf.pkl # Trained ML model (also stored in Supabase)
└── README.md
```

---

## CLI Usage

**Generate a plan:**
```bash
python mill_planner.py generate \
    --wip  Narrow_Data_Coil_Stage.xlsx \
    --date 2026-05-25 \
    --out  mill_plan_25-05-2026.xlsx
```

**Learn from corrections:**
```bash
python mill_planner.py learn \
    --generated mill_plan_25-05-2026.xlsx \
    --actual    mill_plan_25-05-2026_corrected.xlsx \
    --db        learning_db.json
```

**Train ML model:**
```bash
python ml_trainer.py train \
    --wip    Narrow_Data_Coil_Stage.xlsx \
    --actual mill_plan_25-05-2026_Actual.xlsx \
    --model  models/section_clf.pkl
```

**Retrain with new data:**
```bash
python ml_trainer.py retrain \
    --wip    Narrow_Data_Coil_Stage.xlsx \
    --actual mill_plan_26-05-2026_Actual.xlsx \
    --model  models/section_clf.pkl
```

**Check model stats:**
```bash
python ml_trainer.py report --model models/section_clf.pkl
```

**Check accuracy stats:**
```bash
python mill_planner.py stats --db learning_db.json
```

---

## Local Setup

```bash
# Clone repo
git clone https://github.com/your-username/crm-mill-planner.git
cd crm-mill-planner

# Install dependencies
pip install -r requirements.txt

# Run the app
streamlit run app.py
```

---

## Supabase Setup

1. Create a project at [supabase.com](https://supabase.com)
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
4. Go to **Stats & Rules → ML Model Status** → click **Upload local model to Supabase**

---

## Data Privacy

- WIP files are processed in memory only — never stored on the server
- The learning DB stores routing rules (grade + TDC combinations), not individual coil records
- The ML model stores learned weights, not raw coil data
- Test Mode keeps all test data under `TEST_` prefixed keys, separate from production

---

*Tata Steel CRM Sahibabad — Narrow Complex Planning Automation*  
*Built with Streamlit · XGBoost · LightGBM · CatBoost · Supabase*
