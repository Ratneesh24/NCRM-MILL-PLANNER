"""
ml_classifier.py — XGBoost Section Routing Classifier
=======================================================
Learns to assign coils to mill plan sections from historical data.

Architecture:
  - Features: coil attributes from WIP (Quality, TDC, thickness, width, age etc.)
  - Labels: section + mill from actual planner-made plans
  - Model: XGBoost multi-class classifier
  - Training: grows daily as actual plans are uploaded

Usage:
  from ml_classifier import SectionClassifier
  clf = SectionClassifier()
  clf.train(X_df, y_series)           # train / retrain
  section, mill, confidence = clf.predict_one(row)
  clf.save('models/section_clf.pkl')
  clf.load('models/section_clf.pkl')
"""

from __future__ import annotations

import os
import pickle
import warnings
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

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

# Minimum confidence to use ML prediction over rule engine
ML_CONFIDENCE_THRESHOLD = 0.70

# Minimum training samples before ML is used at all
MIN_TRAINING_SAMPLES = 50


# ── Feature engineering ──────────────────────────────────────────────────────

# Categorical encodings (learned from data, but seeded with known values)
QUALITY_CODES = [
    'TATFHC','TATXXD','TATD12','TSBH62','TSBH80','TSBM41','TSBM55',
    'TSBF62','TSBF75','TSBCLA','TATBID','TATT01','OTHER'
]
TDC_CODES = [
    'TR17','T012','D012','AH12','VI01','LG01','HC80','BSW2','BSW1',
    'C162','C462','JL06','JL07','JL12','JL20','HC84','BD01','MJ01',
    'TE17','OTHER'
]
NEXT_STAGE_CODES = [
    'R-C R SLITTER','B-ANNEALING','RW-REWINDING','S-SPM',
    'M-ROLLING MILL','PP-PENDING FOR PLAN','09-QA','OTHER'
]
STORAGE_CODES = [
    'R037','RC01','R034','R032','R033','R116','RP01','RP02',
    'RNM6','NC13','NC12','NC14','NC04','NC07','OTHER'
]
PROD_CODES = ['C01','C09','B28','B29','OTHER']
LAST_STAGE_CODES = [
    'ROLLING MILL','ANNEALING','REWINDING','PICKLING','SPM','OTHER'
]


def _encode_cat(val: str, known: list) -> int:
    """Ordinal encode a categorical value."""
    val = str(val).strip().upper()
    for i, k in enumerate(known):
        if k in val or val == k:
            return i
    return len(known) - 1   # 'OTHER'


def _remark_features(remark: str) -> dict:
    """Extract numerical features from Planning Remark string."""
    r = str(remark).upper()
    return {
        'rmk_has_fh':      int('FH' in r),
        'rmk_has_tube':    int('TUBE' in r),
        'rmk_has_ann':     int('ANN' in r),
        'rmk_has_final':   int('FINAL' in r),
        'rmk_has_lgbala':  int('LG' in r or 'BALA' in r),
        'rmk_has_hold':    int('HOLD' in r and '>>' not in r),
        'rmk_n_steps':     r.count('>>') + r.count('>'),
        'rmk_target_thick': _parse_rt_from_remark(r),
    }


def _parse_rt_from_remark(remark: str) -> float:
    """Try to extract final target thickness from remark like 'FH 1.60'."""
    import re
    nums = re.findall(r'\d+\.\d+', remark)
    if nums:
        floats = [float(n) for n in nums if 0.3 <= float(n) <= 6.0]
        if floats:
            return min(floats)   # typically the last (smallest) number
    return 0.0


