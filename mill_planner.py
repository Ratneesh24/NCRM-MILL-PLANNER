#!/usr/bin/env python3
"""
mill_planner.py — Tata Steel CRM Sahibabad Narrow Complex Mill Planner
=======================================================================
Self-learning daily rolling mill plan generator.

Commands
--------
  generate   Build a plan from WIP data
  learn      Diff corrected plan against generated; update learning DB
  stats      Print DB summary and accuracy trend
  review     Show unresolved conflicts for manual resolution
  rule-add   Manually add / override a routing rule
  rollback   Restore DB from a dated backup

Usage
-----
  python mill_planner.py generate --wip FILE --date YYYY-MM-DD --out FILE [--days N] [--db FILE]
  python mill_planner.py learn    --generated FILE --actual FILE --db FILE [--date YYYY-MM-DD]
  python mill_planner.py stats    --db FILE
  python mill_planner.py review   --db FILE
  python mill_planner.py rule-add --db FILE --key "Q|TDC|PC|NS" --section SEC --mill MILL [--confidence N]
  python mill_planner.py rollback --db FILE --to BACKUP_FILE
"""

import argparse
import json
import os
import shutil
import sys
from datetime import date, datetime
from pathlib import Path

# Make sure the package directory is on the path
sys.path.insert(0, os.path.dirname(__file__))

from generator import generate_daily_plan
from learner   import learn, load_db, save_db, backup_db
from constants import SECTION_ORDER, SECTION_SHORT_NAME, CRM04_PRIORITY, CRM06_PRIORITY


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _today():
    return date.today().isoformat()


def _parse_date(s):
    for fmt in ('%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y'):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Cannot parse date: {s!r}  (use YYYY-MM-DD)")


# ─────────────────────────────────────────────────────────────────────────────
# generate
# ─────────────────────────────────────────────────────────────────────────────

def cmd_generate(args):
    plan_date = _parse_date(args.date)
    db = load_db(args.db) if args.db else None

    result = generate_daily_plan(
        wip_file    = args.wip,
        plan_date   = plan_date,
        output_file = args.out,
        days        = args.days,
        learning_db = db,
        verbose     = True,
    )
    print(f"\n✓  Output written → {args.out}")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# learn
# ─────────────────────────────────────────────────────────────────────────────

def cmd_learn(args):
    plan_date = _parse_date(args.date) if args.date else None

    session, corrections = learn(
        generated_path = args.generated,
        actual_path    = args.actual,
        db_path        = args.db,
        plan_date      = plan_date,
        verbose        = True,
    )

    # Warn if accuracy dropped > 5% vs previous session
    db = load_db(args.db)
    if len(db['session_log']) >= 2:
        prev_acc = db['session_log'][-2].get('overall_accuracy', 1.0)
        curr_acc = session['overall_accuracy']
        if prev_acc - curr_acc > 0.05:
            print(f"\n⚠  WARNING: Accuracy dropped {(prev_acc-curr_acc)*100:.1f}% vs previous session.")
            print("   Check whether the correct corrected plan was uploaded.")

    return session


# ─────────────────────────────────────────────────────────────────────────────
# stats
# ─────────────────────────────────────────────────────────────────────────────

