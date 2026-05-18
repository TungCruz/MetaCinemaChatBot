"""
Chatbot session store — two-tier (L1 memory + SQL Server DB).

Architecture
------------
L1 cache   : in-process dict keyed by session key, evicted after _SESSION_TTL
             seconds of inactivity (same 30-minute window used before).
DB layer   : SQL Server table ChatbotSessions (auto-created on first write).
             Writes are dispatched to a daemon thread so they never block the
             response path.  Reads fall through to DB on an L1 miss and
             re-populate the cache on a hit.

Session keys
------------
  logged-in user   →  "u:{user_id}"           (user_id is an int)
  anonymous user   →  "t:{session_token[:100]}" (session_token is a str)
  both absent      →  no-op / return {}

Session data format
-------------------
A plain dict whose values are strings or ints, e.g.
  {"last_movie_id": 5, "last_movie_title": "Avengers"}

Serialised to/from JSON in the DB column (NVARCHAR MAX).

Backward-compatible public API
-------------------------------
  get(user_id=None, session_token=None)  -> dict
  update(user_id=None, session_token=None, data: dict = None) -> None

Both old call-sites (get(user_id=42), update(user_id=42, data={...})) still
work because the new parameters are keyword-only with the same defaults.

DB resilience
-------------
Every DB operation is wrapped in try/except.  Any failure is logged at DEBUG
level and the call degrades gracefully: reads return {} (or the L1 value if
available); writes are silently dropped from the DB but still land in L1.

Import discipline
-----------------
`from db import get_conn` is done inside each function that needs it — never
at module level — to prevent circular-import problems at application startup.
"""

import json
import logging
import threading
import time
from datetime import datetime

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_SESSION_TTL = 1800  # seconds — 30 minutes of inactivity

# ---------------------------------------------------------------------------
# L1 in-memory cache
# Entries: key -> (monotonic_timestamp: float, data: dict)
# ---------------------------------------------------------------------------

_store: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()

# ---------------------------------------------------------------------------
# One-time table creation (guarded by a flag so it only runs once per process)
# ---------------------------------------------------------------------------

_table_ensured = False
_table_lock = threading.Lock()

_CREATE_TABLE_SQL = """
IF NOT EXISTS (
    SELECT 1 FROM INFORMATION_SCHEMA.TABLES
    WHERE TABLE_NAME = 'ChatbotSessions'
)
CREATE TABLE ChatbotSessions (
    SessionKey  NVARCHAR(120) NOT NULL PRIMARY KEY,
    SessionData NVARCHAR(MAX) NOT NULL,
    UpdatedAt   DATETIME2     NOT NULL
)
"""


def _ensure_table() -> None:
    """Create ChatbotSessions if it does not already exist (runs once per process)."""
    global _table_ensured
    if _table_ensured:
        return
    with _table_lock:
        if _table_ensured:
            return
        try:
            from db import get_conn  # local import — avoids circular import
            with get_conn() as conn:
                c = conn.cursor()
                c.execute(_CREATE_TABLE_SQL)
                conn.commit()
            _table_ensured = True
        except Exception as exc:
            logger.debug("session_store: could not ensure ChatbotSessions table: %s", exc)


# ---------------------------------------------------------------------------
# Key helpers
# ---------------------------------------------------------------------------

def _make_key(user_id=None, session_token=None) -> str | None:
    """Return the canonical session key, or None when both args are absent."""
    if user_id is not None:
        return f"u:{user_id}"
    if session_token is not None:
        return f"t:{str(session_token)[:100]}"
    return None


# ---------------------------------------------------------------------------
# DB helpers (called from daemon threads — must not raise)
# ---------------------------------------------------------------------------

def _db_load(key: str) -> dict | None:
    """
    Fetch a session from the DB.
    Returns the parsed dict on a valid (non-expired) hit, or None on miss /
    expired / any DB error.
    """
    _ensure_table()
    try:
        from db import get_conn
        with get_conn() as conn:
            c = conn.cursor()
            c.execute(
                "SELECT SessionData, UpdatedAt FROM ChatbotSessions WHERE SessionKey = ?",
                key,
            )
            row = c.fetchone()
        if row is None:
            return None
        # Check TTL
        updated_at: datetime = row.UpdatedAt
        age = (datetime.utcnow() - updated_at).total_seconds()
        if age > _SESSION_TTL:
            return None
        return json.loads(row.SessionData)
    except Exception as exc:
        logger.debug("session_store: DB read failed for key=%r: %s", key, exc)
        return None