def extract_features(row: pd.Series) -> dict:
    """
    Convert a WIP row into a fixed-size numerical feature vector.
    All features are deterministic — no randomness.
    """
    thick = float(row.get('Actual Thick') or 0)
    rt    = float(row.get('Plan Rolling Thick 1') or 0)
    width = float(row.get('Actual Width') or 0)
    weight= float(row.get('Input Coil Weight') or 0)
    age   = float(row.get('Coil Age(# Days)') or 0)

    features = {
        # Numerical — most important features
        'actual_thick':     thick,
        'plan_rt':          rt,
        'thick_minus_rt':   round(thick - rt, 3),   # positive = above target
        'rt_over_thick':    round(rt / max(thick, 0.01), 3),
        'actual_width':     width,
        'input_weight':     weight,
        'coil_age':         age,
        'age_bucket':       int(min(age, 30) // 5),  # 0-5d, 5-10d, ...

        # Categorical (ordinal encoded)
        'quality_enc':  _encode_cat(str(row.get('Actual Quality','')), QUALITY_CODES),
        'tdc_enc':      _encode_cat(str(row.get('Cust TDC','')), TDC_CODES),
        'next_enc':     _encode_cat(str(row.get('Next Stage','')), NEXT_STAGE_CODES),
        'storage_enc':  _encode_cat(str(row.get('Storage Location','')), STORAGE_CODES),
        'prod_enc':     _encode_cat(str(row.get('Product Code','')), PROD_CODES),
        'last_enc':     _encode_cat(str(row.get('Last Production Stage','')), LAST_STAGE_CODES),

        # Binary flags — derived from categorical
        'is_tatfhc':    int(str(row.get('Actual Quality','')) == 'TATFHC'),
        'is_tatxxd':    int(str(row.get('Actual Quality','')) == 'TATXXD'),
        'is_tsbh62':    int(str(row.get('Actual Quality','')) == 'TSBH62'),
        'is_tsbh80':    int(str(row.get('Actual Quality','')) == 'TSBH80'),
        'is_tsbm41':    int(str(row.get('Actual Quality','')) == 'TSBM41'),
        'is_hc80':      int(str(row.get('Cust TDC','')) == 'HC80'),
        'is_bsw2':      int(str(row.get('Cust TDC','')) in {'BSW2','BSW1','BSW4'}),
        'is_tr17':      int(str(row.get('Cust TDC','')) == 'TR17'),
        'is_lg01':      int(str(row.get('Cust TDC','')) == 'LG01'),
        'is_t012':      int(str(row.get('Cust TDC','')) == 'T012'),
        'is_d012':      int(str(row.get('Cust TDC','')) == 'D012'),
        'is_c09':       int(str(row.get('Product Code','')) == 'C09'),
        'is_b28':       int(str(row.get('Product Code','')) in {'B28','B29'}),
        'is_rc01':      int(str(row.get('Storage Location','')) == 'RC01'),
        'is_r037':      int(str(row.get('Storage Location','')) == 'R037'),
        'is_r034':      int(str(row.get('Storage Location','')) == 'R034'),
        'is_rnm6':      int(str(row.get('Storage Location','')) == 'RNM6'),
        'to_slitter':   int('R-C R SLITTER' in str(row.get('Next Stage',''))),
        'to_anneal':    int('B-ANNEALING' in str(row.get('Next Stage',''))),
        'to_rewind':    int('RW-REWINDING' in str(row.get('Next Stage',''))),
        'to_spm':       int('S-SPM' in str(row.get('Next Stage',''))),
        'to_pending':   int('PP-PENDING' in str(row.get('Next Stage',''))),
        'at_target':    int(abs(thick - rt) <= 0.05) if rt > 0 else 0,
        'above_target': int(thick > rt + 0.05) if rt > 0 else 0,
        'below_target': int(thick < rt - 0.05) if rt > 0 else 0,
    }

    # Remark-derived features
    features.update(_remark_features(str(row.get('Planning Remark', ''))))

    return features


def build_feature_matrix(df: pd.DataFrame) -> pd.DataFrame:
    """Convert a DataFrame of WIP rows into ML feature matrix."""
    records = []
    for _, row in df.iterrows():
        records.append(extract_features(row))
    return pd.DataFrame(records)


# ── Classifier ───────────────────────────────────────────────────────────────

class SectionClassifier:
    """
    XGBoost multi-class classifier for mill plan section routing.

    Lifecycle:
      1. Create instance
      2. Call add_training_data() with each day's WIP + actual labels
      3. Call train() to fit the model
      4. Call predict_one() or predict_batch() for inference
      5. Call save() to persist; load() to restore
    """

    def __init__(self):
        self.model       = None
        self.is_trained  = False
        self.n_samples   = 0
        self.feature_names = None
        self.training_log  = []
        self._X: list = []   # accumulated feature rows
        self._y: list = []   # accumulated labels (int indices)
        self._sources: list = []  # 'actual' or 'synthetic'

    # ── Data accumulation ─────────────────────────────────────────

    def add_training_data(self,
                          wip_df: pd.DataFrame,
                          labels: dict,   # {coil_number: 'SECTION_KEY|MILL'}
                          source: str = 'actual') -> int:
        """
        Add labelled coils to the training set.

        Parameters
        ----------
        wip_df  : WIP DataFrame (already filtered to eligible coils)
        labels  : dict mapping coil number → 'SECTION_KEY|MILL' label string
        source  : 'actual' (real planner decision) or 'synthetic' (rule engine)

        Returns number of coils added.
        """
        added = 0
        for _, row in wip_df.iterrows():
            coil = str(row.get('Coil Number', '')).strip()
            if coil not in labels:
                continue
            label_str = labels[coil]
            if label_str not in LABEL_TO_IDX:
                continue
            feats = extract_features(row)
            self._X.append(feats)
            self._y.append(LABEL_TO_IDX[label_str])
            self._sources.append(source)
            added += 1
        return added

    def add_synthetic_data(self, wip_df: pd.DataFrame) -> int:
        """
        Use the rule engine output as synthetic training labels.
        Actual labels (added via add_training_data) will have higher weight.
        """
        from sectioning import assign_section_base
        added = 0
        for _, row in wip_df.iterrows():
            sec, mill = assign_section_base(row)
            label_str = f"{sec}|{mill}"
            if label_str not in LABEL_TO_IDX:
                continue
            feats = extract_features(row)
            self._X.append(feats)
            self._y.append(LABEL_TO_IDX[label_str])
            self._sources.append('synthetic')
            added += 1
        return added

    # ── Training ──────────────────────────────────────────────────

    def train(self, verbose: bool = True) -> dict:
        """
        Fit XGBoost on all accumulated training data.
        Actual labels get 3x sample weight over synthetic.
        """
        if len(self._X) < MIN_TRAINING_SAMPLES:
            raise ValueError(
                f"Need at least {MIN_TRAINING_SAMPLES} training samples. "
                f"Have {len(self._X)}. Add more actual plan data first.")

        try:
            import xgboost as xgb
        except ImportError:
            raise ImportError("xgboost not installed. Run: pip install xgboost")

        X_df = pd.DataFrame(self._X).fillna(0)
        y    = np.array(self._y)
        weights = np.where(np.array(self._sources) == 'actual', 3.0, 1.0)

        self.feature_names = list(X_df.columns)

        # Remap class indices to contiguous range (XGBoost requires 0..N-1)
        unique_classes = sorted(set(y.tolist()))
        self._class_map    = {orig: new for new, orig in enumerate(unique_classes)}
        self._class_unmap  = {new: orig for orig, new in self._class_map.items()}
        y_mapped = np.array([self._class_map[yi] for yi in y.tolist()])

        self.model = xgb.XGBClassifier(
            n_estimators       = 300,
            max_depth          = 6,
            learning_rate      = 0.1,
            subsample          = 0.8,
            colsample_bytree   = 0.8,
            min_child_weight   = 2,
            gamma              = 0.1,
            reg_alpha          = 0.1,
            reg_lambda         = 1.0,
            objective          = 'multi:softprob',
            num_class          = len(unique_classes),
            eval_metric        = 'mlogloss',
            random_state       = 42,
            verbosity          = 0,
            n_jobs             = -1,
        )
        self.model.fit(X_df, y_mapped, sample_weight=weights)
        self.is_trained  = True
        self.n_samples   = len(self._X)

        # Cross-validation accuracy estimate
        from sklearn.model_selection import cross_val_score
        actual_mask = np.array(self._sources) == 'actual'
        if actual_mask.sum() >= 20:
            cv_X = X_df[actual_mask]
            cv_y = y[actual_mask]
            cv_w = weights[actual_mask]
            try:
                cv_y_mapped = np.array([self._class_map.get(yi, yi)
                                         for yi in cv_y.tolist()])
                cv_scores = cross_val_score(
                    xgb.XGBClassifier(
                        n_estimators=300, max_depth=6,
                        objective='multi:softprob',
                        num_class=len(set(self._class_map.values())),
                        verbosity=0, random_state=42),
                    cv_X, cv_y_mapped,
                    cv=min(5, actual_mask.sum() // 5),
                    scoring='accuracy',
                )
                cv_acc = cv_scores.mean()
            except Exception:
                cv_acc = None
        else:
            cv_acc = None

        result = {
            'n_total':    len(self._X),
            'n_actual':   int(actual_mask.sum()),
            'n_synthetic': int((~actual_mask).sum()),
            'cv_accuracy': round(cv_acc, 4) if cv_acc else None,
            'trained_at': datetime.utcnow().isoformat(),
            'n_classes':  len(SECTION_MILL_LABELS),
        }
        self.training_log.append(result)

        if verbose:
            print(f"\n{'═'*55}")
            print(f"  ML CLASSIFIER TRAINED")
            print(f"{'═'*55}")
            print(f"  Total samples   : {result['n_total']}")
            print(f"  Actual labels   : {result['n_actual']} (3x weight)")
            print(f"  Synthetic labels: {result['n_synthetic']} (1x weight)")
            if cv_acc:
                print(f"  CV Accuracy     : {cv_acc*100:.1f}%  "
                      f"(on actual labels only)")
            else:
                print(f"  CV Accuracy     : need 20+ actual samples for CV")
            print(f"{'═'*55}")

        return result

    # ── Inference ─────────────────────────────────────────────────

    def predict_one(self, row: pd.Series) -> Tuple[str, str, float]:
        """
        Predict section + mill for one coil.

        Returns
        -------
        (section_key, mill_code, confidence)
        confidence = max class probability (0–1)
        """
        if not self.is_trained or self.model is None:
            return 'OTHER', 'UNKNOWN', 0.0

        feats = extract_features(row)
        X = pd.DataFrame([feats])[self.feature_names].fillna(0)
        proba = self.model.predict_proba(X)[0]
        mapped_idx = int(np.argmax(proba))
        conf  = float(proba[mapped_idx])
        orig_idx = getattr(self, '_class_unmap', {}).get(mapped_idx, mapped_idx)
        label = IDX_TO_LABEL.get(orig_idx, 'OTHER|UNKNOWN')
        section, mill = label.split('|')
        return section, mill, conf

    def predict_batch(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Predict for all rows in a DataFrame.
        Returns DataFrame with columns: coil_number, section, mill, confidence.
        """
        if not self.is_trained or self.model is None:
            return pd.DataFrame(columns=['coil_number','section','mill','confidence'])

        records = [extract_features(row) for _, row in df.iterrows()]
        X = pd.DataFrame(records)[self.feature_names].fillna(0)
        proba  = self.model.predict_proba(X)
        mapped_idxs = np.argmax(proba, axis=1)
        confs  = proba[np.arange(len(proba)), mapped_idxs]
        class_unmap = getattr(self, '_class_unmap', {})

        results = []
        for i, (_, row) in enumerate(df.iterrows()):
            orig_idx = class_unmap.get(int(mapped_idxs[i]), int(mapped_idxs[i]))
            label = IDX_TO_LABEL.get(orig_idx, 'OTHER|UNKNOWN')
            sec, mill = label.split('|')
            results.append({
                'coil_number': str(row.get('Coil Number', '')),
                'section':     sec,
                'mill':        mill,
                'confidence':  round(float(confs[i]), 4),
            })
        return pd.DataFrame(results)

    def feature_importance(self, top_n: int = 15) -> pd.DataFrame:
        """Return top N most important features."""
        if not self.is_trained or self.model is None:
            return pd.DataFrame()
        imp = self.model.feature_importances_
        return pd.DataFrame({
            'feature':    self.feature_names,
            'importance': imp,
        }).sort_values('importance', ascending=False).head(top_n)

    # ── Persistence ───────────────────────────────────────────────

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, 'wb') as f:
            pickle.dump({
                'model':         self.model,
                'is_trained':    self.is_trained,
                'n_samples':     self.n_samples,
                'feature_names': self.feature_names,
                'training_log':  self.training_log,
                'X':             self._X,
                'y':             self._y,
                'sources':       self._sources,
                'class_map':     getattr(self, '_class_map', {}),
                'class_unmap':   getattr(self, '_class_unmap', {}),
            }, f)

    def load(self, path: str) -> bool:
        if not Path(path).exists():
            return False
        with open(path, 'rb') as f:
            data = pickle.load(f)
        self.model         = data['model']
        self.is_trained    = data['is_trained']
        self.n_samples     = data['n_samples']
        self.feature_names = data['feature_names']
        self.training_log  = data.get('training_log', [])
        self._X            = data.get('X', [])
        self._y            = data.get('y', [])
        self._sources      = data.get('sources', [])
        self._class_map    = data.get('class_map', {})
        self._class_unmap  = data.get('class_unmap', {})
        return True

    @property
    def ready(self) -> bool:
        return self.is_trained and self.n_samples >= MIN_TRAINING_SAMPLES
