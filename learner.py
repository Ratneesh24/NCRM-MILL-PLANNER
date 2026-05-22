"""
learner.py — Diff engine, pattern extraction, and learning_db management.

The learning_db.json file grows with every correction session.
Confidence thresholds:
  1  = observation only (not applied during generation)
  2  = soft rule (applied, flagged in output)
  3+ = hard rule (applied, overrides base decision tree)
  10+ = locked (never auto-overridden)
"""

import json
import os
import shutil
from datetime import date, datetime
from pathlib import Path

from parser import parse_plan, build_coil_index, normalise_header


# ---------------------------------------------------------------------------
# DB initialisation
# ---------------------------------------------------------------------------
EMPTY_DB = {
    "schema_version":    "1.0",
    "last_updated":      "",
    "total_sessions":    0,
    "cumulative_accuracy": 0.0,
    "grade_routing":     {},
    "coil_overrides":    {},
    "sort_rules":        {},
    "split_rules":       {},
    "inclusion_rules":   {
        "exclude_patterns": [],
        "include_patterns": [],
    },
    "header_vocab":      {},
    "customer_abbrev":   {},
    "rt_corrections":    {},
    "session_log":       [],
    "conflict_log":      [],
}


def load_db(db_path):
    """Load or initialise the learning DB."""
    if db_path and Path(db_path).exists():
        with open(db_path, 'r') as f:
            db = json.load(f)
        # Ensure all keys exist (forward compatibility)
        for k, v in EMPTY_DB.items():
            if k not in db:
                db[k] = v
        return db
    return dict(EMPTY_DB)


def save_db(db, db_path):
    """Save the learning DB atomically via a temp file."""
    if not db_path:
        return
    db['last_updated'] = date.today().isoformat()
    tmp = db_path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(db, f, indent=2, default=str)
    os.replace(tmp, db_path)


def backup_db(db_path):
    """Create a dated backup before a learn session."""
    if not db_path or not Path(db_path).exists():
        return
    backup_dir = Path(db_path).parent / 'learning_db_backup'
    backup_dir.mkdir(exist_ok=True)
    stamp = date.today().isoformat()
    dest = backup_dir / f"learning_db_{stamp}.json"
    shutil.copy2(db_path, dest)
    return str(dest)


# ---------------------------------------------------------------------------
# Diff engine
# ---------------------------------------------------------------------------
def diff_plans(generated_path, actual_path, plan_date=None):
    """
    Compare generated vs corrected plan.
    Returns correction_log: list of dicts, each describing one correction.
    """
    gen_plan = parse_plan(generated_path, plan_date)
    act_plan = parse_plan(actual_path,    plan_date)

    gen_idx = build_coil_index(gen_plan)
    act_idx = build_coil_index(act_plan)

    correction_log = []
    today_str = date.today().isoformat()

    # 1. Compare every coil in the actual plan against what was generated
    for coil_id, act_info in act_idx.items():
        if coil_id not in gen_idx:
            correction_log.append({
                'type':            'coil_added',
                'coil':            coil_id,
                'actual_section':  act_info['section'],
                'actual_mill':     act_info['mill'],
                'generated_section': None,
                'coil_data':       act_info['data'],
                'date':            today_str,
            })
            continue

        gen_info = gen_idx[coil_id]

        # Section / mill assignment
        if gen_info['section'] != act_info['section']:
            correction_log.append({
                'type':              'section_assignment',
                'coil':              coil_id,
                'generated_section': gen_info['section'],
                'generated_mill':    gen_info['mill'],
                'actual_section':    act_info['section'],
                'actual_mill':       act_info['mill'],
                'coil_data':         act_info['data'],
                'date':              today_str,
            })
        elif gen_info['mill'] != act_info['mill']:
            correction_log.append({
                'type':           'mill_assignment',
                'coil':           coil_id,
                'section':        act_info['section'],
                'generated_mill': gen_info['mill'],
                'actual_mill':    act_info['mill'],
                'coil_data':      act_info['data'],
                'date':           today_str,
            })
        elif gen_info['position'] != act_info['position']:
            correction_log.append({
                'type':               'ordering',
                'coil':               coil_id,
                'section':            act_info['section'],
                'mill':               act_info['mill'],
                'generated_position': gen_info['position'],
                'actual_position':    act_info['position'],
                'coil_data':          act_info['data'],
                'date':               today_str,
            })

        # RT value change
        gen_rt  = _safe_float(gen_info['data'].get('Plan Rolling Thick 1'))
        act_rt  = _safe_float(act_info['data'].get('Plan Rolling Thick 1'))
        if abs(gen_rt - act_rt) > 0.005:
            correction_log.append({
                'type':          'rt_value',
                'coil':          coil_id,
                'generated_rt':  gen_rt,
                'actual_rt':     act_rt,
                'coil_data':     act_info['data'],
                'date':          today_str,
            })

        # Customer abbreviation
        gen_cust = str(gen_info['data'].get('Customer Desc') or '').strip()
        act_cust = str(act_info['data'].get('Customer Desc') or '').strip()
        if gen_cust != act_cust and act_cust:
            correction_log.append({
                'type':               'customer_abbrev',
                'coil':               coil_id,
                'generated_customer': gen_cust,
                'actual_customer':    act_cust,
                'coil_data':          act_info['data'],
                'date':               today_str,
            })

    # 2. Coils the model generated but planner removed
    for coil_id in gen_idx:
        if coil_id not in act_idx:
            correction_log.append({
                'type':              'coil_excluded',
                'coil':              coil_id,
                'generated_section': gen_idx[coil_id]['section'],
                'generated_mill':    gen_idx[coil_id]['mill'],
                'coil_data':         gen_idx[coil_id]['data'],
                'date':              today_str,
            })

    # 3. Section splits: same section appears more than once in actual
    from collections import Counter
    act_sec_counts = Counter(s['section_key'] for s in act_plan['sections'])
    for sec, count in act_sec_counts.items():
        if count > 1:
            correction_log.append({
                'type':        'section_split',
                'section':     sec,
                'split_count': count,
                'date':        today_str,
            })

    # 4. Header wording changes
    gen_sec_map = {s['section_key']: s['raw_header'] for s in gen_plan['sections']}
    act_sec_map = {s['section_key']: s['raw_header'] for s in act_plan['sections']}
    for sk in set(gen_sec_map) & set(act_sec_map):
        if gen_sec_map[sk] != act_sec_map[sk]:
            correction_log.append({
                'type':             'header_wording',
                'section_key':      sk,
                'generated_header': gen_sec_map[sk],
                'actual_header':    act_sec_map[sk],
                'date':             today_str,
            })

    return correction_log, gen_plan, act_plan


