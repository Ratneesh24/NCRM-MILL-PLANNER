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

_ROW_KEY = "mill_planner_v1"

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
                          .eq("key", _ROW_KEY)
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
                "key":        _ROW_KEY,
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
