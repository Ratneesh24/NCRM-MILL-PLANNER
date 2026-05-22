"""
Section assignment — the core routing decision tree.

`assign_section(row)` returns (section_key, mill_code) for any WIP coil.

Mill codes used internally: 'CRM04', 'CRM06', 'CRM04/06', 'UNKNOWN'.
"""

from constants import HEAVY_MATT_STORAGE


def _f(v, default=0.0):
    """Safe float."""
    try:
        if v is None:
            return default
        f = float(v)
        if f != f:   # NaN
            return default
        return f
    except (ValueError, TypeError):
        return default


def _s(v):
    """Safe string."""
    if v is None:
        return ''
    s = str(v).strip()
    return '' if s.lower() == 'nan' else s


def assign_section_base(row):
    """
    Apply the base decision tree from the spec (extended with data findings).
    Returns (section_key, mill_code).  Falls back to ('OTHER','UNKNOWN').
    """
    quality    = _s(row.get('Actual Quality'))
    prod_code  = _s(row.get('Product Code'))
    tdc        = _s(row.get('Cust TDC'))
    storage    = _s(row.get('Storage Location'))
    next_stage = _s(row.get('Next Stage'))
    work_ctr   = _s(row.get('Work Center')).upper()
    customer   = _s(row.get('Customer Desc'))
    remark     = _s(row.get('Planning Remark')).upper()
    thick      = _f(row.get('Actual Thick'))
    rt         = _f(row.get('Plan Rolling Thick 1'))
    last_stage = _s(row.get('Last Production Stage')).upper()

    # ── Helper: pick mill from Work Center if available ────────────────
    def _wc_mill(default):
        if 'CRM04' in work_ctr:  return 'CRM04'
        if 'CRM06' in work_ctr:  return 'CRM06'
        return default

    # ── 0. NULL / EMPTY NEXT STAGE — route by quality + last stage ─────
    #   Some SAP records have a blank Next Stage (parsed as empty string).
    #   Apply grade-based routing ignoring next_stage for these coils.
    if not next_stage or next_stage in {'PP-PENDING FOR PLAN'}:
        # TSBM41 / LG01 at finishing gauge → CRCA finish CRM06
        if quality == 'TSBM41' and tdc == 'LG01':
            return 'CRCA_FINISH_CRM06', 'CRM06'
        # H&T grades at target thickness → HT finish
        if prod_code in {'B28', 'B29'} and quality in {'TSBH62', 'TSBH80',
                                                         'TSB75S', 'TSB80C'}:
            if rt <= thick + 0.01:
                return 'HT_FINISH', 'CRM04'
            return 'RE_ROLLING', 'CRM06'
        # Tube grades at SPM last stage → super bright
        if last_stage == 'SPM' and prod_code == 'C01':
            return 'SKIN_PASS_SUPER_BRIGHT', 'CRM04'
        # Tube TATXXD from SPM or rolling → super bright
        if quality == 'TATXXD' and prod_code == 'C01':
            return 'SKIN_PASS_SUPER_BRIGHT', 'CRM04'
        # Non-standard prod codes (778, XXX etc.) → general rolling
        if prod_code in {'778', 'XX', 'XXX', 'YYY'}:
            return 'ROLLING', _wc_mill('CRM04/06')

    # ── 1. TUBE FULL HARD (TATFHC, C09, TR17) ──────────────────────────
    if quality == 'TATFHC' and prod_code == 'C09':
        mill = 'CRM04' if 'CRM04' in work_ctr else \
               'CRM06' if 'CRM06' in work_ctr else 'CRM04/06'
        return 'TUBE_FH', mill

    # ── 2. SKIN-PASS HEAVY MATT (TATBID / BD01) ───────────────────────
    if quality == 'TATBID' or tdc == 'BD01':
        if ('S-SPM' in next_stage or storage.upper() in HEAVY_MATT_STORAGE
                or 'M-ROLLING' in next_stage):
            # BD01 going back to rolling mill is still a heavy-matt candidate
            # unless it's a first-pass scenario; check by thickness
            if thick <= 0.70 and 'M-ROLLING' in next_stage:
                return 'SKIN_PASS_HEAVY_MATT', 'CRM06'
            if 'S-SPM' in next_stage or storage.upper() in HEAVY_MATT_STORAGE:
                return 'SKIN_PASS_HEAVY_MATT', 'CRM06'
        # BD01 / TATBID going to annealing = still skin-pass prep
        if 'B-ANNEALING' in next_stage and thick <= 0.80:
            return 'SKIN_PASS_HEAVY_MATT', 'CRM06'

    # ── 3. ROLLING ON BRIGHT ROLLS (D012 4mm → ~2.02) ─────────────────
    if tdc == 'D012' and thick >= 3.8 and rt <= 2.05 and \
       'RW-REWINDING' in next_stage:
        return 'ROLLING_BRIGHT', 'CRM04'

    # ── 4. SKIN-PASS CHROME PLATED (D012 ~2.02 → ~1.92) ───────────────
    if tdc == 'D012' and 2.0 <= thick <= 2.05 and 0 < rt <= 1.93:
        return 'SKIN_PASS_CHROME', 'CRM04'

    # ── 5. SKIN-PASS SUPER BRIGHT ─────────────────────────────────────
    #   a) TATT01 / MJ01 (Munjal Auto)
    if quality == 'TATT01' or tdc == 'MJ01':
        return 'SKIN_PASS_SUPER_BRIGHT', 'CRM04'

    #   b) T012 / VI01 final pass → slitter
    if (prod_code == 'C01' and tdc in {'T012', 'VI01'}
            and 'R-C R SLITTER' in next_stage):
        return 'SKIN_PASS_SUPER_BRIGHT', 'CRM04'

    #   c) TATXXD / T012 at finish gauge → slitter or QA (last pass)
    if (prod_code == 'C01' and quality == 'TATXXD'
            and 'R-C R SLITTER' in next_stage):
        return 'SKIN_PASS_SUPER_BRIGHT', 'CRM04'

    #   d) TATXXD / T012 going to QA (finished from CRM04 — final pass done)
    if (prod_code == 'C01' and quality == 'TATXXD'
            and tdc in {'T012', 'VI01'}
            and '09-QA' in next_stage
            and last_stage == 'ROLLING MILL'
            and thick <= 2.30):
        return 'SKIN_PASS_SUPER_BRIGHT', 'CRM04'

    #   e) TATXXD / VI01 going to QA (Vaish Industries finish grade)
    if (prod_code == 'C01' and quality == 'TATXXD'
            and tdc == 'VI01'
            and '09-QA' in next_stage):
        return 'SKIN_PASS_SUPER_BRIGHT', 'CRM04'

    #   f) TATXXD / AH12 — after annealing → going to Skin Pass (S-SPM)
    if (prod_code == 'C01' and quality == 'TATXXD'
            and tdc == 'AH12'
            and 'S-SPM' in next_stage):
        return 'SKIN_PASS_SUPER_BRIGHT', 'CRM04'

    #   g) D012 ~2.10 → super bright first skin pass
    if tdc == 'D012' and 2.05 < thick <= 2.15 and 1.95 <= rt <= 2.00:
        return 'SKIN_PASS_SUPER_BRIGHT', 'CRM04'

    #   h) D012 final pass → slitter (already at target, post-SB rolling)
    if (tdc == 'D012' and prod_code == 'C01'
            and 'R-C R SLITTER' in next_stage
            and thick > 2.05):
        return 'SKIN_PASS_SUPER_BRIGHT', 'CRM04'

    #   i) D012 going to QA after CRM04 rolling
    if (tdc == 'D012' and '09-QA' in next_stage
            and last_stage == 'ROLLING MILL' and thick <= 2.15):
        return 'SKIN_PASS_SUPER_BRIGHT', 'CRM04'

    #   j) D012 going to annealing at intermediate gauge (first super-bright pass)
    if (tdc == 'D012' and prod_code == 'C01'
            and 'B-ANNEALING' in next_stage
            and 2.00 < thick <= 2.20):
        return 'SKIN_PASS_SUPER_BRIGHT', 'CRM04'

    # ── 6. CRCA FINISH CRM04 (C09 TSBF*, CH62) ────────────────────────
    if prod_code == 'C09' and ('TSBF' in quality or tdc == 'CH62'):
        if not (tdc == 'JL12' or quality == 'TSBF75'):
            return 'CRCA_FINISH', 'CRM04'

    # ── 7. LG BALA / TSBM41 routing ───────────────────────────────────
    if quality == 'TSBM41' and tdc == 'LG01':
        if 'B-ANNEALING' in next_stage:
            return 'FIRST_ROLLING', 'CRM06'
        if ('R-C R SLITTER' in next_stage or '09-QA' in next_stage
                or 'RW-REWINDING' in next_stage):
            return 'CRCA_FINISH_CRM06', 'CRM06'
        # Still at rolling mill (e.g. next = M-ROLLING MILL) → CRCA finish
        if 'M-ROLLING' in next_stage:
            return 'CRCA_FINISH_CRM06', 'CRM06'

    # ── 8. H&T grades (TSBH62 / TSBH80 / TSB75S / TSB80C) ────────────
    HT_QUALITIES = {'TSBH62', 'TSBH80', 'TSB75S', 'TSB80C'}
    HT_TDCS      = {'C162', 'C462', 'C176', 'C180', 'C280',
                    'BSW1', 'BSW2', 'BSW4'}

    if prod_code in {'B28', 'B29'} or quality in HT_QUALITIES:

        #   a) Final pass → slitter
        if 'R-C R SLITTER' in next_stage:
            return 'HT_FINISH', 'CRM04'

        #   b) Last-pass coils pending / at QA in storage R032/R034
        if ('PP-PENDING FOR PLAN' in next_stage or '09-QA' in next_stage):
            if last_stage in ('ROLLING MILL',) and rt > 0 and rt <= thick:
                return 'HT_FINISH', 'CRM04'

        #   c) Multi-pass H&T: going back to rolling mill
        if 'M-ROLLING' in next_stage:
            # Determine pass number from route depth
            route = _s(row.get('Process Route'))
            if 'H&T' in route.upper():
                # 3+ pass route → RE_ROLLING until last pass
                mill = _wc_mill('CRM06')
                return 'RE_ROLLING', mill

        #   d) B28 going to annealing (furnace)
        if 'H-FURNACE' in next_stage or 'B-ANNEALING' in next_stage:
            # First pass of a multi-pass H&T — goes to CRM06 normally
            if storage == 'R116' or thick >= 2.5:
                return 'FIRST_ROLLING', 'CRM06'
            return 'RE_ROLLING', 'CRM06'

        #   e) B28 going to rewinding (not final pass)
        if 'RW-REWINDING' in next_stage:
            return 'RE_ROLLING', 'CRM06'

    # ── 9. FIRST ROLLING CRM06 (C55 HCCR: HC84/JL20 InSafe/Karam) ─────
    if tdc in {'HC84', 'JL20'} and quality == 'TSBCLA':
        return 'FIRST_ROLLING', 'CRM06'

    # ── 10. FIRST ROLLING CRM06 (B28 heavy via R116) ──────────────────
    if (prod_code == 'B28' and 'B-ANNEALING' in next_stage
            and storage == 'R116'):
        return 'FIRST_ROLLING', 'CRM06'

    # ── 11. RE-ROLLING CRM06 ──────────────────────────────────────────
    if tdc == 'JL12' or quality == 'TSBF75':
        return 'RE_ROLLING', 'CRM06'
    if quality == 'TSBH80' and tdc == 'HC80':
        return 'RE_ROLLING', 'CRM06'

    # ── 12. GENERAL ROLLING — tube / AH12 / D012 heavy gauge ──────────
    if prod_code == 'C01' and tdc in {'AH12', 'TE17', 'JL06', 'JL07'}:
        if ('RW-REWINDING' in next_stage or 'B-ANNEALING' in next_stage
                or 'M-ROLLING' in next_stage or 'S-SPM' in next_stage
                or '09-QA' in next_stage):
            mill = _wc_mill('CRM04/06')
            return 'ROLLING', mill

    if prod_code == 'C01' and tdc == 'D012' and thick >= 3.5:
        return 'ROLLING', 'CRM04/06'

    if prod_code == 'C01' and quality == 'TSBM55':
        return 'ROLLING', 'CRM04'

    # ── 13. BOX STRAP → ROLLING CRM04 ─────────────────────────────────
    if 'BOX STRAP' in remark or customer.upper() == 'BOX':
        return 'ROLLING', 'CRM04'

    # ── 14. TATXXD / T012 going to rewinding or annealing (first pass) ─
    #   These are tube coils still in their rolling campaign
    if prod_code == 'C01' and quality == 'TATXXD' and tdc in {'T012', 'AH12'}:
        if ('RW-REWINDING' in next_stage or 'B-ANNEALING' in next_stage
                or 'M-ROLLING' in next_stage):
            mill = _wc_mill('CRM04/06')
            return 'ROLLING', mill

    # ── 15. TATD12 / D012 going to rolling or annealing (general pass) ─
    if prod_code == 'C01' and tdc == 'D012' and quality == 'TATD12':
        if ('M-ROLLING' in next_stage or 'B-ANNEALING' in next_stage):
            mill = _wc_mill('CRM04/06')
            return 'ROLLING', mill

    # ── 16. TSBH62 / B28 going back to rolling (multi-pass H&T campaign) ─
    #   These are intermediate passes in a long H&T sequence (e.g. 3.2→2.9→2.2)
    #   that haven't reached final gauge yet — RE_ROLLING at CRM06
    if prod_code == 'B28' and quality in {'TSBH62', 'TSBH80'} \
            and 'M-ROLLING' in next_stage:
        mill = _wc_mill('CRM06')
        return 'RE_ROLLING', mill

    # ── 17. TSBH62 / B28 at QA (finished rolling, pending dispatch) ────
    #   Some coils show Next=09-QA but are still in the rolling sequence.
    #   If RT > Actual Thick, one more pass needed → RE_ROLLING
    if (prod_code == 'B28' and quality in HT_QUALITIES
            and '09-QA' in next_stage
            and last_stage == 'ROLLING MILL'
            and rt > thick + 0.05):      # RT unexpectedly greater = data anomaly
        return 'HT_FINISH', 'CRM04'

    # ── 18. TATXXD / VI01 going back to rolling mill ─────────────────
    if (prod_code == 'C01' and quality == 'TATXXD'
            and tdc == 'VI01'
            and 'M-ROLLING' in next_stage):
        mill = _wc_mill('CRM04')
        return 'SKIN_PASS_SUPER_BRIGHT', mill

    # ── 19. NaN quality / unrecognised grade — heuristic by TDC / route ─
    #   Coils with blank quality but identifiable TDC
    if not quality:
        route = _s(row.get('Process Route'))
        if 'LG BALA' in remark or tdc == 'LG01':
            return 'CRCA_FINISH_CRM06', 'CRM06'
        if 'TUBE' in remark or tdc == 'TR17':
            return 'ROLLING', 'CRM04/06'
        if 'HROP' in remark:               # HR-origin offspec → general rolling
            return 'ROLLING', 'CRM04/06'
        # Route-based fallback: SPM-bound → skin pass
        if '->S->' in route or 'S-SPM' in next_stage:
            return 'SKIN_PASS_SUPER_BRIGHT', 'CRM04'
        # Default unknown to general rolling rather than OTHER
        return 'ROLLING', _wc_mill('CRM04/06')

    return 'OTHER', 'UNKNOWN'


def assign_section_with_learning(row, learning_db=None):
    """
    Enhanced assignment: consult learning_db before falling back to base tree.
    Returns (section_key, mill_code, source_tag).
    """
    coil_num = _s(row.get('Coil Number'))

    if learning_db:
        # 1. Coil-level override always wins
        ov = learning_db.get('coil_overrides', {}).get(coil_num)
        if ov:
            return ov['section'], ov['mill'], 'COIL_OVERRIDE'

        # 2. Grade routing
        quality   = _s(row.get('Actual Quality'))
        tdc       = _s(row.get('Cust TDC'))
        prod_code = _s(row.get('Product Code'))
        nxt       = _s(row.get('Next Stage'))
        key = f"{quality}|{tdc}|{prod_code}|{nxt}"
        rule = learning_db.get('grade_routing', {}).get(key)
        if rule:
            conf = rule.get('confidence', 0)
            if conf >= 3:
                return rule['section'], rule['mill'], 'LEARNED_HARD'
            if conf >= 2:
                return rule['section'], rule['mill'], 'LEARNED_SOFT'

    # 3. Fall back to base tree
    section, mill = assign_section_base(row)
    return section, mill, 'BASE_RULE'
