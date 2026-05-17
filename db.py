"""
Database queries — port of ChatController.cs BuildMovieContext / BuildFoodContext / BuildKnowledgeContext
Uses pymssql (no system ODBC driver required, works on Render Linux).
"""
import os
import re
import pymssql
from datetime import datetime, timedelta
from contextlib import contextmanager

_DAY_NAMES = {
    0: "Thứ Hai",
    1: "Thứ Ba",
    2: "Thứ Tư",
    3: "Thứ Năm",
    4: "Thứ Sáu",
    5: "Thứ Bảy",
    6: "Chủ Nhật",
}


def day_vn(weekday: int) -> str:
    return _DAY_NAMES.get(weekday, "")


# ─────────────────────────────────────────────────────────────────────────────
#  Connection helpers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_conn() -> dict:
    """Parse pyodbc-style connection string into pymssql keyword args."""
    conn_str = os.getenv("DB_CONNECTION_STRING", "")
    if not conn_str:
        raise RuntimeError("DB_CONNECTION_STRING not set")
    params = dict(re.findall(r'(\w+)\s*=\s*([^;]+)', conn_str, re.IGNORECASE))
    return {
        "server":   params.get("SERVER",   params.get("server",   "")),
        "database": params.get("DATABASE", params.get("database", "")),
        "user":     params.get("UID",      params.get("uid",      "")),
        "password": params.get("PWD",      params.get("pwd",      "")),
    }


class _Row:
    """Wraps a tuple row to allow both attribute (dot) and integer index access."""
    __slots__ = ("_d", "_v")

    def __init__(self, cols, values):
        object.__setattr__(self, "_d", dict(zip(cols, values)))
        object.__setattr__(self, "_v", tuple(values))

    def __getattr__(self, name):
        try:
            return object.__getattribute__(self, "_d")[name]
        except KeyError:
            raise AttributeError(name)

    def __getitem__(self, key):
        if isinstance(key, int):
            return object.__getattribute__(self, "_v")[key]
        return object.__getattribute__(self, "_d")[key]


class _Cursor:
    """
    Wraps a pymssql cursor to be API-compatible with pyodbc:
    - Converts ? placeholders → %s (pymssql style)
    - execute() accepts positional args: c.execute(sql, p1, p2, ...)
    - fetchone() / fetchall() return objects with dot-notation column access
    """

    def __init__(self, cursor):
        self._c = cursor

    def execute(self, sql, *args):
        sql = sql.replace("?", "%s")
        if not args:
            self._c.execute(sql)
        elif len(args) == 1 and isinstance(args[0], (tuple, list)):
            self._c.execute(sql, tuple(args[0]))
        else:
            self._c.execute(sql, args)
        return self

    def _cols(self):
        return [d[0] for d in (self._c.description or [])]

    def fetchone(self):
        row = self._c.fetchone()
        if row is None:
            return None
        cols = self._cols()
        return _Row(cols, row)

    def fetchall(self):
        cols = self._cols()
        return [_Row(cols, row) for row in self._c.fetchall()]

    def __getattr__(self, name):
        return getattr(self._c, name)


class _Conn:
    """Thin wrapper so get_conn() yields an object with .cursor() and .commit()."""

    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        return _Cursor(self._conn.cursor())

    def commit(self):
        self._conn.commit()


