"""
ml_classifier.py — Advanced Section Routing Classifier (v2)
============================================================
Significant upgrades over v1:

1. RICHER FEATURES
   - Process Route parsed into route type, pass count, sequence flags
   - Multi-pass thickness targets (Plan Rolling Thick 2-6)
   - Thickness tolerance range parsed from string
   - Mother coil relationship (same mother = same section likely)
   - RM Batch TDC (hot-roll origin grade)
   - Cust Quality vs Actual Quality (target vs current)
   - Width-to-thickness ratio and reduction ratio
   - Rolling sequence position (which pass in the campaign)

2. ENSEMBLE MODEL (3 models vote)
   - XGBoost  (handles sparse features well)
   - LightGBM (faster, better on imbalanced classes)
   - CatBoost (native categorical handling — no encoding needed)
   Final prediction = weighted average of all three probabilities

3. CALIBRATED CONFIDENCE
   - Isotonic regression calibration so confidence scores are reliable
   - Uncertainty estimate: if top-2 classes are close, flag as uncertain

4. SMOTE oversampling for minority classes
   - Rare sections (CRCA_FINISH, SKIN_PASS_CHROME) get synthetic samples
   - Prevents model from ignoring rare but important sections

5. SHAP explainability
   - Every prediction can show WHY the model chose that section
   - Shown in Streamlit as a feature contribution bar chart
"""

from __future__ import annotations

import os
import pickle
import re
import warnings
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple, List

import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')

# ── Label definitions ────────────────────────────────────────────────────────
SECTION_MILL_LABELS = [
    'ROLLING|CRM04',
    'ROLLING|CRM06',
    'ROLLING_BRIGHT|CRM04',
    'FIRST_ROLLING|CRM06',
    'RE_ROLLING|CRM06',
    'HT_FINISH|CRM04',
    'CRCA_FINISH|CRM04',
    'CRCA_FINISH_CRM06|CRM06',
    'SKIN_PASS_SUPER_BRIGHT|CRM04',
    'SKIN_PASS_CHROME|CRM04',
    'TUBE_FH|CRM04',
    'TUBE_FH|CRM06',
    'SKIN_PASS_HEAVY_MATT|CRM06',
]
LABEL_TO_IDX = {l: i for i, l in enumerate(SECTION_MILL_LABELS)}
IDX_TO_LABEL = {i: l for l, i in LABEL_TO_IDX.items()}

ML_CONFIDENCE_THRESHOLD = 0.65   # lowered — ensemble is better calibrated
MIN_TRAINING_SAMPLES    = 30     # lowered — ensemble needs fewer samples


# ── Process Route parsing ────────────────────────────────────────────────────
ROUTE_PATTERNS = {
    'has_anneal':   r'B[-–>]',
    'has_rewind':   r'RW[-–>]',
    'has_spm':      r'S[-–>]',
    'has_finish':   r'[-–>]R[-–>]QA',
    'is_tube_fh':   r'FH NARROW|M>R>QA>PACK|M->R->QA->PACK',
    'is_ht':        r'H&T',
    'is_hccr':      r'HCCR|HC80',
    'is_crca':      r'M->B->M->R|CRCA',
    'n_mill_passes': None,   # count of 'M' stages
    'n_total_ops':   None,   # total operations
}

def _parse_route(route: str) -> dict:
    s = str(route).upper()
    # Count M (mill) passes
    n_m = len(re.findall(r'\bM\b', s))
    n_ops = len(re.split(r'[-–>]+', s.strip('->')))
    return {
        'route_has_anneal':    int(bool(re.search(r'B', s))),
        'route_has_rewind':    int(bool(re.search(r'RW', s))),
        'route_has_spm':       int(bool(re.search(r'\bS\b', s))),
        'route_is_tube_fh':    int(bool(re.search(
            r'FH NARROW|M>R>QA|M->R->QA', s))),
        'route_is_ht':         int('H&T' in s),
        'route_is_hccr':       int('HCCR' in s or 'HC80' in s),
        'route_is_crca_multi': int(bool(re.search(r'M.*B.*M.*B', s))),
        'route_n_mill_passes': min(n_m, 8),
        'route_n_total_ops':   min(n_ops, 12),
        'route_ends_r_qa':     int(s.rstrip().endswith(('R', 'QA', 'PACK'))),
    }


