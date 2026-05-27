"""
db.py — Supabase persistence layer for learning_db
===================================================
Stores the entire learning_db as a single JSONB document in Supabase.

Table schema (create once in Supabase SQL Editor):
    CREATE TABLE learning_db (
        key        TEXT PRIMARY KEY,
        data       JSONB,
        updated_at TIMESTAMPTZ DEFAULT now()
    );

Streamlit secrets required (Settings → Secrets in Streamlit Cloud):
    SUPABASE_URL = "https://ypgydkhytrjvbozkebed.supabase.co"
    SUPABASE_KEY = "sb_secret_..."   ← use the SECRET key, not publishable

Fallback: if env vars absent or connection fails, uses /tmp/learning_db.json
"""

import json
import os
from datetime import datetime
from typing import Tuple

# ── Test mode — set to True to use test_ prefixed keys ─────────────────────
# Controlled via Streamlit session state, never touches production keys
_TEST_MODE = False   # module-level flag; toggled by set_test_mode()

_ROW_KEY_PROD = "mill_planner_v1"
_ROW_KEY_TEST = "TEST_mill_planner_v1"

def set_test_mode(enabled: bool):
    """Call this from app.py when the user toggles test mode."""
    global _TEST_MODE
    _TEST_MODE = enabled

def is_test_mode() -> bool:
    return _TEST_MODE

def _row_key() -> str:
    return _ROW_KEY_TEST if _TEST_MODE else _ROW_KEY_PROD

# Keep _ROW_KEY as alias for anything that still references it directly
_ROW_KEY = _ROW_KEY_PROD

try:
    from supabase import create_client
    _SUPABASE_AVAILABLE = True
except ImportError:
    _SUPABASE_AVAILABLE = False


def _get_env():
    """
    Read SUPABASE_URL and SUPABASE_KEY from:
      1. Streamlit secrets (st.secrets) — primary when running on Streamlit Cloud
      2. os.environ — fallback / local dev
    Returns (url, key) strings or ('', '') if not found.
    """
    url = key = ""

    # Try Streamlit secrets first
    try:
        import streamlit as st
        url = st.secrets.get("SUPABASE_URL", "")
        key = st.secrets.get("SUPABASE_KEY", "")
    except Exception:
        pass

    # Fall back to environment variables
    if not url:
        url = os.environ.get("SUPABASE_URL", "").strip()
    if not key:
        key = os.environ.get("SUPABASE_KEY", "").strip()

    return url.strip(), key.strip()


_client_cache = None
_last_error   = ""

def _get_client():
    """Return a cached Supabase client, or None with error stored in _last_error."""
    global _client_cache, _last_error

    if _client_cache is not None:
        return _client_cache

    if not _SUPABASE_AVAILABLE:
        _last_error = "supabase package not installed"
        return None

    url, key = _get_env()

    if not url:
        _last_error = "SUPABASE_URL not set in Streamlit secrets"
        return None
    if not key:
        _last_error = "SUPABASE_KEY not set in Streamlit secrets"
        return None
    if key.startswith("sb_publishable_"):
        _last_error = (
            "Wrong key type — you used the Publishable key. "
            "Use the SECRET key (sb_secret_...) from Settings → API Keys → Secret keys."
        )
        return None

    try:
        _client_cache = create_client(url, key)
        _last_error = ""
        return _client_cache
    except Exception as e:
        _last_error = f"create_client failed: {e}"
        return None


def _local_path():
    return os.environ.get("LOCAL_DB_PATH", "/tmp/learning_db.json")


# ── Empty DB template ───────────────────────────────────────────────────────
EMPTY_DB = {
    "schema_version":      "1.0",
    "last_updated":        "",
    "total_sessions":      0,
    "cumulative_accuracy": 0.0,
    "grade_routing":       {},
    "coil_overrides":      {},
    "sort_rules":          {},
    "split_rules":         {},
    "inclusion_rules": {
        "exclude_patterns": [],
        "include_patterns": [],
    },
    "header_vocab":    {},
    "customer_abbrev": {},
    "rt_corrections":  {},
    "session_log":     [],
    "conflict_log":    [],
}