# ---------------------------------------------------------------------------
# Pattern extraction & DB update
# ---------------------------------------------------------------------------
def _safe_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _describe_coil(d):
    """Short textual description of a coil for include/exclude patterns."""
    parts = []
    for k in ('Actual Quality', 'Cust TDC', 'Product Code', 'Next Stage'):
        v = d.get(k)
        if v:
            parts.append(f"{k}={v}")
    return ' | '.join(parts)


def extract_and_update(correction_log, learning_db, gen_plan, act_plan):
    """
    Process correction_log, update learning_db in place.
    Returns (rules_added, rules_reinforced, conflicts_flagged) counters.
    """
    today = date.today().isoformat()
    added = reinforced = conflicts = 0

    # ── Grade routing corrections ───────────────────────────────────────
    for c in correction_log:
        if c['type'] not in ('section_assignment', 'mill_assignment'):
            continue
        d = c['coil_data']
        qual  = str(d.get('Actual Quality') or '').strip()
        tdc   = str(d.get('Cust TDC') or '').strip()
        prod  = str(d.get('Product Code') or '').strip()
        nxt   = str(d.get('Next Stage') or '').strip()
        key   = f"{qual}|{tdc}|{prod}|{nxt}"
        target_sec  = c.get('actual_section',  '')
        target_mill = c.get('actual_mill',     '')

        if not target_sec or target_sec == 'UNKNOWN':
            continue

        existing = learning_db['grade_routing'].get(key)
        if existing:
            if (existing['section'] == target_sec
                    and existing['mill'] == target_mill):
                existing['confidence'] += 1
                existing['observations'] += 1
                existing['last_seen'] = today
                reinforced += 1
            else:
                # Conflict — only flag if existing confidence < 10
                if existing['confidence'] < 10:
                    learning_db['conflict_log'].append({
                        'date':               today,
                        'key':                key,
                        'existing_rule':      f"{existing['section']}|{existing['mill']}",
                        'new_evidence':       f"{target_sec}|{target_mill}",
                        'existing_confidence': existing['confidence'],
                        'resolved':           False,
                        'resolution':         None,
                    })
                    conflicts += 1
        else:
            learning_db['grade_routing'][key] = {
                'section':      target_sec,
                'mill':         target_mill,
                'confidence':   1,
                'observations': 1,
                'overrides':    0,
                'last_seen':    today,
                'source':       'learned',
            }
            added += 1

    # ── Persistent coil-level overrides (same coil corrected 2+ times) ──
    from collections import Counter, defaultdict
    coil_corrections = defaultdict(list)
    for c in correction_log:
        if c['type'] == 'section_assignment':
            coil_corrections[c['coil']].append(c)
    for coil_id, corrs in coil_corrections.items():
        if len(corrs) >= 2:
            c = corrs[-1]
            learning_db['coil_overrides'][coil_id] = {
                'section':    c['actual_section'],
                'mill':       c['actual_mill'],
                'confidence': len(corrs),
                'last_seen':  today,
                'note':       'Auto-promoted: corrected ≥ 2 times',
            }

    # ── Sort exceptions ──────────────────────────────────────────────────
    order_corrections = [c for c in correction_log if c['type'] == 'ordering']
    from collections import defaultdict as dd2
    by_section = dd2(list)
    for c in order_corrections:
        by_section[c['section']].append(c)
    for sec, corrs in by_section.items():
        if sec not in learning_db['sort_rules']:
            learning_db['sort_rules'][sec] = {
                'confirmed_rule':    'width_desc_thick_desc_age_desc',
                'exceptions_logged': 0,
                'exception_patterns': [],
            }
        learning_db['sort_rules'][sec]['exceptions_logged'] += len(corrs)

    # ── Inclusion / exclusion patterns ──────────────────────────────────
    for c in correction_log:
        if c['type'] == 'coil_added':
            learning_db['inclusion_rules']['include_patterns'].append({
                'condition':   _describe_coil(c['coil_data']),
                'confidence':  1,
                'description': f"Planner added coil to {c['actual_section']}",
                'date':        today,
            })
        if c['type'] == 'coil_excluded':
            learning_db['inclusion_rules']['exclude_patterns'].append({
                'condition':   _describe_coil(c['coil_data']),
                'confidence':  1,
                'description': f"Planner removed coil from {c['generated_section']}",
                'date':        today,
            })

    # ── Header vocabulary ────────────────────────────────────────────────
    for c in correction_log:
        if c['type'] == 'header_wording':
            sk = c['section_key']
            learning_db['header_vocab'][sk] = c['actual_header']

    # ── RT corrections ───────────────────────────────────────────────────
    for c in correction_log:
        if c['type'] == 'rt_value':
            d = c['coil_data']
            rt_key = f"{c['coil']}|{today}"
            learning_db['rt_corrections'][rt_key] = {
                'corrected_rt': c['actual_rt'],
                'original_rt':  c['generated_rt'],
                'quality':      str(d.get('Actual Quality') or ''),
                'tdc':          str(d.get('Cust TDC') or ''),
            }

    # ── Customer abbreviations ───────────────────────────────────────────
    for c in correction_log:
        if c['type'] == 'customer_abbrev':
            raw  = str(c['coil_data'].get('Customer Desc') or '').strip()
            corr = str(c.get('actual_customer') or '').strip()
            if raw and corr and raw != corr:
                learning_db['customer_abbrev'][raw] = corr

    # ── Section split rules ──────────────────────────────────────────────
    for c in correction_log:
        if c['type'] == 'section_split':
            sk = c['section']
            if sk not in learning_db['split_rules']:
                learning_db['split_rules'][sk] = {
                    'split_when':  'age_gap_or_width_gap',
                    'description': 'Section split detected by planner',
                    'confidence':  1,
                }
            else:
                learning_db['split_rules'][sk]['confidence'] += 1

    return added, reinforced, conflicts


