"""
Port of ChatController.cs BuildStaffReply and sub-methods.
Auth: C# passes role from Session["StaffRole"] → Python trusts it.

Role-based access:
  Nhân viên bán vé  → showtime, room, food, payment info, counter guide (NO revenue)
  Quản lý ca / Admin → all above + revenue report, staff stats, attendance
"""
import re
from datetime import datetime, timedelta
from typing import Optional
from db import get_conn
from intent_router import normalize, expand_synonyms, extract_requested_date
from admin_router import (
    _build_report_range, _fmt_money, _vn_to_utc,
    _counter_sales, _payment, _food,
)


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
    """Thêm canonical keywords staff-specific vào nm."""
    extras: list[str] = []
    for variants, canonical in _STAFF_SYNONYM_GROUPS:
        if canonical not in nm and any(v in nm for v in variants):
            extras.append(canonical)
    return nm + (" " + " ".join(extras) if extras else "")


def _staff_action(label: str, action: str) -> dict:
    return {"type": "open_url", "label": label, "url": f"/Staff/Staff/{action}"}


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
    return any(kw in nm for kw in ["cham cong", "check in", "check out", "ca lam", "gio lam", "diem danh"])


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
        return {"reply": f"Lỗi tải dashboard: {e}", "actions": [_staff_action("Trang nhân viên", "Index")]}


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
        return {"reply": f"Lỗi tải dashboard: {e}", "actions": [_staff_action("Trang nhân viên", "Index")]}


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
        return {"reply": f"Lỗi tải phòng: {e}", "actions": [_staff_action("Phòng chiếu", "RoomStatus")]}


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
        return {"reply": f"Lỗi tải suất chiếu: {e}", "actions": []}


# ─────────────────────────────────────────────────────────────────────────────
#  Attendance — chỉ quản lý ca, có dữ liệu thực hôm nay
# ─────────────────────────────────────────────────────────────────────────────
def _staff_attendance(role: Optional[str], now: datetime) -> dict:
    if not _is_shiftmanager(role):
        return {
            "reply": "Chức năng chấm công chỉ dành cho Quản lý ca. Bạn không có quyền truy cập.",
            "actions": [_staff_action("Trang nhân viên", "Index")],
        }

    today_start = datetime.combine(now.date(), datetime.min.time())
    today_end   = today_start + timedelta(days=1)
    today_utc_s = _vn_to_utc(today_start)
    today_utc_e = today_utc_s + timedelta(days=1)

    sql = """
        SELECT a.StaffName, a.Role,
               a.CheckIn, a.CheckOut,
               ISNULL(a.Hours, 0) AS hours,
               ISNULL(a.Amount, 0) AS amount
        FROM Attendance a
        WHERE a.CheckIn >= ? AND a.CheckIn < ?
        ORDER BY a.CheckIn
    """
    sql_total = """
        SELECT COUNT(*) AS cnt,
               ISNULL(SUM(a.Hours), 0) AS total_hours,
               ISNULL(SUM(a.Amount), 0) AS total_amount
        FROM Attendance a
        WHERE a.CheckIn >= ? AND a.CheckIn < ?
    """
    try:
        with get_conn() as conn:
            c = conn.cursor()
            c.execute(sql, today_utc_s, today_utc_e)
            rows = c.fetchall()
            c.execute(sql_total, today_utc_s, today_utc_e)
            tot = c.fetchone()

        cnt_staff    = int(tot.cnt or 0)
        total_hours  = float(tot.total_hours or 0)
        total_amount = float(tot.total_amount or 0)

        lines = [f"Chấm công hôm nay ({now.strftime('%d/%m/%Y')}): {cnt_staff} lượt."]
        if not rows:
            lines.append("Chưa có nhân viên nào check-in hôm nay.")
        else:
            for r in rows[:10]:
                checkin_vn  = r.CheckIn  + timedelta(hours=7)
                checkout_vn = (r.CheckOut + timedelta(hours=7)).strftime("%H:%M") if r.CheckOut else "—"
                hrs = f"{float(r.hours or 0):.1f}h"
                lines.append(
                    f"- {r.StaffName} ({r.Role}): vào {checkin_vn.strftime('%H:%M')} — ra {checkout_vn} | {hrs} | {_fmt_money(float(r.amount or 0))}"
                )
            if len(rows) > 10:
                lines.append(f"Còn {len(rows) - 10} nhân viên khác.")
            lines.append(f"Tổng: {total_hours:.1f} giờ — {_fmt_money(total_amount)}.")

        return {
            "reply": "\n".join(lines),
            "actions": [_staff_action("Trang chấm công", "Attendance")],
        }
    except Exception as e:
        return {
            "reply": f"Lỗi tải dữ liệu chấm công: {e}",
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
        with get_conn() as conn:
            c = conn.cursor()

            c.execute(sql_total)
            total_staff = int(c.fetchone()[0] or 0)

            c.execute(sql_by_role)
            roles = c.fetchall()

            today_utc_s = _vn_to_utc(datetime.combine(now.date(), datetime.min.time()))
            today_utc_e = today_utc_s + timedelta(days=1)
            c.execute(sql_checkin_today, today_utc_s, today_utc_e)
            checkin_today = int(c.fetchone().cnt or 0)

        lines = [f"Thống kê nhân viên ({now.strftime('%d/%m/%Y')}):"]
        lines.append(f"- Tổng số nhân viên: {total_staff} người.")
        for r in roles:
            lines.append(f"  • {r.Role or 'Chưa phân vai'}: {int(r.cnt)} người")
        lines.append(f"- Đã check-in hôm nay: {checkin_today} người.")

        return {
            "reply": "\n".join(lines),
            "actions": [_staff_action("Chấm công", "Attendance")],
        }
    except Exception as e:
        return {
            "reply": f"Lỗi tải thống kê nhân viên: {e}",
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
        return _staff_attendance(role, now)

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
