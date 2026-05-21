"""
Port of ChatController.cs BuildStaffReply and sub-methods.
Auth: C# passes role from Session["StaffRole"] → Python trusts it.

Role-based access:
  Nhân viên bán vé  → showtime, room, food, payment info, counter guide (NO revenue)
  Quản lý ca / Admin → all above + revenue report, staff stats, attendance
"""
import re
import json
import os
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from db import get_conn
from intent_router import normalize, expand_synonyms, _apply_synonyms, extract_requested_date
from admin_router import (
    _build_report_range, _fmt_money, _vn_to_utc,
    _counter_sales, _payment, _food,
)

# URL gốc của web C# (set trong .env khi deploy: WEB_BASE_URL=https://rapchieuphim.somee.com)
_WEB_BASE_URL = os.getenv("WEB_BASE_URL", "").rstrip("/")
_INTERNAL_SECRET = os.getenv("INTERNAL_SECRET") or os.getenv("CHATBOT_INTERNAL_SECRET", "")


# ─────────────────────────────────────────────────────────────────────────────
#  Staff-specific synonym expansion
#  Áp dụng sau expand_synonyms() chung — chỉ dùng trong context nhân viên.
# ─────────────────────────────────────────────────────────────────────────────
_STAFF_SYNONYM_GROUPS: list[tuple[list[str], str]] = [
    # ── Tình trạng ghế ───────────────────────────────────────────────────
    (["con cho ngoi", "ghe da ban", "ghe da dat",
      "kiem tra ghe", "suc chua phong", "co bao nhieu ghe",
      "phong con ghe", "ghe trong phong"], "ghe trong"),

    # ── Lịch chiếu hôm nay ───────────────────────────────────────────────
    (["lich phim", "phim hom nay", "suat hom nay",
      "buoi chieu hom nay", "xem lich", "lich chieu hom nay",
      "hom nay chieu gi", "chieu gi hom nay"], "lich hom nay"),

    # ── Chấm công / ca làm ───────────────────────────────────────────────
    (["nghi phep", "vang mat", "ngay cong",
      "gio vao lam", "gio ra ve", "ca toi nay", "ca hom nay",
      "ai truc ca", "truc ca", "gio lam hom nay"], "ca lam"),

    # ── Bán tại quầy ─────────────────────────────────────────────────────
    (["ve tai quay", "ban ve cho khach", "thanh toan tai quay",
      "dat ve tai quay", "pos", "tiep khach",
      "ban ve truc tiep", "thu tien khach"], "ban tai quay"),

    # ── Xác thực vé / QR ─────────────────────────────────────────────────
    (["quet ve", "quet qr", "check qr", "check ve", "kiem ve",
      "kiem tra ma ve", "khach dua qr", "khach dua ma",
      "ma booking", "ma ve khach"], "xac thuc ve"),

    # ── Phòng chiếu ──────────────────────────────────────────────────────
    (["phong chieu", "tinh trang phong",
      "phong bi loi", "phong nao dang chieu",
      "kiem tra phong", "phong trong"], "phong"),

    # ── Thanh toán / giao dịch lỗi ───────────────────────────────────────
    (["hoa don", "bill", "lich su giao dich",
      "giao dich bi loi", "loi thanh toan",
      "ve chua thanh toan", "pending payment",
      "khach chua thanh toan"], "giao dich"),

    # ── Doanh thu ca (quản lý ca) ────────────────────────────────────────
    (["ket qua kinh doanh", "tong tien thu", "cuoi ca",
      "thu duoc bao nhieu", "so tien hom nay",
      "bao nhieu tien", "doanh thu hom nay",
      "ca nay thu duoc", "tong doanh thu"], "doanh thu"),

    # ── Thống kê nhân viên (quản lý ca) ─────────────────────────────────
    (["ai lam hom nay", "danh sach nhan vien hom nay",
      "nhan vien truc", "ai truc", "lich nhan vien",
      "so nhan vien", "bao nhieu nhan vien hom nay"], "nhan vien hom nay"),
]


def _expand_staff(nm: str) -> str:
    """Áp dụng _STAFF_SYNONYM_GROUPS (staff context) vào nm đã expand_synonyms."""
    return _apply_synonyms(nm, _STAFF_SYNONYM_GROUPS)


def _staff_action(label: str, action: str) -> dict:
    return {"type": "open_url", "label": label, "url": f"/Staff/Staff/{action}"}


