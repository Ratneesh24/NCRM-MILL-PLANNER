# CRM Narrow Complex — Mill Planning System v5.0

Tata Steel CRM Sahibabad · Integrated Pipeline-Aware Planning

## Architecture

    WIP.xlsx
       │
       ├─► [ VALIDATED CORE — unchanged ]
       │     generator.py      filters + Excel writer
       │     sectioning.py     19-step routing tree
       │     ml_classifier.py  XGB + LGBM + CatBoost ensemble
       │     learner.py        self-learning from corrections
       │     db.py             Supabase persistence
       │
       └─► [ NEW ENGINE — crm/ package ]
             config.py     one source of truth (consumers, stages,
                           roll life, shift capacity, mode weights)
             pipeline.py   §1 WIP model · consumer classification ·
                           stage drill-down · flow edges · aging
             health.py     §4 Stage Health Index (RAG, starvation date,
                           overload, days-of-cover)
             scoring.py    §2§3§7 7-factor explainable priority engine
             campaign.py   §6 roll campaigns · coil sequencing ·
                           changeover optimisation · capacity
             twin.py       §8 digital twin forward simulation
             planner.py    orchestrator + Excel export

## Consumer model

| Consumer | Daily ask | Buffer stage | Route |
|---|---|---|---|
| Tube Plant | 210 MT | C R Slitter | via CRS |
| OEM | 50 MT | C R Slitter | via CRS |
| H&T Line | 35 MT | Furnace | **direct — no CRS** |

Classification: customer = Sahibabad Tube Plant → TUBE.
Process route contains an `H` step → H&T. Everything else → OEM.

## Roll life (MT before dressing)

| Roll | 1st rolling (H&T) | 1st rolling (other) | Re-rolling | Finishing |
|---|---|---|---|---|
| Light Matt | 200 | 100 | 100 | 100 |
| Bright | — | — | — | 100 |
| Super Bright | — | — | — | 300 |
| Chrome Plated | — | — | — | 300 |
| Heavy Matt | — | — | — | 200 |

Roll change = 45 min.

## Shift capacity (MT / 8h)

| Type | CRM-04 | CRM-06 |
|---|---|---|
| 1st rolling | — | 120 |
| Re-rolling | 80 | 95 |
| Finishing | 50 | 60 |

Mixed shifts use a MT-weighted blend of the above.

## Scoring factors

starvation · demand share · aging · pipeline protection ·
quality risk · throughput · roll continuity

Weights vary by mode: Balanced · Tube Priority · OEM Priority ·
H&T Priority · Clear Aging · Max Throughput.

**Auto-drop**: if the boosted consumer is already healthy, the boost
transfers to the most starved consumer instead.

## Pages

1. 🏭 Pipeline Overview — stage-wise WIP, drill-down, flow, aging
2. 🩺 Stage Health — health index, starvation dates, alerts
3. 🎯 Plan Builder — score → select → campaign → Excel
4. 🔮 Digital Twin — forward simulation, bottleneck prediction
5. 🧠 Learn — self-learning from corrections
6. 📊 Stats — model status, rules, outcome log
