"""
Port of ChatController.cs BuildStaffReply and sub-methods.
Auth: C# passes role from Session["StaffRole"] → Python trusts it.
"""
import re
from datetime import datetime, timedelta
from typing import Optional
from db import get_conn
from intent_router import normalize, extract_requested_date
from admin_router import (
    _build_report_range, _fmt_money, _vn_to_utc,
    _counter_sales, _payment, _food,
)


def _staff_action(label: str, action: str) -> dict:
    return {"type": "open_url", "label": label, "url": f"/Staff/Staff/{action}"}


# Normalized role names (after calling normalize()) that count as staff/shiftmanager.
# C# stores Vietnamese role strings like "Quản lý ca", "Nhân viên bán vé" in the DB.
# normalize() strips diacritics → "quan ly ca", "nhan vien ban ve", "admin".
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
    return any(kw in nm for kw in ["suat chieu", "lich chieu", "gio chieu", "lich hom nay", "lich ngay"])


def _is_attendance_q(nm: str) -> bool:
    return any(kw in nm for kw in ["cham cong", "check in", "check out", "ca lam", "gio lam"])


def _is_counter_sale_q(nm: str) -> bool:
    return any(kw in nm for kw in ["ban tai quay", "quay ban", "ban truc tiep", "thu ngan", "counter"])


def _is_room_q(nm: str) -> bool:
    return any(kw in nm for kw in ["phong", "ghe", "bao tri", "tam dong"])


def _is_food_q(nm: str) -> bool:
    return any(kw in nm for kw in ["do an", "do uong", "combo", "bap", "nuoc", "menu"])


def _is_payment_q(nm: str) -> bool:
    return any(kw in nm for kw in ["thanh toan", "payos", "giao dich", "cho thanh toan", "that bai", "pending", "failed"])


# ─────────────────────────────────────────────────────────────────────────────
#  Staff dashboard
# ─────────────────────────────────────────────────────────────────────────────
def _staff_dashboard(role: Optional[str], now: datetime) -> dict:
    today_start = now.date()
    today_end = today_start + timedelta(days=1)
    today_utc_s = _vn_to_utc(datetime.combine(today_start, datetime.min.time()))
    today_utc_e = today_utc_s + timedelta(days=1)
    next_week = datetime.combine(today_start + timedelta(days=8), datetime.min.time())

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
    sql_shows_today = "SELECT COUNT(*) FROM Showtimes WHERE StartTime >= ? AND StartTime < ?"
    sql_upcoming = "SELECT COUNT(*) FROM Showtimes WHERE StartTime >= ? AND StartTime < ?"
    sql_pending = "SELECT COUNT(*) FROM Bookings WHERE PaymentStatus IN ('PendingPayment','PendingSelect')"
    sql_rooms = "SELECT COUNT(*) FROM Rooms"

    try:
        with get_conn() as conn:
            c = conn.cursor()

            c.execute(sql_counter, today_utc_s, today_utc_e)
            row = c.fetchone()
            cnt_invoices = int(row.cnt or 0)
            cnt_tickets = int(row.tickets or 0)
            cnt_revenue = float(row.revenue or 0)

            c.execute(sql_shows_today, today_start, today_end)
            shows_today = c.fetchone()[0]

            c.execute(sql_upcoming, now, next_week)
            upcoming = c.fetchone()[0]

            c.execute(sql_pending)
            pending = c.fetchone()[0]

            c.execute(sql_rooms)
            total_rooms = c.fetchone()[0]

        lines = [
            "Tổng quan vận hành nhân viên:",
            f"- Hôm nay có {shows_today} suất chiếu; 7 ngày tới còn {upcoming} suất.",
            f"- Bán tại quầy hôm nay: {cnt_invoices} hóa đơn, {cnt_tickets} vé, {_fmt_money(cnt_revenue)}.",
            f"- Giao dịch khách đang chờ: {pending}.",
            f"- Phòng chiếu: {total_rooms} phòng.",
        ]
        actions = [
            _staff_action("Xác thực vé", "Index"),
            _staff_action("Phòng chiếu", "RoomStatus"),
            _staff_action("Bán vé tại quầy", "Sales"),
        ]
        if _is_shiftmanager(role):
            actions.append(_staff_action("Chấm công", "Attendance"))

        return {"reply": "\n".join(lines), "actions": actions}
    except Exception as e:
        return {"reply": f"Lỗi tải dashboard nhân viên: {e}", "actions": [_staff_action("Trang nhân viên", "Index")]}