def _friendly_db_error(area: str) -> str:
    return (
        f"Mình chưa đọc được dữ liệu {area} lúc này. "
        "Bạn vẫn có thể mở màn hình nghiệp vụ để thao tác trực tiếp."
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Role sets  (normalized via normalize() — removes diacritics, lowercase)
# ─────────────────────────────────────────────────────────────────────────────
_STAFF_ROLES = {
    "admin",
    "staff",
    "shiftmanager",
    "quan ly ca",          # Quản lý ca
    "nhan vien ban ve",    # Nhân viên bán vé
    "nhan vien",           # Nhân viên (generic)
    "quan ly",             # Quản lý (generic)
}
_SHIFTMANAGER_ROLES = {
    "admin",
    "shiftmanager",
    "quan ly ca",          # Quản lý ca
    "quan ly",             # Quản lý (generic)
}


def _is_staff_auth(role: Optional[str]) -> bool:
    return bool(role) and normalize(role) in _STAFF_ROLES


def _is_shiftmanager(role: Optional[str]) -> bool:
    return bool(role) and normalize(role) in _SHIFTMANAGER_ROLES


# ─────────────────────────────────────────────────────────────────────────────
#  Intent detectors
# ─────────────────────────────────────────────────────────────────────────────
def _is_seat_status_q(nm: str) -> bool:
    return any(kw in nm for kw in ["ghe trong", "con ghe", "lap day", "ghe con", "tinh trang ghe"])


def _is_showtime_q(nm: str) -> bool:
    return any(kw in nm for kw in [
        "suat chieu", "lich chieu", "gio chieu", "lich hom nay",
        "lich ngay", "suat nao", "nhung suat", "cac suat"
    ])


def _is_attendance_q(nm: str) -> bool:
    return any(kw in nm for kw in [
        "cham cong", "check in", "check out", "ca lam", "gio lam", "diem danh",
        "lam duoc bao nhieu phut", "lam duoc bao nhieu gio", "tien luong",
        "luong hom nay", "so gio lam", "gio cong", "da lam duoc",
        "hom nay lam", "lam bao nhieu", "cong hom nay",
    ])


def _is_counter_sale_q(nm: str) -> bool:
    return any(kw in nm for kw in ["ban tai quay", "quay ban", "ban truc tiep", "thu ngan", "counter"])


def _is_ticket_validation_q(nm: str) -> bool:
    return any(kw in nm for kw in [
        "xac thuc ve", "quet ve", "quet qr", "check qr", "check ve",
        "kiem ve", "ma ve", "qr ve", "ma booking", "validate ticket"
    ])


def _is_room_q(nm: str) -> bool:
    return any(kw in nm for kw in ["phong", "ghe", "bao tri", "tam dong"])


def _is_food_q(nm: str) -> bool:
    return any(kw in nm for kw in ["do an", "do uong", "combo", "bap", "nuoc", "menu"])


def _is_payment_q(nm: str) -> bool:
    return any(kw in nm for kw in ["thanh toan", "payos", "giao dich", "cho thanh toan", "that bai", "pending", "failed"])


def _is_revenue_q(nm: str) -> bool:
    """Báo cáo doanh thu — chỉ quản lý ca."""
    return any(kw in nm for kw in [
        "doanh thu", "bao cao", "thong ke doanh", "doanh so",
        "tong thu", "ban duoc bao nhieu", "thu duoc bao nhieu",
        "oanh so", "thu nhap hom nay",
    ])


def _is_staff_stats_q(nm: str) -> bool:
    """Thống kê nhân viên — chỉ quản lý ca."""
    return any(kw in nm for kw in [
        "thong ke nhan vien", "nhan su", "danh sach nhan vien",
        "so luong nhan vien", "nhan vien hom nay", "bao nhieu nhan vien",
        "co bao nhieu nguoi", "nhan vien nao",
    ])


def _table_exists(cursor, table_name: str) -> bool:
    cursor.execute("SELECT CASE WHEN OBJECT_ID(?, 'U') IS NULL THEN 0 ELSE 1 END", f"dbo.{table_name}")
    row = cursor.fetchone()
    return bool(row and int(row[0] or 0) == 1)


def _parse_attendance_datetime(value) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        ms = re.search(r"/Date\((-?\d+)", value)
        if ms:
            try:
                return datetime.utcfromtimestamp(int(ms.group(1)) / 1000)
            except Exception:
                return None
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone().replace(tzinfo=None)
            return parsed
        except Exception:
            return None
    return None


def _extract_staff_name(message: str, nm: str) -> Optional[str]:
    """Trích xuất tên/username nhân viên từ câu hỏi.
    Ví dụ: 'nhân viên tung1 hôm nay làm bao nhiêu' → 'tung1'
    """
    # Thử pattern: "nhân viên <name>" hoặc "nv <name>"
    for pat in [
        r"nhan vien\s+([a-z0-9_]+)",
        r"\bnv\s+([a-z0-9_]+)",
        r"cua\s+([a-z0-9_]+)\s+(?:hom nay|tuan|thang)",
        r"([a-z0-9_]+)\s+(?:hom nay|tuan nay|thang nay)\s+(?:lam|cong|gio)",
        r"([a-z0-9_]+)\s+(?:da lam|lam duoc|lam bao nhieu|cong bao nhieu)",
    ]:
        m = re.search(pat, nm)
        if m:
            candidate = m.group(1)
            # Bỏ qua các từ khóa thông thường
            if candidate not in {"hom", "nay", "tuan", "thang", "ca", "gio", "phut", "tien", "luong"}:
                return candidate
    return None


def _fetch_attendance_via_api(from_date: datetime, to_date: datetime,
                               staff_name: Optional[str] = None) -> Optional[list[dict]]:
    """Gọi /Api/ChatbotAttendanceData trên C# web để lấy dữ liệu attendance.json.
    Trả về list[dict] hoặc None nếu không gọi được.
    """
    if not _WEB_BASE_URL:
        return None
    try:
        params = {
            "from": from_date.strftime("%Y-%m-%d"),
            "to":   to_date.strftime("%Y-%m-%d"),
        }
        if staff_name:
            params["staffName"] = staff_name
        url = f"{_WEB_BASE_URL}/Api/ChatbotAttendanceData?{urllib.parse.urlencode(params)}"
        headers = {"X-Internal-Secret": _INTERNAL_SECRET} if _INTERNAL_SECRET else {}
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("success") and isinstance(data.get("items"), list):
                return data["items"]
    except Exception:
        pass
    return None


def _attendance_file_candidates() -> list[Path]:
    here = Path(__file__).resolve()
    env_path = os.getenv("ATTENDANCE_JSON_PATH", "").strip()
    candidates = []
    if env_path:
        candidates.append(Path(env_path))
    candidates.extend([
        here.parents[1] / "MetaCinemaWeb" / "RapChieuPhim" / "App_Data" / "attendance.json",
        here.parents[1] / "MetaCinemaWeb" / "RapChieuPhim" / "bin" / "App_Data" / "attendance.json",
    ])
    return candidates


def _load_attendance_json_records() -> Optional[list[dict]]:
    for path in _attendance_file_candidates():
        try:
            if not path.exists():
                continue
            raw = path.read_text(encoding="utf-8-sig")
            data = json.loads(raw or "[]")
            return data if isinstance(data, list) else []
        except Exception:
            continue
    return None


def _format_attendance_records(records: list[dict], label: str, staff_name_filter: Optional[str],
                                now: datetime, action_url: str = "Attendance") -> dict:
    """Format danh sách bản ghi chấm công thành reply tiếng Việt."""
    total_hours  = sum(float(r.get("Hours") or r.get("hours") or 0) for r in records)
    total_amount = sum(float(r.get("Amount") or r.get("amount") or 0) for r in records)
    staff_ids    = {r.get("StaffId") or r.get("staffId") for r in records}
    open_shifts  = sum(1 for r in records if not (r.get("CheckOut") or r.get("checkOut")))

    # --- Hiển thị chi tiết 1 nhân viên cụ thể ---
    if staff_name_filter and len(records) > 0:
        lines = [f"Thông tin chấm công của {records[0].get('StaffName') or staff_name_filter} {label.lower()}:"]
        for r in records[:5]:
            ci = _parse_attendance_datetime(r.get("CheckIn") or r.get("checkIn"))
            co = _parse_attendance_datetime(r.get("CheckOut") or r.get("checkOut"))
            ci_str = (ci + timedelta(hours=7)).strftime("%H:%M %d/%m") if ci else "—"
            co_str = (co + timedelta(hours=7)).strftime("%H:%M") if co else "đang làm"
            hrs    = float(r.get("Hours") or r.get("hours") or 0)
            mins   = round(hrs * 60)
            amt    = float(r.get("Amount") or r.get("amount") or 0)
            rate   = float(r.get("HourlyRate") or r.get("hourlyRate") or 0)
            lines.append(
                f"- Vào {ci_str} → Ra {co_str} | {mins} phút ({hrs:.2f}h)"
                + (f" | Lương: {_fmt_money(amt)}" if amt > 0 else "")
                + (f" | Đơn giá: {_fmt_money(rate)}/h" if rate > 0 else "")
            )
        if open_shifts:
            lines.append(f"⚠ {open_shifts} ca chưa checkout.")
        lines.append(f"Tổng: {round(total_hours * 60)} phút ({total_hours:.2f}h) — {_fmt_money(total_amount)}.")
        return {"reply": "\n".join(lines), "actions": [_staff_action("Chấm công", action_url)]}

    # --- Tổng hợp nhiều nhân viên ---
    lines = [
        f"Tổng hợp chấm công {label.lower()}:",
        f"- {len(records)} lượt, {len([x for x in staff_ids if x])} nhân viên, {open_shifts} ca đang mở.",
        f"- Tổng giờ: {total_hours:.2f}h | Tiền công: {_fmt_money(total_amount)}.",
    ]
    from collections import defaultdict
    by_staff: dict = defaultdict(lambda: {"hours": 0.0, "amount": 0.0, "open": 0, "name": ""})
    for r in records:
        key = r.get("StaffId") or r.get("staffId") or r.get("StaffName") or r.get("staffName") or "unknown"
        b = by_staff[key]
        b["name"] = r.get("StaffName") or r.get("staffName") or "Nhân viên"
        b["hours"]  += float(r.get("Hours")  or r.get("hours")  or 0)
        b["amount"] += float(r.get("Amount") or r.get("amount") or 0)
        if not (r.get("CheckOut") or r.get("checkOut")):
            b["open"] += 1
    top = sorted(by_staff.values(), key=lambda x: x["hours"], reverse=True)[:5]
    if top:
        lines.append("Theo nhân viên:")
        for item in top:
            extra = f", {item['open']} ca đang mở" if item["open"] else ""
            lines.append(f"- {item['name']}: {item['hours']:.2f}h — {_fmt_money(item['amount'])}{extra}.")
    return {"reply": "\n".join(lines), "actions": [_staff_action("Chấm công", action_url)]}


def _attendance_from_json(message: str, nm: str, now: datetime, action_url: str = "Attendance",
                           staff_name_filter: Optional[str] = None) -> Optional[dict]:
    records = _load_attendance_json_records()
    if records is None:
        return None

    rng = _build_report_range(message, nm, now, "week")
    start_utc = rng["start_utc"]
    end_utc   = rng["end_utc"]
    filtered  = []
    for item in records:
        check_in = _parse_attendance_datetime(item.get("CheckIn") or item.get("checkIn"))
        if not check_in or check_in < start_utc or check_in >= end_utc:
            continue
        role = item.get("Role") or item.get("role") or ""
        if normalize(role) == "admin":
            continue
        if staff_name_filter:
            name = (item.get("StaffName") or item.get("staffName") or "").lower()
            sid  = str(item.get("StaffId") or item.get("staffId") or "").lower()
            if staff_name_filter.lower() not in name and staff_name_filter.lower() not in sid:
                continue
        filtered.append(item)

    return _format_attendance_records(filtered, rng["label"], staff_name_filter, now, action_url)


# ─────────────────────────────────────────────────────────────────────────────
#  Dashboard — cơ bản (nhân viên bán vé): không có doanh thu
# ─────────────────────────────────────────────────────────────────────────────
def _staff_dashboard_basic(now: datetime) -> dict:
    today_start = now.date()
    today_end   = today_start + timedelta(days=1)
    next_week   = datetime.combine(today_start + timedelta(days=8), datetime.min.time())

    sql_shows_today = "SELECT COUNT(*) FROM Showtimes WHERE StartTime >= ? AND StartTime < ?"
    sql_upcoming    = "SELECT COUNT(*) FROM Showtimes WHERE StartTime >= ? AND StartTime < ?"
    sql_pending     = "SELECT COUNT(*) FROM Bookings WHERE PaymentStatus IN ('PendingPayment','PendingSelect')"
    sql_rooms       = "SELECT COUNT(*) FROM Rooms"

    try:
        with get_conn() as conn:
            c = conn.cursor()
            c.execute(sql_shows_today, today_start, today_end)
            shows_today = c.fetchone()[0]
            c.execute(sql_upcoming, now, next_week)
            upcoming = c.fetchone()[0]
            c.execute(sql_pending)
            pending = c.fetchone()[0]
            c.execute(sql_rooms)
            total_rooms = c.fetchone()[0]

        lines = [
            "Tổng quan vận hành hôm nay:",
            f"- Suất chiếu hôm nay: {shows_today} suất; 7 ngày tới: {upcoming} suất.",
            f"- Giao dịch khách đang chờ thanh toán: {pending}.",
            f"- Phòng chiếu: {total_rooms} phòng.",
        ]
        actions = [
            _staff_action("Xác thực vé", "Index"),
            _staff_action("Phòng chiếu", "RoomStatus"),
            _staff_action("Bán vé tại quầy", "Sales"),
        ]
        return {"reply": "\n".join(lines), "actions": actions}
    except Exception as e:
        return {"reply": _friendly_db_error("dashboard nhân viên"), "actions": [_staff_action("Trang nhân viên", "Index")]}


# ─────────────────────────────────────────────────────────────────────────────
#  Dashboard — đầy đủ (quản lý ca): có doanh thu
# ─────────────────────────────────────────────────────────────────────────────
def _staff_dashboard_full(now: datetime) -> dict:
    today_start  = now.date()
    today_end    = today_start + timedelta(days=1)
    today_utc_s  = _vn_to_utc(datetime.combine(today_start, datetime.min.time()))
    today_utc_e  = today_utc_s + timedelta(days=1)
    next_week    = datetime.combine(today_start + timedelta(days=8), datetime.min.time())

    sql_counter = """
        SELECT COUNT(b.Id) AS cnt,
               ISNULL(SUM(bs.seat_cnt), 0) AS tickets,
               ISNULL(SUM(b.GrandTotal), 0) AS revenue
        FROM Bookings b
        LEFT JOIN (SELECT BookingId, COUNT(*) seat_cnt FROM BookingSeats GROUP BY BookingId) bs
               ON bs.BookingId = b.Id
        WHERE (b.UserId IS NULL OR b.UserId = 0)
          AND b.PaymentStatus IN ('Paid','CheckedIn')
          AND b.CreatedAt >= ? AND b.CreatedAt < ?
    """
    sql_online = """
        SELECT ISNULL(SUM(b.GrandTotal), 0) AS revenue
        FROM Bookings b
        WHERE b.UserId IS NOT NULL AND b.UserId > 0
          AND b.PaymentStatus IN ('Paid','CheckedIn')
          AND b.CreatedAt >= ? AND b.CreatedAt < ?
    """
    sql_shows_today = "SELECT COUNT(*) FROM Showtimes WHERE StartTime >= ? AND StartTime < ?"
    sql_upcoming    = "SELECT COUNT(*) FROM Showtimes WHERE StartTime >= ? AND StartTime < ?"
    sql_pending     = "SELECT COUNT(*) FROM Bookings WHERE PaymentStatus IN ('PendingPayment','PendingSelect')"
    sql_rooms       = "SELECT COUNT(*) FROM Rooms"

    try:
        with get_conn() as conn:
            c = conn.cursor()

            c.execute(sql_counter, today_utc_s, today_utc_e)
            row = c.fetchone()
            cnt_invoices  = int(row.cnt or 0)
            cnt_tickets   = int(row.tickets or 0)
            cnt_revenue   = float(row.revenue or 0)

            c.execute(sql_online, today_utc_s, today_utc_e)
            online_revenue = float(c.fetchone()[0] or 0)

            c.execute(sql_shows_today, today_start, today_end)
            shows_today = c.fetchone()[0]

            c.execute(sql_upcoming, now, next_week)
            upcoming = c.fetchone()[0]

            c.execute(sql_pending)
            pending = c.fetchone()[0]

            c.execute(sql_rooms)
            total_rooms = c.fetchone()[0]

        total_revenue = cnt_revenue + online_revenue
        lines = [
            "Tổng quan vận hành hôm nay (Quản lý ca):",
            f"- Suất chiếu hôm nay: {shows_today} suất; 7 ngày tới: {upcoming} suất.",
            f"- Bán tại quầy: {cnt_invoices} hóa đơn, {cnt_tickets} vé — {_fmt_money(cnt_revenue)}.",
            f"- Đặt vé online: {_fmt_money(online_revenue)}.",
            f"- Tổng doanh thu hôm nay: {_fmt_money(total_revenue)}.",
            f"- Giao dịch đang chờ: {pending}.",
            f"- Phòng chiếu: {total_rooms} phòng.",
        ]
        actions = [
            _staff_action("Xác thực vé", "Index"),
            _staff_action("Phòng chiếu", "RoomStatus"),
            _staff_action("Bán vé tại quầy", "Sales"),
            _staff_action("Chấm công", "Attendance"),
        ]
        return {"reply": "\n".join(lines), "actions": actions}
    except Exception as e:
        return {"reply": _friendly_db_error("dashboard nhân viên"), "actions": [_staff_action("Trang nhân viên", "Index")]}


# ─────────────────────────────────────────────────────────────────────────────
#  Staff room status
# ─────────────────────────────────────────────────────────────────────────────
def _staff_room(now: datetime) -> dict:
    today_start = now.date()
    today_end   = today_start + timedelta(days=1)

    sql = """
        SELECT r.Id, r.Name,
               ISNULL(r.Status, N'Sẵn sàng') AS status,
               (SELECT COUNT(*) FROM Seats s WHERE s.RoomId = r.Id AND s.IsActive = 1) AS active_seats,
               (SELECT COUNT(*) FROM Seats s WHERE s.RoomId = r.Id AND s.IsActive = 0) AS broken_seats,
               (SELECT COUNT(*) FROM Showtimes st WHERE st.RoomId = r.Id
                AND st.StartTime >= ? AND st.StartTime < ?) AS shows_today
        FROM Rooms r ORDER BY r.Name
    """
    try:
        with get_conn() as conn:
            c = conn.cursor()
            c.execute(sql, today_start, today_end)
            rooms = c.fetchall()

        lines = ["Tình trạng phòng chiếu hôm nay:"]
        for r in rooms:
            name = r.Name if normalize(r.Name).startswith("phong") else f"Phòng {r.Name}"
            lines.append(
                f"- {name}: {r.status}, {int(r.active_seats or 0)} ghế hoạt động, "
                f"{int(r.broken_seats or 0)} ghế khóa/bảo trì, {int(r.shows_today or 0)} suất hôm nay."
            )

        return {"reply": "\n".join(lines), "actions": [_staff_action("Quản lý phòng", "RoomStatus")]}
    except Exception as e:
        return {"reply": _friendly_db_error("phòng chiếu"), "actions": [_staff_action("Phòng chiếu", "RoomStatus")]}


# ─────────────────────────────────────────────────────────────────────────────
#  Staff showtime view (read-only)
# ─────────────────────────────────────────────────────────────────────────────
def _staff_showtime(message: str, now: datetime) -> dict:
    requested = extract_requested_date(message, now) or datetime.combine(now.date(), datetime.min.time())
    day_start = requested.replace(hour=0, minute=0, second=0)
    day_end   = day_start + timedelta(days=1)

    sql = """
        SELECT s.StartTime, ISNULL(m.Title,'Phim chua ro') AS title,
               ISNULL(r.Name,'N/A') AS room_name,
               (r.SeatRows * r.SeatCols) AS total_seats,
               (SELECT COUNT(*) FROM BookingSeats bs
                INNER JOIN Bookings b ON b.Id = bs.BookingId
                WHERE b.ShowtimeId = s.Id AND b.PaymentStatus IN ('Paid','PendingPayment','CheckedIn')
               ) AS sold_seats
        FROM Showtimes s
        LEFT JOIN Movies m ON m.Id = s.MovieId
        LEFT JOIN Rooms  r ON r.Id = s.RoomId
        WHERE s.StartTime >= ? AND s.StartTime < ?
        ORDER BY s.StartTime
    """
    try:
        with get_conn() as conn:
            c = conn.cursor()
            c.execute(sql, day_start, day_end)
            rows = c.fetchall()

        lines = [f"Suất chiếu ngày {day_start.strftime('%d/%m/%Y')}: {len(rows)} suất."]
        if not rows:
            lines.append("Chưa có suất nào trong ngày này.")
        else:
            for s in rows[:8]:
                total = int(s.total_seats or 0)
                sold  = int(s.sold_seats or 0)
                room  = s.room_name if normalize(s.room_name).startswith("phong") else f"Phòng {s.room_name}"
                lines.append(f"- {s.StartTime.strftime('%H:%M')} | {s.title} | {room} | đã bán/giữ {sold}/{total}")
            if len(rows) > 8:
                lines.append(f"Còn {len(rows) - 8} suất khác trong ngày.")

        return {"reply": "\n".join(lines), "actions": [_staff_action("Xác thực vé", "Index")]}
    except Exception as e:
        return {"reply": _friendly_db_error("suất chiếu"), "actions": [_staff_action("Xác thực vé", "Index")]}


# ─────────────────────────────────────────────────────────────────────────────
#  Attendance — chỉ quản lý ca, có dữ liệu thực hôm nay
# ─────────────────────────────────────────────────────────────────────────────
def _staff_attendance(role: Optional[str], now: datetime, message: str = "hôm nay", nm: str = "hom nay") -> dict:
    if not _is_shiftmanager(role):
        return {
            "reply": "Chấm công chỉ dành cho Quản lý ca. Tài khoản nhân viên bán vé không có quyền truy cập mục này.",
            "actions": [_staff_action("Trang nhân viên", "Index")],
        }

    staff_name = _extract_staff_name(message, nm)
    rng        = _build_report_range(message, nm, now, "day")
    from_date  = rng["start_utc"].date() if hasattr(rng.get("start_utc"), "date") else now.date()
    to_date    = rng["end_utc"].date()   if hasattr(rng.get("end_utc"),   "date") else now.date()

    # ── 1. Thử gọi HTTP API (khi deploy trên Render) ─────────────────────────
    try:
        api_records = _fetch_attendance_via_api(
            datetime.combine(from_date, datetime.min.time()),
            datetime.combine(to_date,   datetime.min.time()),
            staff_name,
        )
        if api_records is not None:
            if not api_records:
                who = f" của {staff_name}" if staff_name else ""
                return {
                    "reply": f"Chưa có dữ liệu chấm công{who} trong khoảng thời gian này.",
                    "actions": [_staff_action("Chấm công", "Attendance")],
                }
            return _format_attendance_records(api_records, rng["label"], staff_name, now)
    except Exception:
        pass

    # ── 2. Thử đọc file JSON local (môi trường dev) ──────────────────────────
    json_reply = _attendance_from_json(message, nm, now, staff_name_filter=staff_name)
    if json_reply is not None:
        return json_reply

    # ── 3. Báo lỗi rõ ràng — không còn thông báo kỹ thuật lộ ra ngoài ───────
    return {
        "reply": (
            "Mình chưa đọc được dữ liệu chấm công lúc này. "
            "Bạn có thể mở trang Chấm công để xem trực tiếp."
        ),
        "actions": [_staff_action("Chấm công", "Attendance")],
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Staff statistics — chỉ quản lý ca
# ─────────────────────────────────────────────────────────────────────────────
def _staff_stats(now: datetime) -> dict:
    sql_by_role = "SELECT Role, COUNT(*) AS cnt FROM Staff GROUP BY Role ORDER BY cnt DESC"
    sql_total   = "SELECT COUNT(*) FROM Staff"
    sql_checkin_today = """
        SELECT COUNT(DISTINCT a.StaffId) AS cnt
        FROM Attendance a
        WHERE a.CheckIn >= ? AND a.CheckIn < ?
    """

    try:
        # Lấy dữ liệu check-in hôm nay: ưu tiên API → file JSON → bỏ qua
        today_start = datetime.combine(now.date(), datetime.min.time())
        api_records = _fetch_attendance_via_api(today_start, today_start)
        if api_records is not None:
            checked_ids = {r.get("StaffId") or r.get("staffId") for r in api_records if r}
            checkin_today = len([x for x in checked_ids if x])
        else:
            json_records = _load_attendance_json_records()
            if json_records is not None:
                today_utc_s = _vn_to_utc(today_start)
                today_utc_e = today_utc_s + timedelta(days=1)
                checked_ids = set()
                for item in json_records:
                    ci = _parse_attendance_datetime(item.get("CheckIn") or item.get("checkIn"))
                    if ci and today_utc_s <= ci < today_utc_e:
                        checked_ids.add(item.get("StaffId") or item.get("staffId"))
                checkin_today = len([x for x in checked_ids if x])
            else:
                checkin_today = None

        with get_conn() as conn:
            c = conn.cursor()
            c.execute(sql_total)
            total_staff = int(c.fetchone()[0] or 0)
            c.execute(sql_by_role)
            roles = c.fetchall()

        lines = [f"Thống kê nhân viên ({now.strftime('%d/%m/%Y')}):"]
        lines.append(f"- Tổng số nhân viên: {total_staff} người.")
        for r in roles:
            lines.append(f"  • {r.Role or 'Chưa phân vai'}: {int(r.cnt)} người")
        if checkin_today is None:
            lines.append("- Chưa có dữ liệu check-in hôm nay.")
        else:
            lines.append(f"- Đã check-in hôm nay: {checkin_today} người.")

        return {
            "reply": "\n".join(lines),
            "actions": [_staff_action("Chấm công", "Attendance")],
        }
    except Exception as e:
        return {
            "reply": _friendly_db_error("thống kê nhân viên"),
            "actions": [_staff_action("Trang nhân viên", "Index")],
        }


# ─────────────────────────────────────────────────────────────────────────────
#  Counter sales — nhân viên bán vé chỉ xem hướng dẫn, không thấy doanh thu
# ─────────────────────────────────────────────────────────────────────────────
def _counter_sale_info() -> dict:
    """Hướng dẫn bán vé tại quầy — không hiển thị doanh thu."""
    lines = [
        "Bán vé tại quầy — hướng dẫn nhanh:",
        "1. Vào trang Bán vé tại quầy.",
        "2. Chọn phim → chọn suất chiếu → chọn ghế trống.",
        "3. Thêm đồ ăn/uống nếu khách muốn.",
        "4. Nhấn Hoàn tất — hóa đơn được ghi nhận ngay.",
        "Báo cáo doanh thu chi tiết chỉ dành cho Quản lý ca.",
    ]
    return {
        "reply": "\n".join(lines),
        "actions": [_staff_action("Bán vé tại quầy", "Sales")],
    }


def _ticket_validation_info() -> dict:
    lines = [
        "Xác thực vé nhanh:",
        "1. Vào màn hình Xác thực vé.",
        "2. Quét QR hoặc nhập mã vé dạng BKG-xxxxxx.",
        "3. Kiểm tra trạng thái hợp lệ, phim, suất chiếu, phòng và ghế.",
        "4. Nếu vé hợp lệ, xác nhận khách vào rạp; nếu không hợp lệ, kiểm tra lại lịch sử thanh toán/booking.",
    ]
    return {
        "reply": "\n".join(lines),
        "actions": [_staff_action("Xác thực vé", "Index")],
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Main entry point
# ─────────────────────────────────────────────────────────────────────────────
def try_build_staff_reply(message: str, role: Optional[str], now: datetime) -> Optional[dict]:
    """Called from main.py when pageContext.area/mode == 'staff'. Returns None → fall through to Gemini."""

    # ── Kiểm tra xác thực ────────────────────────────────────────────────────
    if not _is_staff_auth(role):
        return {
            "reply": "Bạn cần đăng nhập tài khoản nhân viên để dùng trợ lý vận hành.",
            "actions": [{"type": "open_url", "label": "Đăng nhập", "url": "/User/Login"}],
        }

    is_manager = _is_shiftmanager(role)
    nm = _expand_staff(expand_synonyms(normalize(message)))

    # ── Thống kê nhân viên (chỉ quản lý ca) ─────────────────────────────────
    if _is_staff_stats_q(nm):
        if not is_manager:
            return {
                "reply": "Thống kê nhân viên chỉ dành cho Quản lý ca.",
                "actions": [_staff_action("Trang nhân viên", "Index")],
            }
        return _staff_stats(now)

    # ── Báo cáo doanh thu (chỉ quản lý ca) ──────────────────────────────────
    if _is_revenue_q(nm):
        if not is_manager:
            return {
                "reply": "Báo cáo doanh thu chỉ dành cho Quản lý ca. Bạn không có quyền xem mục này.",
                "actions": [_staff_action("Trang nhân viên", "Index")],
            }
        return _counter_sales(message, nm, now)

    # ── Chấm công (chỉ quản lý ca) ───────────────────────────────────────────
    if _is_attendance_q(nm):
        return _staff_attendance(role, now, message, nm)

    # ── Suất chiếu / ghế ─────────────────────────────────────────────────────
    if _is_seat_status_q(nm) or _is_showtime_q(nm):
        return _staff_showtime(message, now)

    # ── Bán vé tại quầy ──────────────────────────────────────────────────────
    if _is_counter_sale_q(nm):
        # Quản lý ca xem được doanh thu quầy; nhân viên chỉ xem hướng dẫn
        if is_manager:
            return _counter_sales(message, nm, now)
        return _counter_sale_info()

    # ── Xác thực vé / QR ───────────────────────────────────────────────────
    if _is_ticket_validation_q(nm):
        return _ticket_validation_info()

    # ── Phòng chiếu ──────────────────────────────────────────────────────────
    if _is_room_q(nm):
        return _staff_room(now)

    # ── Đồ ăn/uống ───────────────────────────────────────────────────────────
    if _is_food_q(nm):
        return _food(message, nm, now)

    # ── Thanh toán ───────────────────────────────────────────────────────────
    if _is_payment_q(nm):
        return _payment(message, nm, now)

    # ── Dashboard mặc định (phân theo role) ─────────────────────────────────
    if is_manager:
        return _staff_dashboard_full(now)
    return _staff_dashboard_basic(now)
