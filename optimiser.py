"""
optimiser.py — Roll Change Minimisation Engine
===============================================
Analyses the generated mill plan section sequence for CRM04 and CRM06,
calculates total roll-change downtime, then finds the optimal section
order that minimises roll changes while respecting hard constraints.

Key constraints that must never be violated:
  1. Width cascade within each section (already enforced by generator)
  2. SKIN_PASS_CHROME must complete its full campaign before any roll change
     (chrome rolls are expensive; partial campaigns damage roll surface)
  3. TUBE_FH must stay last or second-last on CRM04 (heat + contamination risk)
  4. SKIN_PASS_HEAVY_MATT must stay last on CRM06 (heavy scale contaminates)
  5. Section types that share a roll type can be merged into one campaign
     (e.g. HT_FINISH + CRCA_FINISH + TUBE_FH all run on BRIGHT rolls)
"""

from itertools import permutations
from typing import List, Dict, Tuple

# ── Roll type for every section key ────────────────────────────────────────
ROLL_TYPE = {
    'ROLLING':                 'LIGHT_MATT',
    'ROLLING_BRIGHT':          'BRIGHT',
    'FIRST_ROLLING':           'LIGHT_MATT',
    'RE_ROLLING':              'LIGHT_MATT',
    'HT_FINISH':               'BRIGHT',
    'CRCA_FINISH':             'BRIGHT',
    'CRCA_FINISH_CRM06':       'BRIGHT',
    'SKIN_PASS_SUPER_BRIGHT':  'SUPER_BRIGHT',
    'SKIN_PASS_CHROME':        'CHROME_PLATED',
    'TUBE_FH':                 'BRIGHT',
    'SKIN_PASS_HEAVY_MATT':    'HEAVY_MATT',
}

# ── Roll change time in minutes (symmetric) ─────────────────────────────────
# Based on CRM Sahibabad typical changeover data
ROLL_CHANGE_MINUTES = {
    frozenset(['LIGHT_MATT',    'BRIGHT']):        45,
    frozenset(['BRIGHT',        'SUPER_BRIGHT']):  30,
    frozenset(['BRIGHT',        'CHROME_PLATED']): 50,
    frozenset(['BRIGHT',        'HEAVY_MATT']):    60,
    frozenset(['LIGHT_MATT',    'HEAVY_MATT']):    40,
    frozenset(['LIGHT_MATT',    'SUPER_BRIGHT']):  50,
    frozenset(['LIGHT_MATT',    'CHROME_PLATED']): 60,
    frozenset(['SUPER_BRIGHT',  'CHROME_PLATED']): 25,
    frozenset(['SUPER_BRIGHT',  'HEAVY_MATT']):    55,
    frozenset(['CHROME_PLATED', 'HEAVY_MATT']):    55,
}

# Mill speed (MT/hour) per roll type — used to estimate production gain
MILL_SPEED_MT_HR = {
    'CRM04': {
        'LIGHT_MATT':    18.0,
        'BRIGHT':        14.0,
        'SUPER_BRIGHT':  12.0,
        'CHROME_PLATED': 11.0,
    },
    'CRM06': {
        'LIGHT_MATT':    16.0,
        'BRIGHT':        13.0,
        'HEAVY_MATT':    10.0,
    },
}

# ── Hard ordering constraints ───────────────────────────────────────────────
# (section_key, must_be_position) where position is 'LAST', 'FIRST', or int index
HARD_CONSTRAINTS = {
    'CRM04': {
        'SKIN_PASS_CHROME':    'FIRST',   # chrome campaign must open; never interrupted
        'SKIN_PASS_HEAVY_MATT': None,     # not on CRM04
    },
    'CRM06': {
        'SKIN_PASS_HEAVY_MATT': 'LAST',   # heavy scale, always ends the shift
        'CRCA_FINISH_CRM06':    'BEFORE_TUBE_FH',  # LG Bala must come before Tube FH
    },
}

# Sections that MUST stay grouped (same roll type, never interleave different type between them)
MUST_GROUP = {
    'BRIGHT_CRM04':   ['HT_FINISH', 'CRCA_FINISH', 'TUBE_FH'],   # all bright on CRM04
    'BRIGHT_CRM06':   ['CRCA_FINISH_CRM06', 'TUBE_FH'],          # all bright on CRM06
    'LIGHT_MATT_CRM06': ['FIRST_ROLLING', 'RE_ROLLING', 'ROLLING'], # all matt on CRM06
}


def get_change_cost(roll_a: str, roll_b: str) -> int:
    """Minutes to change from roll_a to roll_b. 0 if same roll type."""
    if roll_a == roll_b:
        return 0
    return ROLL_CHANGE_MINUTES.get(frozenset([roll_a, roll_b]), 45)