def cmd_stats(args):
    db = load_db(args.db)

    n_sessions  = db.get('total_sessions', 0)
    cum_acc     = db.get('cumulative_accuracy', 0.0)
    grade_rules = db.get('grade_routing', {})
    coil_ovr    = db.get('coil_overrides', {})
    sort_exc    = db.get('sort_rules', {})
    cust_abbr   = db.get('customer_abbrev', {})
    conflicts   = [c for c in db.get('conflict_log', []) if not c.get('resolved')]
    split_rules = db.get('split_rules', {})
    sessions    = db.get('session_log', [])

    hard   = sum(1 for v in grade_rules.values() if v.get('confidence', 0) >= 3)
    soft   = sum(1 for v in grade_rules.values() if v.get('confidence', 0) == 2)
    obs    = sum(1 for v in grade_rules.values() if v.get('confidence', 0) == 1)

    print("=" * 66)
    print("  MILL PLANNER — LEARNING DATABASE SUMMARY")
    print("=" * 66)
    print(f"  Sessions completed      : {n_sessions}")
    print(f"  Cumulative accuracy     : {cum_acc*100:.1f}%")
    print(f"  Last updated            : {db.get('last_updated','—')}")

    # Accuracy trend (last 10 sessions)
    if sessions:
        recent = sessions[-10:]
        print(f"\n  ACCURACY TREND (last {len(recent)} sessions):")
        line = '    '
        for s in recent:
            line += f"{s.get('session_date','?')} → {s.get('overall_accuracy',0)*100:.1f}%   "
        print(line.rstrip())

    print(f"\n  RULE SUMMARY:")
    print(f"    Grade routing rules     : {hard} hard | {soft} soft | {obs} observations")
    print(f"    Coil-level overrides    : {len(coil_ovr)}")
    print(f"    Sort exceptions logged  : {sum(r.get('exceptions_logged',0) for r in sort_exc.values())}")
    print(f"    Section split rules     : {len(split_rules)}")
    print(f"    Customer abbreviations  : {len(cust_abbr)}")

    if conflicts:
        print(f"\n  PENDING CONFLICTS ({len(conflicts)} — require manual review):")
        for i, c in enumerate(conflicts[:5], 1):
            print(f"    {i}. {c.get('key','?')}")
            print(f"       existing={c.get('existing_rule','?')}  "
                  f"new={c.get('new_evidence','?')}  "
                  f"confidence={c.get('existing_confidence',0)}")
        print("  Run: python mill_planner.py review --db <db_path>")
    else:
        print("\n  No unresolved conflicts.")

    # Lowest accuracy section (last 7 sessions)
    if len(sessions) >= 3:
        sec_errors = {}
        for s in sessions[-7:]:
            for ctype, cnt in s.get('corrections_by_type', {}).items():
                sec_errors[ctype] = sec_errors.get(ctype, 0) + cnt
        worst = max(sec_errors, key=sec_errors.get) if sec_errors else None
        if worst:
            print(f"\n  MOST COMMON CORRECTION TYPE (last 7 sessions): {worst} "
                  f"({sec_errors[worst]} occurrences)")

    print("\n  RECOMMENDATION:")
    print("    → Upload corrected plans daily for best accuracy.")
    if conflicts:
        print(f"    → Resolve {len(conflicts)} conflict(s) before next generate run.")
    print("=" * 66)


# ─────────────────────────────────────────────────────────────────────────────
# review
# ─────────────────────────────────────────────────────────────────────────────

def cmd_review(args):
    db = load_db(args.db)
    conflicts = [c for c in db.get('conflict_log', []) if not c.get('resolved')]

    if not conflicts:
        print("No unresolved conflicts in the learning DB.")
        return

    print(f"\n{'='*60}")
    print(f"  UNRESOLVED CONFLICTS ({len(conflicts)} total)")
    print(f"{'='*60}")

    for i, c in enumerate(conflicts, 1):
        print(f"\n[{i}] Key       : {c.get('key','?')}")
        print(f"    Existing  : {c.get('existing_rule','?')} "
              f"(confidence={c.get('existing_confidence',0)})")
        print(f"    New evidence: {c.get('new_evidence','?')}")
        print(f"    Date      : {c.get('date','?')}")

    print(f"\n{'─'*60}")
    print("To resolve a conflict, run:")
    print("  python mill_planner.py rule-add --db <db> "
          "--key \"<key>\" --section <SEC> --mill <MILL> --confidence <N>")
    print(f"{'='*60}")


# ─────────────────────────────────────────────────────────────────────────────
# rule-add
# ─────────────────────────────────────────────────────────────────────────────