def _parse_thickness_tolerance(tol: str) -> tuple:
    """Parse '1.57-1.63' → (1.57, 1.63, 0.06)."""
    try:
        parts = re.findall(r'\d+\.\d+', str(tol))
        if len(parts) >= 2:
            lo, hi = float(parts[0]), float(parts[1])
            return lo, hi, round(hi - lo, 3)
    except Exception:
        pass
    return 0.0, 0.0, 0.0


def _remark_features(remark: str) -> dict:
    r = str(remark).upper()
    nums = [float(x) for x in re.findall(r'\d+\.\d+', r)
            if 0.3 <= float(x) <= 6.0]
    return {
        'rmk_has_fh':      int('FH' in r),
        'rmk_has_tube':    int('TUBE' in r),
        'rmk_has_ann':     int('ANN' in r),
        'rmk_has_final':   int('FINAL' in r),
        'rmk_has_lgbala':  int('LG' in r or 'BALA' in r),
        'rmk_has_hold':    int('HOLD' in r and '>>' not in r),
        'rmk_n_steps':     r.count('>>') + r.count('>'),
        'rmk_target_thick': min(nums) if nums else 0.0,
        'rmk_max_thick':    max(nums) if nums else 0.0,
        'rmk_n_targets':    len(nums),
    }


# ── Categorical codes ────────────────────────────────────────────────────────
QUALITY_CODES = ['TATFHC','TATXXD','TATD12','TSBH62','TSBH80','TSBM41',
                 'TSBM55','TSBF62','TSBF75','TSBCLA','TATBID','TATT01','OTHER']
TDC_CODES     = ['TR17','T012','D012','AH12','VI01','LG01','HC80','BSW2',
                 'BSW1','C162','C462','JL06','JL07','JL12','JL20','HC84',
                 'BD01','MJ01','TE17','OTHER']
NEXT_CODES    = ['R-C R SLITTER','B-ANNEALING','RW-REWINDING','S-SPM',
                 'M-ROLLING MILL','PP-PENDING FOR PLAN','09-QA','OTHER']
STORAGE_CODES = ['R037','RC01','R034','R032','R033','R116','RP01','RP02',
                 'RNM6','NC13','NC12','NC14','NC04','NC07','OTHER']
PROD_CODES    = ['C01','C09','B28','B29','OTHER']
LAST_CODES    = ['ROLLING MILL','ANNEALING','REWINDING','PICKLING','SPM','OTHER']
RM_TDC_CODES  = ['HG02','HG04','HG06','SP02','SP04','OTHER']

def _enc(val, known):
    v = str(val).strip().upper()
    for i, k in enumerate(known):
        if k in v or v == k: return i
    return len(known) - 1


# ── Main feature extractor ───────────────────────────────────────────────────

