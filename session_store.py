"""
Per-user in-memory session — tracks last mentioned movie so follow-up
questions like "phim đó mấy giờ?" or "review phim đó đi" work correctly.

Keys stored per user:
  last_movie_id    (int)  — DB Movies.Id of last resolved movie
  last_movie_title (str)  — display name for Gemini context hint
"""
import time
import threading

_SESSION_TTL = 1800  # 30 minutes of inactivity clears session
_store: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()


def get(user_id: int) -> dict:
    if not user_id:
        return {}
    with _lock:
        ts, data = _store.get(str(user_id), (0.0, {}))
        if time.monotonic() - ts > _SESSION_TTL:
            _store.pop(str(user_id), None)
            return {}
        return data.copy()


def update(user_id: int, data: dict) -> None:
    """Merge data into existing session, resetting TTL."""
    if not user_id:
        return
    key = str(user_id)
    with _lock:
        _, existing = _store.get(key, (0.0, {}))
        _store[key] = (time.monotonic(), {**existing, **data})
