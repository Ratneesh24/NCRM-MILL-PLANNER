"""
Constants for the Mill Planner — section definitions, headers, colours,
customer abbreviations, storage groupings, etc.

All canonical strings live here so generator/learner/parser stay in sync.
"""

# ---------------------------------------------------------------------------
# Section keys (canonical, internal identifiers)
# ---------------------------------------------------------------------------
SECTION_KEYS = [
    'ROLLING_BRIGHT',          # D012 first heavy pass on bright rolls (CRM04)
    'ROLLING',                 # General rolling on light matt rolls
    'FIRST_ROLLING',           # First pass to annealing (CRM06)
    'RE_ROLLING',              # Intermediate re-rolling (CRM06)
    'HT_FINISH',               # H&T finish on bright rolls (CRM04)
    'CRCA_FINISH',             # CRCA finish CRM04
    'CRCA_FINISH_CRM06',       # LG Bala CRCA finish CRM06
    'SKIN_PASS_SUPER_BRIGHT',  # Tube T012/TATXXD skin pass (CRM04)
    'SKIN_PASS_CHROME',        # D012 chrome plated skin pass (CRM04)
    'TUBE_FH',                 # Tube full hard
    'SKIN_PASS_HEAVY_MATT',    # SFC/TATBID heavy matt skin pass (CRM06)
]

# Order in which sections must appear on the sheet
SECTION_ORDER = [
    'ROLLING_BRIGHT',
    'ROLLING',
    'FIRST_ROLLING',
    'RE_ROLLING',
    'HT_FINISH',
    'CRCA_FINISH',
    'CRCA_FINISH_CRM06',
    'SKIN_PASS_SUPER_BRIGHT',
    'SKIN_PASS_CHROME',
    'TUBE_FH',
    'SKIN_PASS_HEAVY_MATT',
]

# Within a section, prefer CRM04 first when split into per-mill sub-sections
MILL_ORDER = ['CRM04', 'CRM04/06', 'CRM06']


# ---------------------------------------------------------------------------
# Section header label builders
# ---------------------------------------------------------------------------
def _mill_label(mill):
    """Convert internal mill code to display label."""
    return {
        'CRM04': 'CRM-04',
        'CRM06': 'CRM-06',
        'CRM04/06': 'CRM-04/06',
        'CRM04_OR_CRM06': 'CRM-04/06',
    }.get(mill, mill)


SECTION_LABEL_BUILDERS = {
    'ROLLING':
        lambda m: f"ROLLING ON LIGHT MATT ROLLS ---------------AT {_mill_label(m)}",
    'ROLLING_BRIGHT':
        lambda m: f"ROLLING ON BRIGHT ROLLS ---------------AT {_mill_label(m)}",
    'FIRST_ROLLING':
        lambda m: f"FIRST ROLLING ON LIGHT MATT ROLLS ---------------AT {_mill_label(m)}",
    'RE_ROLLING':
        lambda m: f"RE-ROLLING ON LIGHT MATT ROLLS ---------------AT {_mill_label(m)}",
    'HT_FINISH':
        lambda m: f"H&T FINISH ON BRIGHT ROLLS---------------AT {_mill_label(m)} ------------DO NOT APPLY R.P.OIL",
    'CRCA_FINISH':
        lambda m: f"CRCA FINISH  ON BRIGHT ROLLS---------------AT {_mill_label(m)} -----------APPLY R.P.OIL",
    'CRCA_FINISH_CRM06':
        lambda m: f"CRCA FINISH  ON BRIGHT ROLLS---------------AT {_mill_label(m)}-----------APPLY R.P.OIL",
    'SKIN_PASS_SUPER_BRIGHT':
        lambda m: f"SKIN-PASS ON SUPER BRIGHT ROLLS---------------AT {_mill_label(m)} -----------APPLY R.P.OIL",
    'SKIN_PASS_CHROME':
        lambda m: f"SKIN-PASS ON CHROMEPLATED ROLLS---------------AT {_mill_label(m)} -----------APPLY R.P.OIL",
    'TUBE_FH':
        lambda m: f"TUBE FH  ON BRIGHT ROLLS---------------AT {_mill_label(m)} -----------APPLY R.P.OIL",
    'SKIN_PASS_HEAVY_MATT':
        lambda m: f"SKIN-PASS ON HEAVEY MATT ROLLS---------------AT {_mill_label(m)}-----------APPLY R.P.OIL",
}


