# Tata Steel CRM Sahibabad — Narrow Complex Mill Planner

A self-learning daily rolling mill plan generator for the Narrow Complex at Tata Steel CRM Sahibabad.  
Converts a daily WIP (Work-In-Progress) coil staging export into a formatted, multi-section `.xlsx` plan — exactly matching the manually-prepared format — and improves accuracy with every planner correction.

---

## Features

| | |
|---|---|
| 📋 **Auto-generates** rolling mill plan from WIP data | Replicates planner's section logic, roll-type grouping, width-cascade ordering |
| 🧠 **Self-learning** | Diffs generated vs corrected plan; extracts rules; improves daily |
| 🏭 **Covers all sections** | Rolling, First Rolling, Re-Rolling, H&T Finish, CRCA Finish, Skin-Pass (Super Bright / Chrome / Heavy Matt), Tube FH |
| ⚡ **Web interface** | Streamlit UI for upload → generate → download |
| 🔒 **No data stored** | WIP files are processed in memory; nothing is persisted on the server |

---

## Web App

**[🚀 Open the live app →](https://your-app-name.streamlit.app)**  
*(replace with your actual Streamlit URL after deployment)*

---

## Local Usage

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Run the Streamlit app
```bash
streamlit run app.py
```

### 3. Or use the CLI directly

**Generate a plan:**
```bash
python mill_planner.py generate \
    --wip  Narrow_Data_Coil_Stage.xlsx \
    --date 2026-05-22 \
    --out  mill_plan_22-05-2026.xlsx \
    --db   learning_db.json
```

**Learn from a corrected plan:**
```bash
python mill_planner.py learn \
    --generated mill_plan_22-05-2026.xlsx \
    --actual    mill_plan_22-05-2026_corrected.xlsx \
    --db        learning_db.json
```

**Check stats and accuracy trend:**
```bash
python mill_planner.py stats --db learning_db.json
```

**Manually add or fix a routing rule:**
```bash
python mill_planner.py rule-add \
    --db         learning_db.json \
    --key        "TATXXD|AH12|C01|RW-REWINDING" \
    --section    ROLLING \
    --mill       CRM04 \
    --confidence 5
```

**Rollback the learning DB:**
```bash
python mill_planner.py rollback \
    --db learning_db.json \
    --to learning_db_backup/learning_db_2026-05-21.json
```

---

## Project Structure

```
mill_planner/
├── app.py            # Streamlit web UI
├── mill_planner.py   # CLI entry point (generate / learn / stats / review / rule-add / rollback)
├── generator.py      # WIP load → filter → assign → sort → Excel writer
├── sectioning.py     # 19-step section assignment decision tree
├── learner.py        # Diff engine, pattern extraction, DB update, accuracy metrics
├── parser.py         # Reads formatted plan .xlsx back to structured dict
├── constants.py      # Section labels, colours, customer abbreviations
├── requirements.txt
└── README.md
```

---

## Section Types Supported

| Section | Roll Type | Mill | Oil |
|---|---|---|---|
| Rolling | Light Matt | CRM04 / CRM06 / Both | — |
| Rolling on Bright Rolls | Bright | CRM04 | — |
| First Rolling | Light Matt | CRM06 | — |
| Re-Rolling | Light Matt | CRM06 | — |
| H&T Finish | Bright | CRM04 | **DO NOT apply R.P.Oil** |
| CRCA Finish | Bright | CRM04 | Apply R.P.Oil |
| CRCA Finish (LG Bala) | Bright | CRM06 | Apply R.P.Oil |
| Skin-Pass Super Bright | Super Bright | CRM04 | Apply R.P.Oil |
| Skin-Pass Chrome Plated | Chrome Plated | CRM04 | Apply R.P.Oil |
| Tube FH | Bright | CRM04/06 | Apply R.P.Oil |
| Skin-Pass Heavy Matt | Heavy Matt | CRM06 | Apply R.P.Oil |

---

## Coil Sorting (Width Cascade)

Within every section, coils are ordered by:
1. **Width ↓** (widest first — protects roll edges)
2. **Thickness ↓** (thicker input first — steady mill load)
3. **SO Number ↑** (same order grouped — no operator re-setup)
4. **Age ↓** (oldest coils first — clears backlog)
5. **Weight ↓** (heavier coils first — maximises MT/hour)

---

## Self-Learning Architecture

```
WIP Data → [Generate] → Draft Plan (.xlsx)
                              ↓
                    Planner reviews & corrects
                              ↓
                    Corrected Plan uploaded
                              ↓
                    [Learn] → learning_db.json updated
                              ↓
                    Next generation uses improved rules
```

Confidence levels: `1` observation → `2` soft rule → `3+` hard rule → `10+` locked.

---

## Data Privacy

- WIP files uploaded to the web app are **processed in memory only**
- No coil data, customer data, or production data is stored on the server
- The `learning_db.json` stores only routing rules (grade + TDC combinations), not individual coil records
- Export and re-import the `learning_db.json` from the Stats page to carry your learned rules across sessions

---

*Tata Steel CRM Sahibabad — Narrow Complex Planning Automation*