# ---------------------------------------------------------------------------
# Accuracy calculation
# ---------------------------------------------------------------------------
def calculate_accuracy(correction_log, total_coils_actual):
    if total_coils_actual == 0:
        return {k: 0.0 for k in ('section_accuracy', 'ordering_accuracy',
                                   'inclusion_accuracy', 'overall_accuracy')}
    sec_err  = sum(1 for c in correction_log
                   if c['type'] == 'section_assignment')
    ord_err  = sum(1 for c in correction_log if c['type'] == 'ordering')
    inc_err  = sum(1 for c in correction_log
                   if c['type'] in ('coil_added', 'coil_excluded'))
    sec_acc  = max(0.0, 1 - sec_err  / total_coils_actual)
    ord_acc  = max(0.0, 1 - ord_err  / total_coils_actual)
    inc_acc  = max(0.0, 1 - inc_err  / total_coils_actual)
    overall  = sec_acc * 0.60 + ord_acc * 0.25 + inc_acc * 0.15
    return {
        'section_accuracy':   round(sec_acc,  4),
        'ordering_accuracy':  round(ord_acc,  4),
        'inclusion_accuracy': round(inc_acc,  4),
        'overall_accuracy':   round(overall,  4),
    }


def build_session_entry(plan_date, gen_plan, act_plan, correction_log,
                        acc, added, reinforced, conflicts):
    n_gen = sum(len(s['coils']) for s in gen_plan['sections'])
    n_act = sum(len(s['coils']) for s in act_plan['sections'])
    return {
        'session_date':       date.today().isoformat(),
        'wip_date':           str(plan_date),
        'total_coils_generated': n_gen,
        'total_coils_actual':    n_act,
        'coils_matched':         n_act - sum(
            1 for c in correction_log if c['type'] == 'coil_added'),
        'section_accuracy':   acc['section_accuracy'],
        'sort_accuracy':      acc['ordering_accuracy'],
        'inclusion_accuracy': acc['inclusion_accuracy'],
        'overall_accuracy':   acc['overall_accuracy'],
        'corrections_by_type': {
            'section_assignment': sum(1 for c in correction_log
                                      if c['type'] == 'section_assignment'),
            'mill_assignment':    sum(1 for c in correction_log
                                      if c['type'] == 'mill_assignment'),
            'ordering':           sum(1 for c in correction_log
                                      if c['type'] == 'ordering'),
            'excluded':           sum(1 for c in correction_log
                                      if c['type'] == 'coil_excluded'),
            'added':              sum(1 for c in correction_log
                                      if c['type'] == 'coil_added'),
            'rt_value':           sum(1 for c in correction_log
                                      if c['type'] == 'rt_value'),
            'customer_abbrev':    sum(1 for c in correction_log
                                      if c['type'] == 'customer_abbrev'),
            'header_wording':     sum(1 for c in correction_log
                                      if c['type'] == 'header_wording'),
            'section_split':      sum(1 for c in correction_log
                                      if c['type'] == 'section_split'),
        },
        'new_rules_added':    added,
        'rules_reinforced':   reinforced,
        'conflicts_flagged':  conflicts,
    }


