"""
db.py — Supabase persistence layer for learning_db
===================================================
Stores the entire learning_db as a single JSONB document in Supabase.

Table schema (create once in Supabase dashboard):
    CREATE TABLE learning_db (
        key        TEXT PRIMARY KEY,
        data       JSONB,
        updated_at TIMESTAMPTZ DEFAULT now()
    );

Environment variables required (set in Streamlit Cloud secrets):
    SUPABASE_URL  = https://xyzxyz.supabase.co
    SUPABASE_KEY  = your-anon-public-key

Fallback: if env vars are absent, falls back to local /tmp/learning_db.json
so the app works even before Supabase is configured.
"""

import json
import os
from datetime import datetime

# The single row key used to store the DB document
_ROW_KEY = "mill_planner_v1"

# ── optional import — graceful degradation if supabase not installed ────────
try:
    from supabase import create_client, Client
    _SUPABASE_AVAILABLE = True
except ImportError:
    _SUPABASE_AVAILABLE = False


def _get_client():
    """Return a Supabase client, or None if not configured."""
    if not _SUPABASE_AVAILABLE:
        return None
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_KEY", "").strip()
    if not url or not key:
        return None
    try:
        return create_client(url, key)
    except Exception:
        return None


def _local_path():
    return os.environ.get("LOCAL_DB_PATH", "/tmp/learning_db.json")


# ── Empty DB template (matches learner.py EMPTY_DB) ────────────────────────
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


# ────────────────────────────────────────────────────────────────────────────
# Public API — used everywhere instead of load_db / save_db from learner.py
# ────────────────────────────────────────────────────────────────────────────

def load_db() -> dict:
    """
    Load the learning DB.
    Tries Supabase first; falls back to local file.
    Returns an initialised empty DB if neither exists.
    """
    client = _get_client()

    if client:
        try:
            resp = client.table("learning_db") \
                         .select("data") \
                         .eq("key", _ROW_KEY) \
                         .execute()
            if resp.data:
                db = resp.data[0]["data"]
                # Forward-compatibility: add any missing top-level keys
                for k, v in EMPTY_DB.items():
                    if k not in db:
                        db[k] = v
                return db
        except Exception as e:
            print(f"[Supabase] load failed ({e}), falling back to local file")

    # ── Local fallback ──────────────────────────────────────────────────
    local = _local_path()
    if os.path.exists(local):
        try:
            with open(local) as f:
                db = json.load(f)
            for k, v in EMPTY_DB.items():
                if k not in db:
                    db[k] = v
            return db
        except Exception:
            pass

    return dict(EMPTY_DB)


def save_db(db: dict) -> bool:
    """
    Persist the learning DB.
    Writes to Supabase (upsert) AND local file (backup).
    Returns True on success.
    """
    db["last_updated"] = datetime.utcnow().isoformat()

    success = False
    client = _get_client()

    if client:
        try:
            client.table("learning_db").upsert({
                "key":        _ROW_KEY,
                "data":       db,
                "updated_at": db["last_updated"],
            }).execute()
            success = True
        except Exception as e:
            print(f"[Supabase] save failed ({e}), saving locally only")

    # Always write local backup regardless
    try:
        local = _local_path()
        tmp   = local + ".tmp"
        with open(tmp, "w") as f:
            json.dump(db, f, indent=2, default=str)
        os.replace(tmp, local)
        if not success:
            success = True   # local save succeeded
    except Exception as e:
        print(f"[Local] save also failed: {e}")

    return success


def is_supabase_connected() -> bool:
    """Return True if Supabase env vars are set and reachable."""
    client = _get_client()
    if not client:
        return False
    try:
        client.table("learning_db").select("key").limit(1).execute()
        return True
    except Exception:
        return False


def get_storage_mode() -> str:
    """Human-readable string describing where the DB is being stored."""
    if is_supabase_connected():
        return "☁️  Supabase (persistent)"
    if os.path.exists(_local_path()):
        return "💾  Local file (resets on server restart)"
    return "🆕  Not initialised yet"