def _db_save(key: str, data: dict) -> None:
    """
    Upsert a session into the DB — race-safe UPDATE-first pattern.
    Try UPDATE; if no row exists (rowcount == 0), INSERT and ignore any
    primary-key violation from a concurrent writer.
    Swallows all exceptions — called from a daemon thread.
    """
    _ensure_table()
    try:
        payload = json.dumps(data, ensure_ascii=False, default=str)
        now = datetime.utcnow()
        from db import get_conn
        with get_conn() as conn:
            c = conn.cursor()
            c.execute(
                "UPDATE ChatbotSessions SET SessionData = ?, UpdatedAt = ? WHERE SessionKey = ?",
                payload, now, key,
            )
            if c.rowcount == 0:
                # No existing row — insert; ignore PK violation from concurrent writer
                try:
                    c.execute(
                        "INSERT INTO ChatbotSessions (SessionKey, SessionData, UpdatedAt)"
                        " VALUES (?, ?, ?)",
                        key, payload, now,
                    )
                except Exception:
                    pass  # concurrent INSERT by another worker — their write wins, ours lost
            conn.commit()
    except Exception as exc:
        logger.debug("session_store: DB write failed for key=%r: %s", key, exc)


def _fire_db_save(key: str, data: dict) -> None:
    """Dispatch _db_save to a daemon thread so the caller is not blocked."""
    t = threading.Thread(target=_db_save, args=(key, data), daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get(user_id=None, session_token=None) -> dict:
    """
    Return the current session data for the given identity.

    Lookup order:
    1. L1 in-memory cache (within TTL) → return immediately.
    2. SQL Server DB → populate L1 on hit, return data.
    3. No data found → return {}.

    Parameters
    ----------
    user_id : int, optional
        ID of a logged-in user.
    session_token : str, optional
        Opaque token for an anonymous session (used when user_id is absent).

    Returns
    -------
    dict
        A copy of the session data (empty dict if no session exists).
    """
    key = _make_key(user_id=user_id, session_token=session_token)
    if key is None:
        return {}

    # --- L1 check ---
    with _lock:
        entry = _store.get(key)
        if entry is not None:
            ts, cached_data = entry
            if time.monotonic() - ts <= _SESSION_TTL:
                return cached_data.copy()
            # Expired — evict
            _store.pop(key, None)

    # --- DB fallback ---
    db_data = _db_load(key)
    if db_data is not None:
        # Re-populate L1
        with _lock:
            _store[key] = (time.monotonic(), db_data)
        return db_data.copy()

    return {}


def update(user_id=None, session_token=None, data: dict = None) -> None:
    """
    Merge *data* into the existing session and persist it.

    L1 is updated synchronously; the DB write is dispatched to a daemon
    thread so the caller returns immediately.

    Parameters
    ----------
    user_id : int, optional
        ID of a logged-in user.
    session_token : str, optional
        Opaque token for an anonymous session (used when user_id is absent).
    data : dict
        Key-value pairs to merge into the session.  Existing keys not present
        in *data* are preserved.
    """
    if data is None:
        data = {}

    key = _make_key(user_id=user_id, session_token=session_token)
    if key is None:
        return

    # --- Update L1 ---
    with _lock:
        entry = _store.get(key)
        if entry is not None:
            ts, existing = entry
            if time.monotonic() - ts <= _SESSION_TTL:
                merged = {**existing, **data}
            else:
                merged = data.copy()
        else:
            merged = data.copy()
        _store[key] = (time.monotonic(), merged)

    # --- Async DB write ---
    _fire_db_save(key, merged)


# ---------------------------------------------------------------------------
# Background L1 cache cleanup — prevents unbounded growth from anonymous tokens
# ---------------------------------------------------------------------------

def _start_cleanup_thread() -> None:
    """Evict expired L1 entries every 5 minutes (daemon — stops with process)."""
    def _loop():
        while True:
            time.sleep(300)
            cutoff = time.monotonic() - _SESSION_TTL
            with _lock:
                stale = [k for k, (ts, _) in _store.items() if ts < cutoff]
                for k in stale:
                    _store.pop(k, None)
            if stale:
                logger.debug("session_store: evicted %d expired L1 entries", len(stale))

    t = threading.Thread(target=_loop, daemon=True, name="session-cleanup")
    t.start()


_start_cleanup_thread()