# ---------------------------------------------------------------------------
# Top-level learn function
# ---------------------------------------------------------------------------
def learn(generated_path, actual_path, db_path, plan_date=None, verbose=True):
    """
    Full learn session:
      1. Backup DB
      2. Diff plans
      3. Extract patterns → update DB
      4. Compute accuracy
      5. Save DB
      6. Print session log
    """
    backup_path = backup_db(db_path)
    learning_db = load_db(db_path)

    correction_log, gen_plan, act_plan = diff_plans(
        generated_path, actual_path, plan_date)

    n_act = sum(len(s['coils']) for s in act_plan['sections'])
    added, reinforced, conflicts = extract_and_update(
        correction_log, learning_db, gen_plan, act_plan)

    acc = calculate_accuracy(correction_log, n_act)
    session = build_session_entry(plan_date or act_plan['date'],
                                  gen_plan, act_plan, correction_log,
                                  acc, added, reinforced, conflicts)
    learning_db['session_log'].append(session)

    # Update rolling accuracy
    prev_acc = learning_db.get('cumulative_accuracy', 0.0)
    n_sess   = learning_db.get('total_sessions', 0) + 1
    cum_acc  = (prev_acc * (n_sess - 1) + acc['overall_accuracy']) / n_sess
    learning_db['cumulative_accuracy'] = round(cum_acc, 4)
    learning_db['total_sessions']      = n_sess

    save_db(learning_db, db_path)

    if verbose:
        _print_session_log(generated_path, actual_path, session,
                           correction_log, backup_path)

    return session, correction_log


def _print_session_log(gen_path, act_path, session, correction_log, backup_path):
    ct = session['corrections_by_type']
    print("=" * 60)
    print(f"SESSION: {session['session_date']}  |  WIP Date: {session['wip_date']}")
    print("=" * 60)
    print(f"Generated plan : {gen_path}")
    print(f"Actual plan    : {act_path}")
    print(f"\nCOILS GENERATED : {session['total_coils_generated']}")
    print(f"COILS IN ACTUAL : {session['total_coils_actual']}")
    print("\nCORRECTIONS FOUND:")
    for ctype, count in ct.items():
        if count:
            print(f"  {ctype:<25s}: {count}")
    print("\nACCURACY THIS SESSION:")
    print(f"  Section accuracy     : {session['section_accuracy']*100:.1f}%")
    print(f"  Ordering accuracy    : {session['sort_accuracy']*100:.1f}%")
    print(f"  Inclusion accuracy   : {session['inclusion_accuracy']*100:.1f}%")
    print(f"  OVERALL ACCURACY     : {session['overall_accuracy']*100:.1f}%")
    print(f"\nRULES UPDATED:")
    print(f"  + NEW RULES ADDED    : {session['new_rules_added']}")
    print(f"  ✓ RULES REINFORCED   : {session['rules_reinforced']}")
    print(f"  ⚠ CONFLICTS FLAGGED  : {session['conflicts_flagged']}")
    if backup_path:
        print(f"\nBACKUP saved: {backup_path}")
    print("=" * 60)
