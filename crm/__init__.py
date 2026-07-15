"""
crm — Tata Steel CRM Sahibabad Narrow Complex Planning System v5.1
==================================================================
Integrated pipeline-aware mill planning:

  config    — all constants (consumers, stages, roll life, capacity)
  pipeline  — WIP model, consumer classification, stage drill-down
  health    — Stage Health Index & consumer coverage (RAG)
  scoring   — 7-factor explainable priority engine
  campaign  — mill sequencing, roll campaigns, changeover optimisation
  twin      — digital twin forward simulation
  dayplan   — whole-day rolling sheet, persisted, priority-locked
"""
from . import config, pipeline, health, scoring, campaign, twin, planner, dayplan

__version__ = "5.1.0"