@contextmanager
def get_conn():
    p = _parse_conn()
    conn = pymssql.connect(
        server=p["server"],
        database=p["database"],
        user=p["user"],
        password=p["password"],
        login_timeout=10,
        timeout=30,
    )
    try:
        yield _Conn(conn)
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
#  Movie context — port of BuildMovieContext(DateTime nowVn)
# ─────────────────────────────────────────────────────────────────────────────
def get_movie_context() -> str:
    now      = datetime.now()
    today    = now.date()
    tomorrow = today + timedelta(days=1)
    cutoff   = today + timedelta(days=8)

    sql_showtimes = """
        SELECT
            s.Id        AS ShowtimeId,
            s.StartTime,
            s.BasePrice,
            s.MovieId,
            r.Name      AS RoomName,
            m.Title,
            m.DurationMinutes,
            m.Rating,
            m.Genre,
            m.Language,
            m.Director,
            m.MainActors,
            m.Description
        FROM Showtimes s
        INNER JOIN Movies m ON m.Id = s.MovieId
        LEFT  JOIN Rooms  r ON r.Id = s.RoomId
        WHERE s.StartTime >= ? AND s.StartTime < ?
        ORDER BY s.StartTime
    """

    sql_coming_soon = """
        SELECT TOP 5
            m.Title, m.Genre, m.ReleaseDate, m.Description
        FROM Movies m
        WHERE m.ReleaseDate > GETDATE()
          AND NOT EXISTS (
              SELECT 1 FROM Showtimes s
              WHERE s.MovieId = m.Id AND s.StartTime >= GETDATE()
          )
        ORDER BY m.ReleaseDate
    """

    lines = []

    try:
        with get_conn() as conn:
            cursor = conn.cursor()

            # ── Fetch showtimes ─────────────────────────────────────────────
            cursor.execute(sql_showtimes, now, datetime.combine(cutoff, datetime.min.time()))
            rows = cursor.fetchall()

            # Group by date → movie
            from collections import defaultdict
            by_date = defaultdict(lambda: defaultdict(list))
            movie_info = {}

            for row in rows:
                dt: datetime = row.StartTime
                movie_id = row.MovieId
                by_date[dt.date()][movie_id].append(row)
                if movie_id not in movie_info:
                    movie_info[movie_id] = row

            today_rows    = by_date.get(today, {})
            tomorrow_rows = by_date.get(tomorrow, {})
            later_dates   = sorted(d for d in by_date if d > tomorrow)

            # ── Hôm nay ────────────────────────────────────────────────────
            lines.append(f"## SUẤT CHIẾU HÔM NAY ({day_vn(today.weekday())}, {today.strftime('%d/%m/%Y')})")
            if not today_rows:
                lines.append("*(Không còn suất nào hôm nay)*")
            else:
                for movie_id, showtimes in today_rows.items():
                    m = movie_info[movie_id]
                    lines.append(f"### {m.Title} ({m.DurationMinutes} phút | {m.Rating or '—'})")
                    for s in sorted(showtimes, key=lambda x: x.StartTime):
                        total_mins = (s.StartTime - now).total_seconds() / 60
                        if total_mins < 0:
                            countdown = "(đang chiếu)"
                        else:
                            hh = int(total_mins) // 60
                            mm = int(total_mins) % 60
                            countdown = f"(còn {hh} giờ {mm} phút nữa)" if hh > 0 else f"(còn {mm} phút nữa)"
                        price = f"{int(s.BasePrice):,}đ".replace(",", ".")
                        lines.append(f"  + {s.StartTime.strftime('%H:%M')} | Phòng {s.RoomName or '?'} | {price} | {countdown}")

            # ── Ngày mai ───────────────────────────────────────────────────
            lines.append("")
            lines.append(f"## SUẤT CHIẾU NGÀY MAI ({day_vn(tomorrow.weekday())}, {tomorrow.strftime('%d/%m/%Y')})")
            if not tomorrow_rows:
                lines.append("*(Không có suất chiếu)*")
            else:
                for movie_id, showtimes in tomorrow_rows.items():
                    m = movie_info[movie_id]
                    lines.append(f"### {m.Title} ({m.DurationMinutes} phút | {m.Rating or '—'})")
                    for s in sorted(showtimes, key=lambda x: x.StartTime):
                        price = f"{int(s.BasePrice):,}đ".replace(",", ".")
                        lines.append(f"  + {s.StartTime.strftime('%H:%M')} | Phòng {s.RoomName or '?'} | {price}")

            # ── 7 ngày tới ─────────────────────────────────────────────────
            if later_dates:
                lines.append("")
                lines.append("## LỊCH CHIẾU 7 NGÀY TỚI")
                for date in later_dates:
                    lines.append(f"**{day_vn(date.weekday())} {date.strftime('%d/%m/%Y')}:**")
                    for movie_id, showtimes in by_date[date].items():
                        m = movie_info[movie_id]
                        times = ", ".join(s.StartTime.strftime("%H:%M") for s in sorted(showtimes, key=lambda x: x.StartTime))
                        price = f"{int(showtimes[0].BasePrice):,}đ".replace(",", ".")
                        lines.append(f"  - {m.Title}: {times} ({price}/vé)")

            # ── Chi tiết phim đang chiếu ───────────────────────────────────
            if movie_info:
                lines.append("")
                lines.append("## THÔNG TIN CHI TIẾT PHIM ĐANG CHIẾU")
                for movie_id, m in sorted(movie_info.items(), key=lambda x: x[1].Title):
                    lines.append(f"### {m.Title}")
                    lines.append(
                        f"- Thể loại: {m.Genre or '—'} | Thời lượng: {m.DurationMinutes} phút"
                        f" | Phân loại: {m.Rating or '—'} | Ngôn ngữ: {m.Language or '—'}"
                    )
                    if m.Director:
                        lines.append(f"- Đạo diễn: {m.Director}")
                    if m.MainActors:
                        lines.append(f"- Diễn viên chính: {m.MainActors}")
                    if m.Description:
                        lines.append(f"- Nội dung: {m.Description}")

            # ── Phim sắp chiếu ─────────────────────────────────────────────
            cursor.execute(sql_coming_soon)
            coming = cursor.fetchall()
            if coming:
                lines.append("")
                lines.append("## PHIM SẮP CHIẾU (CHƯA CÓ LỊCH)")
                for m in coming:
                    release = m.ReleaseDate.strftime("%d/%m/%Y") if m.ReleaseDate else "—"
                    lines.append(f"- **{m.Title}** | {m.Genre or '—'} | Dự kiến: {release}")
                    if m.Description:
                        lines.append(f"  {m.Description}")

    except Exception as e:
        lines.append(f"*(Không thể tải dữ liệu suất chiếu: {e})*")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