def _merge_empty(db):
    for k, v in EMPTY_DB.items():
        if k not in db:
            db[k] = v
    return db


# ── Public API ──────────────────────────────────────────────────────────────

def load_db() -> dict:
    """Load DB from Supabase, falling back to local file."""
    client = _get_client()
    if client:
        try:
            resp = (client.table("learning_db")
                          .select("data")
                          .eq("key", _row_key())
                          .execute())
            if resp.data:
                return _merge_empty(resp.data[0]["data"])
        except Exception as e:
            global _last_error
            _last_error = f"Supabase read error: {e}"

    # Local fallback
    local = _local_path()
    if os.path.exists(local):
        try:
            with open(local) as f:
                return _merge_empty(json.load(f))
        except Exception:
            pass

    return dict(EMPTY_DB)


def save_db(db: dict) -> bool:
    """Save DB to Supabase (upsert) and local file as backup."""
    db["last_updated"] = datetime.utcnow().isoformat()
    success = False

    client = _get_client()
    if client:
        try:
            client.table("learning_db").upsert({
                "key":        _row_key(),
                "data":       db,
                "updated_at": db["last_updated"],
            }).execute()
            success = True
        except Exception as e:
            global _last_error
            _last_error = f"Supabase write error: {e}"

    # Always write local backup
    try:
        local = _local_path()
        tmp   = local + ".tmp"
        with open(tmp, "w") as f:
            json.dump(db, f, indent=2, default=str)
        os.replace(tmp, local)
        if not success:
            success = True
    except Exception:
        pass

    return success


def is_supabase_connected() -> bool:
    """Return True only if client exists AND a real query succeeds."""
    client = _get_client()
    if not client:
        return False
    try:
        client.table("learning_db").select("key").limit(1).execute()
        return True
    except Exception as e:
        global _last_error
        _last_error = f"Connection test failed: {e}"
        return False


def get_storage_mode() -> str:
    """Human-readable storage status, including error reason if disconnected."""
    global _last_error
    _last_error = ""          # reset before fresh check
    if is_supabase_connected():
        return "☁️  Supabase (persistent)"
    reason = _last_error or "env vars not set"
    return f"💾  Local only — {reason}"


# ── Roll State Persistence ──────────────────────────────────────────────────
# Stored as a separate key in the same learning_db table
_ROLL_STATE_KEY   = "roll_state_v1"
_ROLL_STATE_KEY_TEST = "TEST_roll_state_v1"

def _roll_state_key() -> str:
    return _ROLL_STATE_KEY_TEST if _TEST_MODE else _ROLL_STATE_KEY
_ROLL_STATE_LOCAL = "/tmp/roll_state.json"

EMPTY_ROLL_STATE = {
    "CRM04": {
        "roll_type":      "LIGHT_MATT",
        "mt_used":        0.0,
        "mt_life":        300.0,
        "roll_number":    "",
        "installed_date": "",
        "last_updated":   "",
        "last_plan_date": "",
        "history":        []
    },
    "CRM06": {
        "roll_type":      "LIGHT_MATT",
        "mt_used":        0.0,
        "mt_life":        280.0,
        "roll_number":    "",
        "installed_date": "",
        "last_updated":   "",
        "last_plan_date": "",
        "history":        []
    }
}


def load_roll_state() -> dict:
    """Load persisted roll state. Returns defaults if nothing saved yet."""
    client = _get_client()
    if client:
        try:
            resp = (client.table("learning_db")
                          .select("data")
                          .eq("key", _roll_state_key())
                          .execute())
            if resp.data:
                saved = resp.data[0]["data"]
                for mill in ("CRM04", "CRM06"):
                    if mill not in saved:
                        saved[mill] = dict(EMPTY_ROLL_STATE[mill])
                    for k, v in EMPTY_ROLL_STATE[mill].items():
                        if k not in saved[mill]:
                            saved[mill][k] = v
                return saved
        except Exception as e:
            global _last_error
            _last_error = f"Roll state read error: {e}"

    if os.path.exists(_ROLL_STATE_LOCAL):
        try:
            with open(_ROLL_STATE_LOCAL) as f:
                return json.load(f)
        except Exception:
            pass

    return {
        "CRM04": dict(EMPTY_ROLL_STATE["CRM04"]),
        "CRM06": dict(EMPTY_ROLL_STATE["CRM06"]),
    }