def build_section_label(section_key, mill):
    """Return canonical header text for a section + mill combination."""
    builder = SECTION_LABEL_BUILDERS.get(section_key)
    if builder:
        return builder(mill)
    return f"{section_key} AT {_mill_label(mill)}"


# ---------------------------------------------------------------------------
# Section colours (light pastel backgrounds, openpyxl hex without '#')
# ---------------------------------------------------------------------------
SECTION_COLOURS = {
    'ROLLING':                 'DCE6F1',   # light blue
    'ROLLING_BRIGHT':          'DCE6F1',
    'FIRST_ROLLING':           'DCE6F1',
    'RE_ROLLING':              'DCE6F1',
    'HT_FINISH':               'EBF1DE',   # light green
    'CRCA_FINISH':             'FFF2CC',   # light yellow
    'CRCA_FINISH_CRM06':       'FFF2CC',
    'SKIN_PASS_SUPER_BRIGHT':  'FCE4D6',   # light orange
    'SKIN_PASS_CHROME':        'F2DCDB',   # light pink
    'TUBE_FH':                 'E2EFDA',   # mint green
    'SKIN_PASS_HEAVY_MATT':    'DDEBF7',   # sky blue
}


# ---------------------------------------------------------------------------
# Section short names for the priority block
# ---------------------------------------------------------------------------
SECTION_SHORT_NAME = {
    'ROLLING':                 'Rolling on Light Matt Rolls',
    'ROLLING_BRIGHT':          'Rolling on Bright Rolls',
    'FIRST_ROLLING':           'First Rolling on Light Matt Rolls',
    'RE_ROLLING':              'Re-Rolling on Light Matt Rolls',
    'HT_FINISH':               'H&T Finish on Bright Rolls',
    'CRCA_FINISH':             'CRCA Finish on Bright Rolls',
    'CRCA_FINISH_CRM06':       'Finish of L.G.Bala on Bright Rolls',
    'SKIN_PASS_SUPER_BRIGHT':  'Skin-Pass on Super Bright Rolls',
    'SKIN_PASS_CHROME':        'Skin-Pass on Chrome Plated Rolls',
    'TUBE_FH':                 'Tube FH on Bright Rolls',
    'SKIN_PASS_HEAVY_MATT':    'Skin-Pass on Heavy Matt Rolls',
}

# Within each mill, priority order (highest first)
CRM04_PRIORITY = [
    'SKIN_PASS_CHROME',
    'SKIN_PASS_SUPER_BRIGHT',
    'HT_FINISH',
    'CRCA_FINISH',
    'ROLLING_BRIGHT',
    'TUBE_FH',
    'ROLLING',
]
CRM06_PRIORITY = [
    'CRCA_FINISH_CRM06',
    'SKIN_PASS_HEAVY_MATT',
    'RE_ROLLING',
    'FIRST_ROLLING',
    'TUBE_FH',
    'ROLLING',
]