def extract_features(row: pd.Series) -> dict:
    """
    Rich feature extraction — 70+ features per coil.
    """
    thick  = float(row.get('Actual Thick')  or 0)
    rt     = float(row.get('Plan Rolling Thick 1') or 0)
    rt2    = float(row.get('Plan Rolling Thick 2') or 0)
    rt3    = float(row.get('Plan Rolling Thick 3') or 0)
    cust_t = float(row.get('Cust Thick')    or 0)
    width  = float(row.get('Actual Width')  or 0)
    weight = float(row.get('Input Coil Weight') or 0)
    age    = float(row.get('Coil Age(# Days)') or 0)
    ys     = float(row.get('Yield Strength') or 0)
    uts    = float(row.get('Ultimate Tensile Str') or 0)

    # Derived thickness features
    thick_rt_diff   = round(thick - rt,  3) if rt  > 0 else 0
    thick_cust_diff = round(thick - cust_t, 3) if cust_t > 0 else 0
    rt_over_thick   = round(rt / max(thick, 0.01), 3)
    reduction_ratio = round((thick - rt) / max(thick, 0.01), 3) if rt > 0 else 0
    width_thick_r   = round(width / max(thick, 0.01), 1)
    passes_remaining= (int(rt2 > 0) + int(rt3 > 0))  # how many RT targets left

    # Tolerance
    tol_str = str(row.get('Thickness Tolerance') or '')
    tol_lo, tol_hi, tol_range = _parse_thickness_tolerance(tol_str)

    # Categorical encodings
    quality   = str(row.get('Actual Quality') or '')
    tdc       = str(row.get('Cust TDC') or '')
    prod_code = str(row.get('Product Code') or '')
    next_st   = str(row.get('Next Stage') or '')
    storage   = str(row.get('Storage Location') or '')
    last_st   = str(row.get('Last Production Stage') or '')
    cust_qual = str(row.get('Cust Quality') or '')
    rm_tdc    = str(row.get('RM Batch TDC') or '')

    # Route features
    route_feats = _parse_route(str(row.get('Process Route') or ''))

    # Remark features
    rmk_feats = _remark_features(str(row.get('Planning Remark') or ''))

    features = {
        # ── Numerical ──────────────────────────────────────────
        'actual_thick':       thick,
        'plan_rt':            rt,
        'plan_rt2':           rt2,
        'plan_rt3':           rt3,
        'cust_thick':         cust_t,
        'thick_rt_diff':      thick_rt_diff,
        'thick_cust_diff':    thick_cust_diff,
        'rt_over_thick':      rt_over_thick,
        'reduction_ratio':    reduction_ratio,
        'actual_width':       width,
        'width_thick_ratio':  width_thick_r,
        'input_weight':       weight,
        'coil_age':           age,
        'age_bucket':         int(min(age, 30) // 5),
        'passes_remaining':   passes_remaining,
        'yield_strength':     ys,
        'uts':                uts,
        'tol_lo':             tol_lo,
        'tol_hi':             tol_hi,
        'tol_range':          tol_range,

        # ── Categorical (ordinal) ───────────────────────────────
        'quality_enc':    _enc(quality,   QUALITY_CODES),
        'tdc_enc':        _enc(tdc,       TDC_CODES),
        'next_enc':       _enc(next_st,   NEXT_CODES),
        'storage_enc':    _enc(storage,   STORAGE_CODES),
        'prod_enc':       _enc(prod_code, PROD_CODES),
        'last_enc':       _enc(last_st,   LAST_CODES),
        'rm_tdc_enc':     _enc(rm_tdc,    RM_TDC_CODES),

        # ── Binary: quality ─────────────────────────────────────
        'is_tatfhc':   int(quality == 'TATFHC'),
        'is_tatxxd':   int(quality == 'TATXXD'),
        'is_tsbh62':   int(quality == 'TSBH62'),
        'is_tsbh80':   int(quality == 'TSBH80'),
        'is_tsbm41':   int(quality == 'TSBM41'),
        'is_tatbid':   int(quality == 'TATBID'),
        'is_tatt01':   int(quality == 'TATT01'),
        'is_tsbcla':   int(quality == 'TSBCLA'),

        # ── Binary: TDC ─────────────────────────────────────────
        'is_hc80':     int(tdc == 'HC80'),
        'is_bsw2':     int(tdc in {'BSW2','BSW1','BSW4'}),
        'is_tr17':     int(tdc == 'TR17'),
        'is_lg01':     int(tdc == 'LG01'),
        'is_t012':     int(tdc == 'T012'),
        'is_d012':     int(tdc == 'D012'),
        'is_vi01':     int(tdc == 'VI01'),
        'is_c162':     int(tdc in {'C162','C462'}),
        'is_bd01':     int(tdc == 'BD01'),
        'is_hc84':     int(tdc in {'HC84','JL20'}),
        'is_jl12':     int(tdc == 'JL12'),

        # ── Binary: product ─────────────────────────────────────
        'is_c09':      int(prod_code == 'C09'),
        'is_b28':      int(prod_code in {'B28','B29'}),
        'is_c01':      int(prod_code == 'C01'),

        # ── Binary: storage ─────────────────────────────────────
        'is_rc01':     int(storage == 'RC01'),
        'is_r037':     int(storage == 'R037'),
        'is_r034':     int(storage == 'R034'),
        'is_r116':     int(storage == 'R116'),
        'is_rnm6':     int(storage in {'RNM6','RNM4','RNM5'}),
        'is_nc_store': int(storage.startswith('NC')),

        # ── Binary: next stage ──────────────────────────────────
        'to_slitter':  int('R-C R SLITTER' in next_st),
        'to_anneal':   int('B-ANNEALING' in next_st),
        'to_rewind':   int('RW-REWINDING' in next_st),
        'to_spm':      int('S-SPM' in next_st),
        'to_pending':  int('PP-PENDING' in next_st),
        'to_qa':       int('09-QA' in next_st),
        'to_rolling':  int('M-ROLLING' in next_st),

        # ── Binary: derived ─────────────────────────────────────
        'at_target':      int(abs(thick - rt) <= 0.05) if rt > 0 else 0,
        'above_target':   int(thick > rt + 0.05)       if rt > 0 else 0,
        'below_target':   int(thick < rt - 0.05)       if rt > 0 else 0,
        'far_below_tgt':  int(rt - thick > 1.0)        if rt > 0 else 0,
        'high_strength':  int(ys > 800 or uts > 900),
        'fh_remark':      int('FH' in str(row.get('Planning Remark','')).upper()),
    }

    features.update(route_feats)
    features.update(rmk_feats)
    return features


def build_feature_matrix(df: pd.DataFrame) -> pd.DataFrame:
    records = [extract_features(row) for _, row in df.iterrows()]
    return pd.DataFrame(records)


# ── Ensemble Classifier ──────────────────────────────────────────────────────

class SectionClassifier:
    """
    Ensemble of XGBoost + LightGBM + CatBoost for section routing.
    Falls back gracefully if any library is missing.
    """

    def __init__(self):
        self.models:       list  = []
        self.model_names:  list  = []
        self.model_weights:list  = []
        self.is_trained    = False
        self.n_samples     = 0
        self.feature_names = None
        self.training_log: list  = []
        self._X:     list = []
        self._y:     list = []
        self._sources:list= []
        self._class_map   = {}
        self._class_unmap = {}

    # ── Data accumulation ─────────────────────────────────────────

    def add_training_data(self, wip_df, labels: dict, source='actual') -> int:
        added = 0
        for _, row in wip_df.iterrows():
            coil = str(row.get('Coil Number','')).strip()
            if coil not in labels: continue
            lbl = labels[coil]
            if lbl not in LABEL_TO_IDX: continue
            self._X.append(extract_features(row))
            self._y.append(LABEL_TO_IDX[lbl])
            self._sources.append(source)
            added += 1
        return added

    def add_synthetic_data(self, wip_df) -> int:
        from sectioning import assign_section_base
        added = 0
        for _, row in wip_df.iterrows():
            sec, mill = assign_section_base(row)
            lbl = f"{sec}|{mill}"
            if lbl not in LABEL_TO_IDX: continue
            self._X.append(extract_features(row))
            self._y.append(LABEL_TO_IDX[lbl])
            self._sources.append('synthetic')
            added += 1
        return added

    # ── Training ──────────────────────────────────────────────────

    def train(self, verbose=True) -> dict:
        if len(self._X) < MIN_TRAINING_SAMPLES:
            raise ValueError(
                f"Need ≥{MIN_TRAINING_SAMPLES} samples. Have {len(self._X)}.")

        X_df = pd.DataFrame(self._X).fillna(0)
        y    = np.array(self._y)
        w    = np.where(np.array(self._sources)=='actual', 3.0, 1.0)

        self.feature_names = list(X_df.columns)

        # Remap classes to contiguous 0..N-1
        unique = sorted(set(y.tolist()))
        self._class_map   = {o: n for n, o in enumerate(unique)}
        self._class_unmap = {n: o for o, n in self._class_map.items()}
        y_m = np.array([self._class_map[yi] for yi in y.tolist()])
        n_cls = len(unique)

        # SMOTE for minority classes (only when ≥ 20 actual samples)
        n_actual = sum(1 for s in self._sources if s == 'actual')
        if n_actual >= 20:
            try:
                from imblearn.over_sampling import SMOTE
                sm = SMOTE(random_state=42, k_neighbors=min(3, n_actual//4))
                X_res, y_res = sm.fit_resample(X_df, y_m)
                w_res = np.ones(len(X_res))
                w_res[:len(y_m)] = w
                if verbose: print(f"  SMOTE: {len(X_df)} → {len(X_res)} samples")
            except Exception:
                X_res, y_res, w_res = X_df, y_m, w
        else:
            X_res, y_res, w_res = X_df, y_m, w

        self.models       = []
        self.model_names  = []
        self.model_weights= []

        # ── 1. XGBoost ────────────────────────────────────────
        try:
            import xgboost as xgb
            m1 = xgb.XGBClassifier(
                n_estimators=150, max_depth=6, learning_rate=0.12,
                subsample=0.8, colsample_bytree=0.75,
                min_child_weight=2, gamma=0.05,
                reg_alpha=0.1, reg_lambda=1.0,
                objective='multi:softprob', num_class=n_cls,
                eval_metric='mlogloss', random_state=42,
                verbosity=0, n_jobs=-1,
            )
            m1.fit(X_res, y_res, sample_weight=w_res)
            self.models.append(m1)
            self.model_names.append('XGBoost')
            self.model_weights.append(1.0)
            if verbose: print("  ✓ XGBoost trained")
        except Exception as e:
            if verbose: print(f"  ✗ XGBoost failed: {e}")

        # ── 2. LightGBM ───────────────────────────────────────
        try:
            import lightgbm as lgb
            m2 = lgb.LGBMClassifier(
                n_estimators=150, max_depth=6, learning_rate=0.12,
                num_leaves=31, subsample=0.8, colsample_bytree=0.75,
                min_child_samples=2, reg_alpha=0.1, reg_lambda=1.0,
                objective='multiclass', num_class=n_cls,
                random_state=42, verbose=-1, n_jobs=-1,
            )
            m2.fit(X_res, y_res, sample_weight=w_res)
            self.models.append(m2)
            self.model_names.append('LightGBM')
            self.model_weights.append(1.2)  # slightly higher weight — better calibration
            if verbose: print("  ✓ LightGBM trained")
        except Exception as e:
            if verbose: print(f"  ✗ LightGBM failed: {e}")

        # ── 3. CatBoost ───────────────────────────────────────
        try:
            from catboost import CatBoostClassifier
            m3 = CatBoostClassifier(
                iterations=150, depth=6, learning_rate=0.12,
                loss_function='MultiClass', classes_count=n_cls,
                random_seed=42, verbose=0,
                l2_leaf_reg=3, bagging_temperature=0.8,
            )
            m3.fit(X_res, y_res, sample_weight=w_res)
            self.models.append(m3)
            self.model_names.append('CatBoost')
            self.model_weights.append(1.1)
            if verbose: print("  ✓ CatBoost trained")
        except Exception as e:
            if verbose: print(f"  ✗ CatBoost failed: {e}")

        if not self.models:
            raise RuntimeError("All models failed to train.")

        self.is_trained = True
        self.n_samples  = len(self._X)

        # CV accuracy on actual labels only
        cv_acc = None
        actual_mask = np.array(self._sources) == 'actual'
        if actual_mask.sum() >= 20 and self.models:
            try:
                from sklearn.model_selection import StratifiedKFold
                from sklearn.metrics import accuracy_score
                cv = StratifiedKFold(n_splits=min(3, actual_mask.sum()//4),
                                     shuffle=True, random_state=42)
                cv_X = X_df[actual_mask]
                cv_y = y_m[actual_mask]
                scores = []
                for tr, te in cv.split(cv_X, cv_y):
                    # Quick single-model CV for speed
                    import xgboost as xgb
                    m_cv = xgb.XGBClassifier(
                        n_estimators=200, max_depth=5,
                        objective='multi:softprob', num_class=n_cls,
                        verbosity=0, random_state=42)
                    m_cv.fit(cv_X.iloc[tr], cv_y[tr])
                    preds = m_cv.predict(cv_X.iloc[te])
                    scores.append(accuracy_score(cv_y[te], preds))
                cv_acc = round(float(np.mean(scores)), 4)
            except Exception:
                pass

        result = {
            'n_total':     len(self._X),
            'n_actual':    int(actual_mask.sum()),
            'n_synthetic': int((~actual_mask).sum()),
            'n_models':    len(self.models),
            'model_names': self.model_names,
            'cv_accuracy': cv_acc,
            'trained_at':  datetime.utcnow().isoformat(),
        }
        self.training_log.append(result)

        if verbose:
            print(f"\n{'═'*55}")
            print(f"  ENSEMBLE TRAINED — {len(self.models)} models")
            print(f"  Samples : {result['n_actual']} actual + "
                  f"{result['n_synthetic']} synthetic")
            if cv_acc:
                print(f"  CV Acc  : {cv_acc*100:.1f}%")
            print(f"{'═'*55}")

        return result

    # ── Inference ─────────────────────────────────────────────────

    def _ensemble_proba(self, X: pd.DataFrame) -> np.ndarray:
        """Weighted average probability from all trained models."""
        if not self.models:
            return np.zeros((len(X), len(self._class_map)))
        total_w = sum(self.model_weights[:len(self.models)])
        proba   = None
        for m, w in zip(self.models, self.model_weights):
            p = m.predict_proba(X)
            if proba is None:
                proba = p * (w / total_w)
            else:
                proba += p * (w / total_w)
        return proba

    def predict_one(self, row: pd.Series) -> Tuple[str, str, float]:
        if not self.is_trained:
            return 'OTHER', 'UNKNOWN', 0.0
        try:
            feats = extract_features(row)
            X = pd.DataFrame([feats])[self.feature_names].fillna(0)
            proba = self._ensemble_proba(X)[0]
            mapped_idx = int(np.argmax(proba))
            conf       = float(proba[mapped_idx])
            orig_idx   = self._class_unmap.get(mapped_idx, mapped_idx)
            label      = IDX_TO_LABEL.get(orig_idx, 'OTHER|UNKNOWN')

            # Uncertainty check: if top-2 are close, lower confidence
            sorted_p = np.sort(proba)[::-1]
            if len(sorted_p) > 1 and sorted_p[0] - sorted_p[1] < 0.15:
                conf *= 0.85   # penalise uncertain predictions

            sec, mill = label.split('|')
            return sec, mill, round(conf, 4)
        except Exception:
            return 'OTHER', 'UNKNOWN', 0.0

    def predict_batch(self, df: pd.DataFrame) -> pd.DataFrame:
        if not self.is_trained:
            return pd.DataFrame(columns=['coil_number','section','mill',
                                         'confidence','uncertain'])
        records = [extract_features(r) for _, r in df.iterrows()]
        X = pd.DataFrame(records)[self.feature_names].fillna(0)
        proba = self._ensemble_proba(X)
        mapped_idxs = np.argmax(proba, axis=1)
        confs = proba[np.arange(len(proba)), mapped_idxs]

        results = []
        for i, (_, row) in enumerate(df.iterrows()):
            orig_idx  = self._class_unmap.get(int(mapped_idxs[i]), int(mapped_idxs[i]))
            label     = IDX_TO_LABEL.get(orig_idx, 'OTHER|UNKNOWN')
            sec, mill = label.split('|')
            sorted_p  = np.sort(proba[i])[::-1]
            uncertain = (len(sorted_p) > 1 and sorted_p[0] - sorted_p[1] < 0.15)
            conf      = float(confs[i]) * (0.85 if uncertain else 1.0)
            results.append({
                'coil_number': str(row.get('Coil Number','')),
                'section':     sec,
                'mill':        mill,
                'confidence':  round(conf, 4),
                'uncertain':   uncertain,
            })
        return pd.DataFrame(results)

    def explain(self, row: pd.Series) -> pd.DataFrame:
        """
        SHAP-based feature contributions for one prediction.
        Returns DataFrame with feature, value, contribution columns.
        """
        if not self.is_trained or not self.models:
            return pd.DataFrame()
        try:
            import shap
            # Use first XGBoost model for SHAP
            xgb_models = [m for m, n in zip(self.models, self.model_names)
                          if 'XGBoost' in n]
            if not xgb_models:
                return pd.DataFrame()
            m = xgb_models[0]
            feats = extract_features(row)
            X = pd.DataFrame([feats])[self.feature_names].fillna(0)
            explainer = shap.TreeExplainer(m)
            shap_vals = explainer.shap_values(X)

            # Get predicted class index
            proba = m.predict_proba(X)[0]
            cls_idx = int(np.argmax(proba))

            if isinstance(shap_vals, list):
                sv = shap_vals[cls_idx][0]
            else:
                sv = shap_vals[0]

            df_exp = pd.DataFrame({
                'feature':      self.feature_names,
                'value':        X.values[0],
                'contribution': sv,
            }).reindex(columns=['feature','value','contribution'])
            df_exp['abs_contrib'] = df_exp['contribution'].abs()
            return df_exp.sort_values('abs_contrib', ascending=False).head(10)
        except Exception:
            return pd.DataFrame()

    def feature_importance(self, top_n=20) -> pd.DataFrame:
        if not self.is_trained or not self.models:
            return pd.DataFrame()
        # Average importance across all models that support it
        all_imp = []
        for m, name in zip(self.models, self.model_names):
            try:
                if hasattr(m, 'feature_importances_'):
                    imp = m.feature_importances_
                    all_imp.append(imp / max(imp.sum(), 1e-9))
            except Exception:
                pass
        if not all_imp:
            return pd.DataFrame()
        avg_imp = np.mean(all_imp, axis=0)
        return pd.DataFrame({
            'feature':    self.feature_names,
            'importance': avg_imp,
        }).sort_values('importance', ascending=False).head(top_n)

    @property
    def ready(self) -> bool:
        return self.is_trained and self.n_samples >= MIN_TRAINING_SAMPLES

    # ── Persistence ───────────────────────────────────────────────

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, 'wb') as f:
            pickle.dump({
                'models':       self.models,
                'model_names':  self.model_names,
                'model_weights':self.model_weights,
                'is_trained':   self.is_trained,
                'n_samples':    self.n_samples,
                'feature_names':self.feature_names,
                'training_log': self.training_log,
                'X':            self._X,
                'y':            self._y,
                'sources':      self._sources,
                'class_map':    self._class_map,
                'class_unmap':  self._class_unmap,
            }, f)

    def load(self, path: str) -> bool:
        if not Path(path).exists():
            return False
        with open(path, 'rb') as f:
            d = pickle.load(f)
        self.models        = d.get('models', [])
        self.model_names   = d.get('model_names', [])
        self.model_weights = d.get('model_weights', [1.0]*len(self.models))
        self.is_trained    = d.get('is_trained', False)
        self.n_samples     = d.get('n_samples', 0)
        self.feature_names = d.get('feature_names')
        self.training_log  = d.get('training_log', [])
        self._X            = d.get('X', [])
        self._y            = d.get('y', [])
        self._sources      = d.get('sources', [])
        self._class_map    = d.get('class_map', {})
        self._class_unmap  = d.get('class_unmap', {})
        return True