def sequence_cost(section_list: List[Dict]) -> Tuple[int, int, int]:
    """
    Given an ordered list of section dicts, return:
        (total_change_minutes, n_changes, n_same_roll_consecutive)
    """
    total_mins = 0
    n_changes  = 0
    n_same     = 0
    for i in range(len(section_list) - 1):
        r1 = ROLL_TYPE.get(section_list[i]['section_key'],   'UNKNOWN')
        r2 = ROLL_TYPE.get(section_list[i+1]['section_key'], 'UNKNOWN')
        cost = get_change_cost(r1, r2)
        if cost > 0:
            total_mins += cost
            n_changes  += 1
        else:
            n_same += 1
    return total_mins, n_changes, n_same


def estimate_extra_mt(saved_minutes: int, mill: str, avg_roll_type: str) -> float:
    """MT that could be produced in the time saved from fewer roll changes."""
    speed = MILL_SPEED_MT_HR.get(mill, {}).get(avg_roll_type, 14.0)
    return round((saved_minutes / 60) * speed, 1)


def _split_by_mill(sections: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    """Split section list into CRM04-only and CRM06-only groups."""
    crm04 = [s for s in sections if s['mill'] in ('CRM04',)]
    crm06 = [s for s in sections if s['mill'] in ('CRM06',)]
    # CRM04/06 combined sections contribute to both mills
    both  = [s for s in sections if s['mill'] == 'CRM04/06']
    return crm04 + both, crm06 + both


def _apply_constraints(ordered: List[Dict], mill: str) -> bool:
    """
    Return True if the ordering satisfies all hard constraints for this mill.
    """
    keys = [s['section_key'] for s in ordered]
    constraints = HARD_CONSTRAINTS.get(mill, {})

    for sec_key, rule in constraints.items():
        if sec_key not in keys:
            continue
        idx = keys.index(sec_key)
        if rule == 'LAST'  and idx != len(keys) - 1:
            return False
        if rule == 'FIRST' and idx != 0:
            return False
        if rule == 'BEFORE_TUBE_FH':
            if 'TUBE_FH' in keys and idx > keys.index('TUBE_FH'):
                return False

    return True


def _greedy_optimise(sections: List[Dict], mill: str) -> List[Dict]:
    """
    Greedy nearest-neighbour: start from each possible section and pick
    the next section that requires the least roll change cost.
    Returns the best ordering found.
    Falls back to brute-force if ≤ 6 sections.
    """
    if not sections:
        return sections

    n = len(sections)
    best_order = sections[:]
    best_cost, _, _ = sequence_cost(sections)

    # Brute-force for small n (≤ 7 — 5040 permutations max)
    if n <= 7:
        for perm in permutations(sections):
            perm_list = list(perm)
            if not _apply_constraints(perm_list, mill):
                continue
            cost, _, _ = sequence_cost(perm_list)
            if cost < best_cost:
                best_cost  = cost
                best_order = perm_list
        return best_order

    # Greedy for larger n
    for start_idx in range(n):
        remaining = sections[:]
        ordered   = [remaining.pop(start_idx)]
        while remaining:
            last_roll = ROLL_TYPE.get(ordered[-1]['section_key'], 'UNKNOWN')
            # Pick next section with minimum roll change cost
            best_next = min(
                remaining,
                key=lambda s: get_change_cost(
                    last_roll, ROLL_TYPE.get(s['section_key'], 'UNKNOWN'))
            )
            ordered.append(best_next)
            remaining.remove(best_next)

        if not _apply_constraints(ordered, mill):
            continue
        cost, _, _ = sequence_cost(ordered)
        if cost < best_cost:
            best_cost  = cost
            best_order = ordered

    return best_order


def optimise_plan(sections: List[Dict]) -> Dict:
    """
    Main entry point.

    Parameters
    ----------
    sections : list of section dicts from generator.build_sections()
               Each dict has: section_key, mill, label, coils_df

    Returns
    -------
    dict with keys:
        crm04_original   : original CRM04 section sequence
        crm04_optimised  : optimised CRM04 sequence
        crm06_original   : original CRM06 sequence
        crm06_optimised  : optimised CRM06 sequence
        crm04_analysis   : detailed analysis dict
        crm06_analysis   : detailed analysis dict
        combined_summary : top-level summary dict
        hints            : list of human-readable suggestion strings
    """
    crm04_secs = [s for s in sections if s['mill'] in ('CRM04', 'CRM04/06')]
    crm06_secs = [s for s in sections if s['mill'] in ('CRM06', 'CRM04/06')]

    def analyse(secs, mill):
        orig_cost, orig_changes, _ = sequence_cost(secs)
        optimised  = _greedy_optimise(secs[:], mill)
        opt_cost, opt_changes, _   = sequence_cost(optimised)

        saved_mins = orig_cost - opt_cost
        saved_changes = orig_changes - opt_changes

        # Dominant roll type for speed lookup
        roll_types = [ROLL_TYPE.get(s['section_key'], 'BRIGHT') for s in secs]
        from collections import Counter
        dominant_roll = Counter(roll_types).most_common(1)[0][0] if roll_types else 'BRIGHT'
        extra_mt = estimate_extra_mt(saved_mins, mill, dominant_roll)

        # Build roll-change event list for original sequence
        events_orig = []
        for i in range(len(secs) - 1):
            r1 = ROLL_TYPE.get(secs[i]['section_key'],   'UNKNOWN')
            r2 = ROLL_TYPE.get(secs[i+1]['section_key'], 'UNKNOWN')
            cost = get_change_cost(r1, r2)
            if cost > 0:
                events_orig.append({
                    'from_section': secs[i]['section_key'],
                    'to_section':   secs[i+1]['section_key'],
                    'from_roll':    r1,
                    'to_roll':      r2,
                    'minutes':      cost,
                })

        events_opt = []
        for i in range(len(optimised) - 1):
            r1 = ROLL_TYPE.get(optimised[i]['section_key'],   'UNKNOWN')
            r2 = ROLL_TYPE.get(optimised[i+1]['section_key'], 'UNKNOWN')
            cost = get_change_cost(r1, r2)
            if cost > 0:
                events_opt.append({
                    'from_section': optimised[i]['section_key'],
                    'to_section':   optimised[i+1]['section_key'],
                    'from_roll':    r1,
                    'to_roll':      r2,
                    'minutes':      cost,
                })

        return {
            'mill':                mill,
            'original_sequence':   [s['section_key'] for s in secs],
            'optimised_sequence':  [s['section_key'] for s in optimised],
            'original_changes':    orig_changes,
            'optimised_changes':   opt_changes,
            'original_downtime_min': orig_cost,
            'optimised_downtime_min': opt_cost,
            'saved_minutes':       saved_mins,
            'saved_changes':       saved_changes,
            'extra_mt_possible':   extra_mt,
            'change_events_original':  events_orig,
            'change_events_optimised': events_opt,
            'optimised_sections':  optimised,
        }

    a04 = analyse(crm04_secs, 'CRM04')
    a06 = analyse(crm06_secs, 'CRM06')

    total_saved = a04['saved_minutes'] + a06['saved_minutes']
    total_saved_changes = a04['saved_changes'] + a06['saved_changes']
    total_extra_mt = a04['extra_mt_possible'] + a06['extra_mt_possible']

    # ── Generate human-readable hints ──────────────────────────────────────
    hints = _generate_hints(a04, a06, total_saved, total_saved_changes, total_extra_mt)

    return {
        'crm04_analysis':  a04,
        'crm06_analysis':  a06,
        'combined_summary': {
            'total_roll_changes_original':  (a04['original_changes'] +
                                             a06['original_changes']),
            'total_roll_changes_optimised': (a04['optimised_changes'] +
                                             a06['optimised_changes']),
            'total_downtime_saved_min':     total_saved,
            'total_extra_mt':               total_extra_mt,
            'optimisation_worthwhile':      total_saved >= 30,
        },
        'hints': hints,
    }


def _generate_hints(a04, a06, total_saved, total_saved_changes, total_extra_mt):
    hints = []

    # ── CRM04 hints ─────────────────────────────────────────────────────
    if a04['saved_changes'] > 0:
        orig_seq = ' → '.join(_short(s) for s in a04['original_sequence'])
        opt_seq  = ' → '.join(_short(s) for s in a04['optimised_sequence'])
        hints.append({
            'mill':     'CRM04',
            'severity': 'HIGH' if a04['saved_minutes'] >= 45 else 'MEDIUM',
            'title':    f"CRM04: Reduce roll changes from "
                        f"{a04['original_changes']} to {a04['optimised_changes']}",
            'detail':   (
                f"Current sequence:  {orig_seq}\n"
                f"Suggested sequence: {opt_seq}\n"
                f"Time saved: {a04['saved_minutes']} min  |  "
                f"Extra production possible: ~{a04['extra_mt_possible']} MT"
            ),
            'action':   _reorder_action(a04),
            'saved_min': a04['saved_minutes'],
            'extra_mt':  a04['extra_mt_possible'],
        })

    if a04['saved_changes'] == 0:
        hints.append({
            'mill':     'CRM04',
            'severity': 'OK',
            'title':    'CRM04: Sequence already optimal',
            'detail':   f"{a04['original_changes']} roll change(s), "
                        f"{a04['original_downtime_min']} min downtime — no improvement possible.",
            'action':   None,
            'saved_min': 0,
            'extra_mt':  0,
        })

    # Specific costly event hints for CRM04
    for evt in a04['change_events_original']:
        if evt['minutes'] >= 50:
            hints.append({
                'mill':     'CRM04',
                'severity': 'WARN',
                'title':    f"Expensive change: {_short(evt['from_section'])} → "
                            f"{_short(evt['to_section'])} costs {evt['minutes']} min",
                'detail':   (
                    f"{evt['from_roll']} → {evt['to_roll']} roll swap.\n"
                    f"Consider grouping all {evt['to_roll']} sections together "
                    f"to avoid returning to this roll type later."
                ),
                'action':   None,
                'saved_min': 0,
                'extra_mt':  0,
            })

    # ── CRM06 hints ─────────────────────────────────────────────────────
    if a06['saved_changes'] > 0:
        orig_seq = ' → '.join(_short(s) for s in a06['original_sequence'])
        opt_seq  = ' → '.join(_short(s) for s in a06['optimised_sequence'])
        hints.append({
            'mill':     'CRM06',
            'severity': 'HIGH' if a06['saved_minutes'] >= 45 else 'MEDIUM',
            'title':    f"CRM06: Reduce roll changes from "
                        f"{a06['original_changes']} to {a06['optimised_changes']}",
            'detail':   (
                f"Current sequence:  {orig_seq}\n"
                f"Suggested sequence: {opt_seq}\n"
                f"Time saved: {a06['saved_minutes']} min  |  "
                f"Extra production possible: ~{a06['extra_mt_possible']} MT"
            ),
            'action':   _reorder_action(a06),
            'saved_min': a06['saved_minutes'],
            'extra_mt':  a06['extra_mt_possible'],
        })

    if a06['saved_changes'] == 0:
        hints.append({
            'mill':     'CRM06',
            'severity': 'OK',
            'title':    'CRM06: Sequence already optimal',
            'detail':   f"{a06['original_changes']} roll change(s), "
                        f"{a06['original_downtime_min']} min downtime.",
            'action':   None,
            'saved_min': 0,
            'extra_mt':  0,
        })

    # ── Combined summary hint ────────────────────────────────────────────
    if total_saved >= 30:
        hints.insert(0, {
            'mill':     'BOTH',
            'severity': 'HIGH',
            'title':    f"🏆 Resequencing saves {total_saved} min "
                        f"({total_saved_changes} fewer roll changes) "
                        f"→ ~{total_extra_mt} MT extra production possible",
            'detail':   (
                f"CRM04: saves {a04['saved_minutes']} min, "
                f"+{a04['extra_mt_possible']} MT\n"
                f"CRM06: saves {a06['saved_minutes']} min, "
                f"+{a06['extra_mt_possible']} MT\n\n"
                f"Apply the suggested sequences below to both mills."
            ),
            'action':   None,
            'saved_min': total_saved,
            'extra_mt':  total_extra_mt,
        })

    return hints


def _short(section_key: str) -> str:
    """Short display name for a section key."""
    MAP = {
        'ROLLING':                 'Rolling',
        'ROLLING_BRIGHT':          'Rolling(Bright)',
        'FIRST_ROLLING':           '1st Rolling',
        'RE_ROLLING':              'Re-Rolling',
        'HT_FINISH':               'H&T Finish',
        'CRCA_FINISH':             'CRCA Finish',
        'CRCA_FINISH_CRM06':       'LG Bala Finish',
        'SKIN_PASS_SUPER_BRIGHT':  'SP SuperBright',
        'SKIN_PASS_CHROME':        'SP Chrome',
        'TUBE_FH':                 'Tube FH',
        'SKIN_PASS_HEAVY_MATT':    'SP HeavyMatt',
    }
    return MAP.get(section_key, section_key)


def _reorder_action(analysis: Dict) -> str:
    """Generate a specific action string for the planner."""
    orig = analysis['original_sequence']
    opt  = analysis['optimised_sequence']
    if orig == opt:
        return None

    # Find which sections moved
    moved = []
    for i, (o, n) in enumerate(zip(orig, opt)):
        if o != n:
            moved.append(f"Move '{_short(n)}' to position {i+1}")

    if moved:
        return '\n'.join(moved[:3])  # top 3 moves
    return "Reorder sections as shown above"