def cmd_rule_add(args):
    db = load_db(args.db)

    confidence = int(args.confidence) if args.confidence else 5
    section    = args.section.upper()
    mill       = args.mill.upper()

    db['grade_routing'][args.key] = {
        'section':      section,
        'mill':         mill,
        'confidence':   confidence,
        'observations': confidence,
        'overrides':    0,
        'last_seen':    _today(),
        'source':       'manual',
    }

    # Mark any matching conflicts as resolved
    for c in db.get('conflict_log', []):
        if c.get('key') == args.key and not c.get('resolved'):
            c['resolved']   = True
            c['resolution'] = f"Manual override → {section}|{mill} @ conf={confidence}"

    save_db(db, args.db)
    print(f"✓  Rule added/updated: {args.key}")
    print(f"   Section={section}, Mill={mill}, Confidence={confidence}")


# ─────────────────────────────────────────────────────────────────────────────
# rollback
# ─────────────────────────────────────────────────────────────────────────────

def cmd_rollback(args):
    if not Path(args.to).exists():
        print(f"ERROR: Backup file not found: {args.to}")
        sys.exit(1)
    # Safety: backup current state first
    backup_db(args.db)
    shutil.copy2(args.to, args.db)
    print(f"✓  Rolled back {args.db} ← {args.to}")


# ─────────────────────────────────────────────────────────────────────────────
# Argument parser
# ─────────────────────────────────────────────────────────────────────────────

def build_parser():
    p = argparse.ArgumentParser(
        prog='mill_planner.py',
        description='Tata Steel CRM Sahibabad — Narrow Complex Mill Planner',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest='command', required=True)

    # generate
    g = sub.add_parser('generate', help='Generate a mill plan from WIP data')
    g.add_argument('--wip',   required=True, help='WIP input .xlsx file')
    g.add_argument('--date',  required=True, help='Planning date (YYYY-MM-DD)')
    g.add_argument('--out',   required=True, help='Output .xlsx file path')
    g.add_argument('--days',  type=int, default=1, help='Number of days to generate (default 1)')
    g.add_argument('--db',    default=None, help='Path to learning_db.json (optional)')

    # learn
    l = sub.add_parser('learn', help='Learn from planner corrections')
    l.add_argument('--generated', required=True, help='Generated plan .xlsx')
    l.add_argument('--actual',    required=True, help='Planner-corrected plan .xlsx')
    l.add_argument('--db',        required=True, help='Path to learning_db.json')
    l.add_argument('--date',      default=None,  help='Plan date (YYYY-MM-DD); auto-detected if omitted')

    # stats
    st = sub.add_parser('stats', help='Show learning DB summary')
    st.add_argument('--db', required=True, help='Path to learning_db.json')

    # review
    rv = sub.add_parser('review', help='Review unresolved conflicts')
    rv.add_argument('--db', required=True, help='Path to learning_db.json')

    # rule-add
    ra = sub.add_parser('rule-add', help='Manually add or override a routing rule')
    ra.add_argument('--db',         required=True, help='Path to learning_db.json')
    ra.add_argument('--key',        required=True,
                    help='Routing key: "Quality|TDC|ProdCode|NextStage"')
    ra.add_argument('--section',    required=True, help='Section key e.g. HT_FINISH')
    ra.add_argument('--mill',       required=True, help='Mill e.g. CRM04')
    ra.add_argument('--confidence', default='5',   help='Confidence level (default 5)')

    # rollback
    rb = sub.add_parser('rollback', help='Restore DB from a backup')
    rb.add_argument('--db', required=True,  help='Path to active learning_db.json')
    rb.add_argument('--to', required=True,  help='Path to backup file to restore from')

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()

    dispatch = {
        'generate': cmd_generate,
        'learn':    cmd_learn,
        'stats':    cmd_stats,
        'review':   cmd_review,
        'rule-add': cmd_rule_add,
        'rollback': cmd_rollback,
    }
    fn = dispatch.get(args.command)
    if fn:
        fn(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