#  Food context
# ─────────────────────────────────────────────────────────────────────────────
def get_food_context() -> str:
    sql = """
        SELECT Name, Category, Price, Description
        FROM FoodAndDrinks
        ORDER BY Category, Name
    """
    lines = ["## MENU ĐỒ ĂN & THỨC UỐNG"]
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(sql)
            rows = cursor.fetchall()
            if not rows:
                lines.append("*(Chưa có menu)*")
            else:
                from collections import defaultdict
                by_cat = defaultdict(list)
                for r in rows:
                    by_cat[r.Category or "Khác"].append(r)
                for cat, items in by_cat.items():
                    lines.append(f"### {cat}")
                    for item in items:
                        price = f"{int(item.Price):,}đ".replace(",", ".")
                        desc = f" — {item.Description}" if item.Description else ""
                        lines.append(f"- {item.Name}: {price}{desc}")
    except Exception as e:
        lines.append(f"*(Không thể tải menu: {e})*")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
#  Knowledge context (ChatbotKnowledge table)
# ─────────────────────────────────────────────────────────────────────────────
def get_knowledge_context() -> str:
    sql = """
        SELECT Title, Content
        FROM ChatbotKnowledge
        WHERE IsActive = 1
        ORDER BY SortOrder, Id
    """
    lines = []
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(sql)
            rows = cursor.fetchall()
            if rows:
                lines.append("## KIẾN THỨC BỔ SUNG (Admin cập nhật)")
                for r in rows:
                    lines.append(f"### {r.Title}")
                    lines.append(r.Content)
    except Exception:
        pass  # table may not exist yet
    return "\n".join(lines)