# ─────────────────────────────────────────────────────────────────────────────
#  Staff room status
# ─────────────────────────────────────────────────────────────────────────────
def _staff_room(now: datetime) -> dict:
    today_start = now.date()
    today_end = today_start + timedelta(days=1)

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
        return {"reply": f"Lỗi tải phòng: {e}", "actions": [_staff_action("Phòng chiếu", "RoomStatus")]}


# ─────────────────────────────────────────────────────────────────────────────
#  Staff showtime view (read-only)
# ─────────────────────────────────────────────────────────────────────────────
def _staff_showtime(message: str, now: datetime) -> dict:
    requested = extract_requested_date(message, now) or datetime.combine(now.date(), datetime.min.time())
    day_start = requested.replace(hour=0, minute=0, second=0)
    day_end = day_start + timedelta(days=1)

    sql = """
        SELECT s.StartTime, ISNULL(m.Title,'Phim chưa rõ') AS title,
               ISNULL(r.Name,'?') AS room_name,
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
                sold = int(s.sold_seats or 0)
                room = s.room_name if normalize(s.room_name).startswith("phong") else f"Phòng {s.room_name}"
                lines.append(f"- {s.StartTime.strftime('%H:%M')} | {s.title} | {room} | đã bán/giữ {sold}/{total}")
            if len(rows) > 8:
                lines.append(f"Còn {len(rows) - 8} suất khác trong ngày.")

        return {"reply": "\n".join(lines), "actions": [_staff_action("Xác thực vé", "Index")]}
    except Exception as e:
        return {"reply": f"Lỗi tải suất chiếu: {e}", "actions": []}


# ─────────────────────────────────────────────────────────────────────────────
#  Staff attendance (ShiftManager only)
# ─────────────────────────────────────────────────────────────────────────────
def _staff_attendance(role: Optional[str]) -> dict:
    if not _is_shiftmanager(role):
        return {
            "reply": "Chỉ quản lý ca mới xem được dữ liệu chấm công trong khu vực nhân viên.",
            "actions": [_staff_action("Trang nhân viên", "Index")],
        }
    return {
        "reply": "Mình chưa hỗ trợ xem chấm công qua chatbot Python. Bạn vào trang Chấm công để xem chi tiết.",
        "actions": [_staff_action("Chấm công", "Attendance")],
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Main entry point
# ─────────────────────────────────────────────────────────────────────────────
def try_build_staff_reply(message: str, role: Optional[str], now: datetime) -> Optional[dict]:
    """Called from main.py when pageContext.area/mode == 'staff'. Returns None → fall through to Gemini."""
    if not _is_staff_auth(role):
        return {
            "reply": "Bạn cần đăng nhập tài khoản nhân viên để dùng trợ lý vận hành.",
            "actions": [{"type": "open_url", "label": "Đăng nhập", "url": "/User/Login"}],
        }

    nm = normalize(message)

    if _is_seat_status_q(nm) or _is_showtime_q(nm):
        return _staff_showtime(message, now)
    if _is_attendance_q(nm):
        return _staff_attendance(role)
    if _is_counter_sale_q(nm):
        return _counter_sales(message, nm, now)
    if _is_room_q(nm):
        return _staff_room(now)
    if _is_food_q(nm):
        return _food(message, nm, now)
    if _is_payment_q(nm):
        return _payment(message, nm, now)

    return _staff_dashboard(role, now)