def save_roll_state(state: dict, plan_date: str = "",
                    crm04_mt_rolled: float = 0.0,
                    crm06_mt_rolled: float = 0.0) -> bool:
    """
    Persist roll state after a shift/analysis.
    Adds today rolled MT to mt_used, appends history entry.
    """
    now = datetime.utcnow().isoformat()

    for mill, mt_rolled in [("CRM04", crm04_mt_rolled),
                              ("CRM06", crm06_mt_rolled)]:
        if mill not in state:
            state[mill] = dict(EMPTY_ROLL_STATE[mill])
        ms           = state[mill]
        old_mt       = ms.get("mt_used", 0.0)
        new_mt       = round(old_mt + mt_rolled, 1)
        ms["mt_used"]        = new_mt
        ms["last_updated"]   = now
        ms["last_plan_date"] = plan_date or now[:10]
        if mt_rolled > 0:
            if "history" not in ms:
                ms["history"] = []
            ms["history"].append({
                "date":        plan_date or now[:10],
                "mt_rolled":   round(mt_rolled, 1),
                "roll_type":   ms.get("roll_type", ""),
                "mt_used_end": new_mt,
            })
            ms["history"] = ms["history"][-90:]

    state["last_updated"] = now
    success = False

    client = _get_client()
    if client:
        try:
            client.table("learning_db").upsert({
                "key":        _roll_state_key(),
                "data":       state,
                "updated_at": now,
            }).execute()
            success = True
        except Exception as e:
            global _last_error
            _last_error = f"Roll state save error: {e}"

    try:
        tmp = _ROLL_STATE_LOCAL + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2, default=str)
        os.replace(tmp, _ROLL_STATE_LOCAL)
        if not success:
            success = True
    except Exception:
        pass

    return success


def record_roll_change(mill: str, new_roll_type: str,
                       new_mt_life: float, roll_number: str = "",
                       installed_date: str = "") -> bool:
    """Reset mt_used to 0 when a roll is physically changed on the mill."""
    state = load_roll_state()
    now   = datetime.utcnow().isoformat()
    if mill not in state:
        state[mill] = dict(EMPTY_ROLL_STATE[mill])
    ms = state[mill]
    if "history" not in ms:
        ms["history"] = []
    ms["history"].append({
        "date":              now[:10],
        "event":             "ROLL_CHANGE",
        "old_roll":          ms.get("roll_type", ""),
        "new_roll":          new_roll_type,
        "mt_used_at_change": ms.get("mt_used", 0.0),
    })
    ms["roll_type"]      = new_roll_type
    ms["mt_used"]        = 0.0
    ms["mt_life"]        = new_mt_life
    ms["roll_number"]    = roll_number
    ms["installed_date"] = installed_date or now[:10]
    ms["last_updated"]   = now
    return save_roll_state(state)


# ── Test Mode Utilities ─────────────────────────────────────────────────────

def clear_test_data() -> Tuple[int, str]:
    """
    Delete all records whose key starts with 'TEST_' from Supabase and local.
    Returns (count_deleted, message).
    """
    deleted = 0
    msg     = ""

    client = _get_client()
    if client:
        try:
            # Fetch all TEST_ keys first
            resp = (client.table("learning_db")
                          .select("key")
                          .like("key", "TEST_%")
                          .execute())
            keys = [r["key"] for r in resp.data]
            if keys:
                client.table("learning_db") \
                      .delete() \
                      .like("key", "TEST_%") \
                      .execute()
                deleted = len(keys)
                msg = f"Deleted {deleted} test record(s) from Supabase: {keys}"
            else:
                msg = "No test records found in Supabase."
        except Exception as e:
            msg = f"Supabase delete error: {e}"

    # Clear local test files
    local_files = [
        "/tmp/learning_db.json",
        "/tmp/roll_state.json",
    ]
    for lf in local_files:
        test_lf = lf.replace(".json", "_TEST.json")
        if os.path.exists(test_lf):
            os.remove(test_lf)

    # Clear test shift files
    shift_dir = "/tmp/shifts"
    if os.path.exists(shift_dir):
        import glob
        test_shifts = glob.glob(os.path.join(shift_dir, "TEST_*.json"))
        for ts in test_shifts:
            os.remove(ts)
        deleted += len(test_shifts)

    return deleted, msg