# ---------------------------------------------------------------------------
# Customer abbreviations
# ---------------------------------------------------------------------------
CUSTOMER_ABBREV = {
    "L.G BALAKRISHNAN":             "L.G BALA",
    "L.G BALA":                     "L.G BALA",
    "L.G BALAKRISHNAN & BROS":      "L.G BALA",
    "SAHIBABAD TUBE PLANT":         "TUBE",
    "BIJOY TRADING":                "BIJOY",
    "BIJOY":                        "BIJOY",
    "SPECIAL STEEL & STRIP":        "SPECIAL STEEL",
    "BANDSAW STRIP":                "BANDSAW",
    "BANDSAW STRIP CORPORATION":    "BANDSAW",
    "BANDSAW":                      "BANDSAW",
    "MI TOOLS & TRADING":           "MI TOOLS",
    "MI TOOLS":                     "MI TOOLS",
    "STEEL STRIPS":                 "STEEL STRIPS",
    "HIGH STEEL":                   "HIGH STEEL",
    "ANCHOR":                       "ANCHOR",
    "DECOSTYLE":                    "DECOSTYLE",
    "SFC":                          "SFC",
    "VAISH INDUSTRIES":             "VAISH",
    "VAISH":                        "VAISH",
    "S.M.STEEL STRIPS":             "S.M.STEEL",
    "S.M.STEEL":                    "S.M.STEEL",
    "TMA INTERNATIONAL":            "TMA",
    "TMA":                          "TMA",
    "RUPH STRIPS":                  "RUPH",
    "RUPH":                         "RUPH",
    "INSAFE SAFETY":                "INSAFE",
    "INSAFE":                       "INSAFE",
    "SARASWATI":                    "SARASWATI",
    "CALLIDA":                      "CALLIDA",
    "MUNJAL AUTO":                  "MUNJAL",
    "MUNJAL":                       "MUNJAL",
    "KARAM SAFETY":                 "KARAM",
    "KARAM":                        "KARAM",
    "BOX":                          "BOX",
    "PANKAJ":                       "PANKAJ",
    "SAGAR":                        "SAGAR",
    "AGGARWAL":                     "AGGARWAL",
    "S.K.STEEL":                    "S.K.STEEL",
    "S.S.G.N":                      "S.S.G.N",
    "SHIV SHAKTI":                  "SHIV SHAKTI",
    "TRADE CUSTOMER":               "TRADE",
}


def abbreviate_customer(name, learning_db=None):
    """Map a raw Customer Desc to the short form used on the plan."""
    if not name or str(name).strip() == '' or str(name).lower() == 'nan':
        return ''
    raw = str(name).strip()

    # learning_db takes precedence (planner-corrected)
    if learning_db and 'customer_abbrev' in learning_db:
        for key, abbr in learning_db['customer_abbrev'].items():
            if key.upper() in raw.upper():
                return abbr

    for key, abbr in CUSTOMER_ABBREV.items():
        if key.upper() in raw.upper():
            return abbr
    return raw[:12]


# ---------------------------------------------------------------------------
# Storage location buckets used by routing logic
# ---------------------------------------------------------------------------
HEAVY_MATT_STORAGE = {'NC04', 'NC07', 'NC12', 'NC13', 'NC14', 'RC07'}

# Locations that confirm the coil is queued for the rolling section
ROLLING_STORAGE = {'R032', 'R033', 'R034', 'R037', 'R116', 'RC01',
                   'RNM6', 'RP01', 'RP02', 'NC10', 'NC11'}


# ---------------------------------------------------------------------------
# Output column definitions
# ---------------------------------------------------------------------------
OUTPUT_COLUMNS = [
    ('Date',                  9),
    ('Batch',                 7),
    ('Planning/ SO No.',     13),
    ('Thick',                 6),
    ('Width',                 6),
    ('Weight',                8),
    ('RT',                    5),
    ('Customer',             13),
    ('Prod.Code',             7),
    ('Quality Code',          8),
    ('TDC No',                6),
    ('Plant',                 5),
    ('Staorage Loc',          9),     # original typo preserved
    ('Planning Reamrk',      20),     # original typo preserved
    ('Current Work Center', 16),
    ('NEXT Work Center',    14),
    ('Route',                30),
    ('Finish',                7),
    ('Age',                   5),
]
N_COLS = len(OUTPUT_COLUMNS)