def get_db_stats() -> dict:
    """Return count of production vs test records in Supabase."""
    client = _get_client()
    if not client:
        return {"error": "Not connected", "prod": 0, "test": 0, "total": 0}
    try:
        all_resp = client.table("learning_db").select("key").execute()
        all_keys = [r["key"] for r in all_resp.data]
        test_keys = [k for k in all_keys if k.startswith("TEST_")]
        prod_keys = [k for k in all_keys if not k.startswith("TEST_")]
        return {
            "total": len(all_keys),
            "prod":  len(prod_keys),
            "test":  len(test_keys),
            "prod_keys": prod_keys,
            "test_keys": test_keys,
        }
    except Exception as e:
        return {"error": str(e), "prod": 0, "test": 0, "total": 0}


# ── ML Model Persistence ────────────────────────────────────────────────────
# Model stored compressed+base64 in Supabase under a dedicated key

_MODEL_KEY       = "ml_model_v2"
_MODEL_KEY_TEST  = "TEST_ml_model_v2"
_LOCAL_MODEL_PATH = "/tmp/models/section_clf.pkl"

import zlib, base64 as _b64

def _model_key() -> str:
    return _MODEL_KEY_TEST if _TEST_MODE else _MODEL_KEY


def save_model_to_supabase(model_path: str) -> bool:
    """
    Compress the .pkl file and store it in Supabase.
    Falls back silently if Supabase is not connected.
    """
    if not os.path.exists(model_path):
        return False

    try:
        with open(model_path, 'rb') as f:
            raw = f.read()
        compressed = zlib.compress(raw, level=9)
        encoded    = _b64.b64encode(compressed).decode('ascii')

        payload = {
            'model_b64':    encoded,
            'size_original': len(raw),
            'size_compressed': len(compressed),
            'saved_at':     datetime.utcnow().isoformat(),
        }

        client = _get_client()
        if client:
            client.table("learning_db").upsert({
                "key":        _model_key(),
                "data":       payload,
                "updated_at": payload['saved_at'],
            }).execute()
            return True
    except Exception as e:
        print(f"[Supabase] model save error: {e}")

    return False


def load_model_from_supabase(model_path: str) -> bool:
    """
    Download model from Supabase, decompress, save to model_path.
    Returns True if successful.
    """
    client = _get_client()
    if not client:
        return False

    try:
        resp = (client.table("learning_db")
                      .select("data")
                      .eq("key", _model_key())
                      .execute())
        if not resp.data:
            return False

        payload    = resp.data[0]["data"]
        encoded    = payload.get("model_b64", "")
        if not encoded:
            return False

        compressed = _b64.b64decode(encoded.encode('ascii'))
        raw        = zlib.decompress(compressed)

        os.makedirs(os.path.dirname(os.path.abspath(model_path)), exist_ok=True)
        tmp = model_path + ".tmp"
        with open(tmp, 'wb') as f:
            f.write(raw)
        os.replace(tmp, model_path)
        return True

    except Exception as e:
        print(f"[Supabase] model load error: {e}")
        return False


def get_model_info() -> dict:
    """Return metadata about the stored model (size, date) without downloading it."""
    client = _get_client()
    if not client:
        return {}
    try:
        resp = (client.table("learning_db")
                      .select("data,updated_at")
                      .eq("key", _model_key())
                      .execute())
        if resp.data:
            d = resp.data[0]["data"]
            return {
                "saved_at":       d.get("saved_at", ""),
                "size_original":  d.get("size_original", 0),
                "size_compressed":d.get("size_compressed", 0),
                "exists":         True,
            }
    except Exception:
        pass
    return {"exists": False}
