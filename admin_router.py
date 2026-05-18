"""
Port of ChatController.cs BuildAdminReply and sub-methods.
Auth: C# passes role from Session["StaffRole"] → Python trusts it (C# is auth gatekeeper).
"""
import re
from datetime import datetime, timedelta, date as date_type, time as dt_time
from typing import Optional
from db import get_conn
from intent_router import normalize, expand_synonyms, _apply_synonyms, extract_requested_date


# ─────────────────────────────────────────────────────────────────────────────
#  Admin-specific synonym expansion
#  Bổ sung sau expand_synonyms() chung — chỉ áp dụng trong context admin.
#  Tất cả string ở dạng normalize() (không dấu, lowercase, single space).
# ─────────────────────────────────────────────────────────────────────────────
_ADMIN_SYNONYM_GROUPS: list[tuple[list[str], str]] = [
    # ── Suất chiếu ───────────────────────────────────────────────────────
    (["xem lich", "lich phim", "phim hom nay", "buoi hom nay",
      "suat hom nay", "suat ngay", "lich tuan nay"], "lich chieu"),
    # Verb tạo lịch (trigger _is_create_showtime_q)
    (["lap lich", "xep lich", "len lich", "bo sung suat",
      "tao moi suat", "lap suat", "tao lich chieu",
      "them lich", "tao them", "bo sung lich", "sap lich",
      "day them suat", "chay them suat"], "tao"),
    # Verb xóa (trigger _is_delete_showtime_q)
    (["huy suat", "xoa suat", "loai bo suat", "bo suat",
      "xoa lich", "huy lich chieu", "xoa het suat"], "xoa"),

    # ── Thanh toán / giao dịch ───────────────────────────────────────────
    (["hoa don", "bill", "receipt", "thu tien",
      "lich su giao dich", "lich su thanh toan",
      "chuyen khoan den", "tien vao", "tra tien roi"], "giao dich"),

    # ── Doanh thu / báo cáo ──────────────────────────────────────────────
    (["ket qua kinh doanh", "loi nhuan", "tong tien thu",
      "so tien thu duoc", "cuoi ngay thu duoc",
      "doanh thu hom nay", "thu duoc bao nhieu",
      "ban duoc bao nhieu tien", "hieu qua kinh doanh"], "doanh thu"),
    (["bao cao tong hop", "thong ke tong", "bao cao ngay",
      "bao cao tuan", "bao cao thang", "xuat bao cao"], "bao cao"),

    # ── Phim ─────────────────────────────────────────────────────────────
    (["danh sach phim", "quan ly phim", "phim moi nhat",
      "them phim", "cap nhat phim", "phim sap ra",
      "bo sung phim", "chỉnh phim"], "sap chieu"),

    # ── Phòng chiếu ──────────────────────────────────────────────────────
    (["phong chieu", "rap chieu", "tinh trang phong",
      "phong bi loi", "kiem tra phong", "sua chua phong",
      "phong dang dung", "so phong", "capacity"], "phong"),

    # ── Nhân viên / phân quyền ───────────────────────────────────────────
    (["quan ly nhan vien", "quan ly nhan su", "danh sach staff",
      "phan quyen", "cap quyen", "thay doi chuc vu",
      "nhan luc", "tuyen dung", "bo phan", "cap nhat quyen"], "nhan vien"),

    # ── Khách hàng / tài khoản ───────────────────────────────────────────
    (["thanh vien", "dang ky", "member",
      "profile khach", "tai khoan khach", "danh sach khach",
      "khach moi", "nguoi mua"], "khach hang"),

    # ── Chatbot knowledge ────────────────────────────────────────────────
    (["day bot", "noi dung bot", "cap nhat kien thuc",
      "sua chatbot", "them cau hoi", "them cau tra loi",
      "bot biet gi", "chatbot tra loi gi", "faq"], "kien thuc"),

    # ── Chấm công / ca làm ───────────────────────────────────────────────
    (["nghi phep", "vang mat", "di muon", "ngay cong",
      "gio vao", "gio ra", "cong lam", "tinh luong",
      "quy dinh ca"], "cham cong"),

    # ── Bán tại quầy ─────────────────────────────────────────────────────
    (["ve tai quay", "ban ve cho khach", "thanh toan quay",
      "pos", "quay vet", "thu ngan quay",
      "dat ve cho khach", "ban truc tiep"], "ban tai quay"),

    # ── Dữ liệu / kiến trúc hệ thống ────────────────────────────────────
    (["erd", "schema", "mo hinh du lieu", "cau truc he thong",
      "diagram", "entity", "bang du lieu", "thiet ke db"], "database"),
]


def _expand_admin(nm: str) -> str:
    """Áp dụng _ADMIN_SYNONYM_GROUPS (admin context) vào nm đã expand_synonyms."""
    return _apply_synonyms(nm, _ADMIN_SYNONYM_GROUPS)


# ─────────────────────────────────────────────────────────────────────────────
#  Admin URL helpers
# ─────────────────────────────────────────────────────────────────────────────
def _admin_action(label: str, action: str) -> dict:
    return {"type": "open_url", "label": label, "url": f"/Admin/Admin/{action}"}


def _admin_quick_actions() -> list:
    return [
        _admin_action("Bảng điều khiển", "Index"),
        _admin_action("Suất chiếu", "Showtimes"),
        _admin_action("Phim", "Movies"),
        _admin_action("Phòng", "Rooms"),
        _admin_action("Chatbot", "ChatbotKnowledge"),
    ]


def _fmt_money(value) -> str:
    try:
        v = float(value or 0)
        return f"{int(v):,}đ".replace(",", ".")
    except Exception:
        return "0đ"


# ─────────────────────────────────────────────────────────────────────────────
#  Date range parser — port of BuildAdminReportRange
# ─────────────────────────────────────────────────────────────────────────────
VN_OFFSET = timedelta(hours=7)


def _vn_to_utc(dt: datetime) -> datetime:
    return dt - VN_OFFSET


def _extract_date_mentions(message: str, now: datetime) -> list:
    dates = []
    for m in re.finditer(r"\b(?P<y>\d{4})-(?P<mo>\d{1,2})-(?P<d>\d{1,2})\b", message):
        try:
            dates.append(datetime(int(m.group("y")), int(m.group("mo")), int(m.group("d"))))
        except ValueError:
            pass
    for m in re.finditer(r"\b(?P<d>\d{1,2})[/\-.](?P<mo>\d{1,2})(?:[/\-.](?P<y>\d{2,4}))?\b", message):
        try:
            day, month = int(m.group("d")), int(m.group("mo"))
            year = int(m.group("y")) if m.group("y") else now.year
            if year < 100:
                year += 2000
            dates.append(datetime(year, month, day))
        except ValueError:
            pass
    return sorted(set(d.date() for d in dates))


def _start_of_week(date) -> datetime:
    wd = date.weekday()  # Mon=0
    return datetime.combine(date - timedelta(days=wd), datetime.min.time())


def _build_report_range(message: str, nm: str, now: datetime, default: str = "month") -> dict:
    date_mentions = _extract_date_mentions(message, now)
    today = now.date()

    if len(date_mentions) >= 2:
        s, e = sorted(date_mentions[:2])
        start_vn = datetime.combine(s, datetime.min.time())
        end_vn   = datetime.combine(e, datetime.min.time()) + timedelta(days=1)
        label = f"Từ {s.strftime('%d/%m/%Y')} đến {e.strftime('%d/%m/%Y')}"
    elif len(date_mentions) == 1:
        d = date_mentions[0]
        start_vn = datetime.combine(d, datetime.min.time())
        end_vn   = start_vn + timedelta(days=1)
        label = f"Ngày {d.strftime('%d/%m/%Y')}"
    elif "hom qua" in nm:
        start_vn = datetime.combine(today - timedelta(days=1), datetime.min.time())
        end_vn   = datetime.combine(today, datetime.min.time())
        label = "Hôm qua"
    elif "hom nay" in nm:
        start_vn = datetime.combine(today, datetime.min.time())
        end_vn   = start_vn + timedelta(days=1)
        label = "Hôm nay"
    elif "tuan truoc" in nm:
        this_week = _start_of_week(today)
        start_vn = this_week - timedelta(days=7)
        end_vn   = this_week
        label = "Tuần trước"
    elif "tuan nay" in nm:
        start_vn = _start_of_week(today)
        end_vn   = start_vn + timedelta(days=7)
        label = "Tuần này"
    elif "thang truoc" in nm:
        first = datetime(today.year, today.month, 1)
        start_vn = first - timedelta(days=1)
        start_vn = datetime(start_vn.year, start_vn.month, 1)
        end_vn   = first
        label = "Tháng trước"
    elif "thang nay" in nm:
        start_vn = datetime(today.year, today.month, 1)
        end_vn   = datetime(today.year + (today.month // 12), (today.month % 12) + 1, 1)
        label = "Tháng này"
    else:
        m = re.search(r"\b(?P<n>\d{1,2})\s*ngay\b", nm)
        if m:
            days = max(1, min(90, int(m.group("n"))))
            start_vn = datetime.combine(today - timedelta(days=days - 1), datetime.min.time())
            end_vn   = datetime.combine(today + timedelta(days=1), datetime.min.time())
            label = f"{days} ngày gần đây"
        elif default == "day":
            start_vn = datetime.combine(today, datetime.min.time())
            end_vn   = start_vn + timedelta(days=1)
            label = "Hôm nay"
        elif default == "week":
            start_vn = datetime.combine(today - timedelta(days=6), datetime.min.time())
            end_vn   = datetime.combine(today + timedelta(days=1), datetime.min.time())
            label = "7 ngày gần đây"
        else:
            start_vn = datetime(today.year, today.month, 1)
            end_vn   = datetime(today.year + (today.month // 12), (today.month % 12) + 1, 1)
            label = "Tháng này"

    return {
        "start_vn": start_vn, "end_vn": end_vn,
        "start_utc": _vn_to_utc(start_vn), "end_utc": _vn_to_utc(end_vn),
        "label": label,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Intent detectors (admin-specific)
# ─────────────────────────────────────────────────────────────────────────────
def _is_admin_payment_q(nm: str) -> bool:
    return any(kw in nm for kw in ["thanh toan", "payos", "giao dich", "transaction",
                                    "cho thanh toan", "that bai", "loi", "pending", "failed"])

def _is_admin_showtime_q(nm: str) -> bool:
    return any(kw in nm for kw in ["suat chieu", "lich chieu", "gio chieu", "lich hom nay", "lich ngay"])

def _is_delete_showtime_q(nm: str) -> bool:
    delete_verb = any(kw in nm for kw in ["xoa", "xóa", "delete", "huy het", "xoa het", "xoa tat ca"])
    showtime_kw = any(kw in nm for kw in ["suat chieu", "suat", "lich chieu"])
    return delete_verb and showtime_kw

def _is_admin_movie_q(nm: str) -> bool:
    return any(kw in nm for kw in ["phim", "top phim", "dang chieu", "sap chieu"])

def _is_admin_room_q(nm: str) -> bool:
    return any(kw in nm for kw in ["phong", "ghe", "bao tri", "tam dong", "lap day"])

def _is_admin_food_q(nm: str) -> bool:
    return any(kw in nm for kw in ["do an", "do uong", "combo", "bap", "nuoc", "menu"])

def _is_admin_staff_q(nm: str) -> bool:
    return any(kw in nm for kw in [
        "nhan vien", "staff", "nhan su",
        "quan ly ca", "vai tro", "len lam", "nang cap chuc", "thang chuc",
        "xuong lam", "ha chuc", "giang chuc", "sua vai tro", "cap nhat vai tro",
    ])

def _is_admin_customer_q(nm: str) -> bool:
    return any(kw in nm for kw in ["khach hang", "khach", "nguoi dung", "user", "tai khoan"])

def _is_admin_chatbot_training_q(nm: str) -> bool:
    return any(kw in nm for kw in ["chatbot", "huan luyen", "knowledge", "kien thuc", "train"])

def _is_admin_attendance_q(nm: str) -> bool:
    return any(kw in nm for kw in ["cham cong", "check in", "check out", "ca lam", "gio lam", "tien cong"])

def _is_admin_counter_sale_q(nm: str) -> bool:
    return any(kw in nm for kw in ["ban tai quay", "quay ban", "ban truc tiep", "thu ngan", "counter"])

def _is_admin_project_data_q(nm: str) -> bool:
    return any(kw in nm for kw in ["du lieu", "data", "database", "tong quan he thong", "ban do du lieu"])


# ─────────────────────────────────────────────────────────────────────────────
#  Sub-reply builders
# ─────────────────────────────────────────────────────────────────────────────
def _dashboard(message: str, nm: str, now: datetime) -> dict:
    rng = _build_report_range(message, nm, now, "month")
    today_start = _vn_to_utc(datetime.combine(now.date(), datetime.min.time()))
    today_end   = today_start + timedelta(days=1)
    week_end    = _vn_to_utc(datetime.combine(now.date() + timedelta(days=8), datetime.min.time()))

    sql_revenue = """
        SELECT COUNT(b.Id) AS cnt,
               ISNULL(SUM(b.GrandTotal), 0) AS revenue,
               ISNULL(SUM(bs_count.seat_cnt), 0) AS tickets
        FROM Bookings b
        LEFT JOIN (
            SELECT BookingId, COUNT(*) AS seat_cnt FROM BookingSeats GROUP BY BookingId
        ) bs_count ON bs_count.BookingId = b.Id
        WHERE b.PaymentStatus IN ('Paid','CheckedIn')
          AND b.CreatedAt >= ? AND b.CreatedAt < ?
    """
    sql_new_users = "SELECT COUNT(*) FROM Users WHERE CreatedAt >= ? AND CreatedAt < ?"
    sql_showtimes_today = "SELECT COUNT(*) FROM Showtimes WHERE StartTime >= ? AND StartTime < ?"
    sql_upcoming = "SELECT COUNT(*) FROM Showtimes WHERE StartTime >= ? AND StartTime < ?"
    sql_pending   = "SELECT COUNT(*) FROM Bookings WHERE PaymentStatus IN ('PendingPayment','PendingSelect')"
    sql_failed_today = """SELECT COUNT(*) FROM Bookings
                          WHERE PaymentStatus = 'Failed' AND CreatedAt >= ? AND CreatedAt < ?"""
    sql_top_movies = """
        SELECT TOP 3 m.Title, COUNT(bs.Id) AS tickets, ISNULL(SUM(b.GrandTotal),0) AS revenue
        FROM Bookings b
        INNER JOIN Showtimes s ON s.Id = b.ShowtimeId
        INNER JOIN Movies m ON m.Id = s.MovieId
        LEFT  JOIN BookingSeats bs ON bs.BookingId = b.Id
        WHERE b.PaymentStatus IN ('Paid','CheckedIn')
          AND b.CreatedAt >= ? AND b.CreatedAt < ?
        GROUP BY m.Title
        ORDER BY revenue DESC, tickets DESC
    """

    try:
        with get_conn() as conn:
            c = conn.cursor()

            c.execute(sql_revenue, today_start, today_end)
            row = c.fetchone(); rev_today = float(row.revenue or 0); tkt_today = int(row.tickets or 0)

            c.execute(sql_revenue, rng["start_utc"], rng["end_utc"])
            row = c.fetchone(); rev_rng = float(row.revenue or 0); tkt_rng = int(row.tickets or 0)

            c.execute(sql_new_users, today_start, today_end)
            new_cust_today = c.fetchone()[0]

            c.execute(sql_new_users, rng["start_utc"], rng["end_utc"])
            new_cust_rng = c.fetchone()[0]

            now_local = now
            c.execute(sql_showtimes_today, now_local.date(), now_local.date() + timedelta(days=1))
            shows_today = c.fetchone()[0]

            c.execute(sql_upcoming, now_local, datetime.combine(now_local.date() + timedelta(days=8), datetime.min.time()))
            upcoming = c.fetchone()[0]

            c.execute(sql_pending)
            pending = c.fetchone()[0]

            c.execute(sql_failed_today, today_start, today_end)
            failed_today = c.fetchone()[0]

            c.execute(sql_top_movies, rng["start_utc"], rng["end_utc"])
            top_movies = c.fetchall()

        lines = [
            f"Tổng quan admin {now.strftime('%d/%m/%Y %H:%M')}:",
            f"- Hôm nay: {_fmt_money(rev_today)}, {tkt_today} vé đã bán, {new_cust_today} khách mới, {shows_today} suất chiếu.",
            f"- {rng['label']}: {_fmt_money(rev_rng)}, {tkt_rng} vé, {new_cust_rng} khách mới.",
            f"- Vận hành: {upcoming} suất sắp tới trong 7 ngày, {pending} giao dịch đang chờ, {failed_today} giao dịch lỗi hôm nay.",
        ]
        if top_movies:
            lines.append(f"Top phim theo doanh thu {rng['label'].lower()}:")
            for m in top_movies:
                lines.append(f"- {m.Title}: {m.tickets} vé, {_fmt_money(m.revenue)}")

        return {"reply": "\n".join(lines), "actions": _admin_quick_actions()}
    except Exception as e:
        return {"reply": f"Lỗi tải dashboard: {e}", "actions": _admin_quick_actions()}


def _payment(message: str, nm: str, now: datetime) -> dict:
    rng = _build_report_range(message, nm, now, "day")
    sql_paid = """
        SELECT COUNT(b.Id) AS cnt, ISNULL(SUM(b.GrandTotal),0) AS revenue,
               ISNULL(SUM(bs.seat_cnt),0) AS tickets
        FROM Bookings b
        LEFT JOIN (SELECT BookingId, COUNT(*) seat_cnt FROM BookingSeats GROUP BY BookingId) bs
               ON bs.BookingId = b.Id
        WHERE b.PaymentStatus IN ('Paid','CheckedIn') AND b.CreatedAt >= ? AND b.CreatedAt < ?
    """
    sql_pending = """
        SELECT TOP 3 b.Id, ISNULL(m.Title,'Phim chưa rõ') AS title, b.GrandTotal
        FROM Bookings b LEFT JOIN Showtimes s ON s.Id=b.ShowtimeId LEFT JOIN Movies m ON m.Id=s.MovieId
        WHERE b.PaymentStatus IN ('PendingPayment','PendingSelect') ORDER BY b.CreatedAt DESC
    """
    sql_failed = """
        SELECT TOP 3 b.Id, ISNULL(m.Title,'Phim chưa rõ') AS title, b.GrandTotal
        FROM Bookings b LEFT JOIN Showtimes s ON s.Id=b.ShowtimeId LEFT JOIN Movies m ON m.Id=s.MovieId
        WHERE b.PaymentStatus='Failed' AND b.CreatedAt >= ? AND b.CreatedAt < ? ORDER BY b.CreatedAt DESC
    """
    sql_pending_count = "SELECT COUNT(*) FROM Bookings WHERE PaymentStatus IN ('PendingPayment','PendingSelect')"
    sql_failed_count  = "SELECT COUNT(*) FROM Bookings WHERE PaymentStatus='Failed' AND CreatedAt >= ? AND CreatedAt < ?"

    try:
        with get_conn() as conn:
            c = conn.cursor()
            c.execute(sql_paid, rng["start_utc"], rng["end_utc"])
            row = c.fetchone(); paid_cnt = row.cnt; rev = float(row.revenue or 0); tkts = int(row.tickets or 0)

            c.execute(sql_pending_count); pending_cnt = c.fetchone()[0]
            c.execute(sql_failed_count, rng["start_utc"], rng["end_utc"]); failed_cnt = c.fetchone()[0]

            c.execute(sql_pending); pending_rows = c.fetchall()
            c.execute(sql_failed, rng["start_utc"], rng["end_utc"]); failed_rows = c.fetchall()

        lines = [
            f"Tình trạng thanh toán {rng['label'].lower()}:",
            f"- Đã thanh toán: {paid_cnt} giao dịch, {tkts} vé, {_fmt_money(rev)}.",
            f"- Đang chờ: {pending_cnt} giao dịch gần nhất.",
            f"- Lỗi/thất bại: {failed_cnt} giao dịch.",
        ]
        for b in pending_rows:
            lines.append(f"- Chờ: BKG-{b.Id:06d} | {b.title} | {_fmt_money(b.GrandTotal)}")
        for b in failed_rows:
            lines.append(f"- Lỗi: BKG-{b.Id:06d} | {b.title} | {_fmt_money(b.GrandTotal)}")

        return {"reply": "\n".join(lines), "actions": [_admin_action("Bảng điều khiển", "Index"), _admin_action("Suất chiếu", "Showtimes")]}
    except Exception as e:
        return {"reply": f"Lỗi tải thanh toán: {e}", "actions": []}


def _showtime(message: str, now: datetime) -> dict:
    requested = extract_requested_date(message, now) or datetime.combine(now.date(), datetime.min.time())
    day_start = requested.replace(hour=0, minute=0, second=0)
    day_end   = day_start + timedelta(days=1)

    sql = """
        SELECT s.Id, s.StartTime, s.BasePrice, ISNULL(m.Title,'Phim chua ro') AS title,
               ISNULL(r.Name,'N/A') AS room_name,
               (r.SeatRows * r.SeatCols) AS total_seats,
               (SELECT COUNT(*) FROM BookingSeats bs
                INNER JOIN Bookings b ON b.Id=bs.BookingId
                WHERE b.ShowtimeId=s.Id AND b.PaymentStatus IN ('Paid','PendingPayment','CheckedIn')
               ) AS sold_seats
        FROM Showtimes s
        LEFT JOIN Movies m ON m.Id=s.MovieId
        LEFT JOIN Rooms  r ON r.Id=s.RoomId
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
                total = int(s.total_seats or 0); sold = int(s.sold_seats or 0)
                room = s.room_name if normalize(s.room_name).startswith("phong") else f"Phòng {s.room_name}"
                lines.append(f"- {s.StartTime.strftime('%H:%M')} | {s.title} | {room} | đã bán/giữ {sold}/{total}")
            if len(rows) > 8:
                lines.append(f"Còn {len(rows)-8} suất khác trong ngày.")

        return {"reply": "\n".join(lines), "actions": [_admin_action("Quản lý suất chiếu", "Showtimes")]}
    except Exception as e:
        return {"reply": f"Lỗi tải suất chiếu: {e}", "actions": []}


def _movies(message: str, nm: str, now: datetime) -> dict:
    rng = _build_report_range(message, nm, now, "month")
    sql = """
        SELECT
          (SELECT COUNT(*) FROM Movies) AS total_movies,
          (SELECT COUNT(DISTINCT MovieId) FROM Showtimes WHERE StartTime >= ?) AS upcoming_count
    """
    sql_top = """
        SELECT TOP 5 m.Title, COUNT(bs.Id) AS tickets, ISNULL(SUM(b.GrandTotal),0) AS revenue
        FROM Bookings b
        INNER JOIN Showtimes s ON s.Id=b.ShowtimeId
        INNER JOIN Movies m ON m.Id=s.MovieId
        LEFT  JOIN BookingSeats bs ON bs.BookingId=b.Id
        WHERE b.PaymentStatus IN ('Paid','CheckedIn') AND b.CreatedAt >= ? AND b.CreatedAt < ?
        GROUP BY m.Title ORDER BY revenue DESC, tickets DESC
    """
    sql_no_show = """
        SELECT TOP 5 m.Title FROM Movies m
        WHERE NOT EXISTS (SELECT 1 FROM Showtimes s WHERE s.MovieId=m.Id AND s.StartTime >= ?)
        ORDER BY m.Id DESC
    """
    try:
        with get_conn() as conn:
            c = conn.cursor()
            c.execute(sql, now)
            row = c.fetchone()
            c.execute(sql_top, rng["start_utc"], rng["end_utc"]); top = c.fetchall()
            c.execute(sql_no_show, now); no_show = [r.Title for r in c.fetchall()]

        lines = [
            "Tổng hợp phim:",
            f"- Tổng phim trong hệ thống: {row.total_movies}.",
            f"- Phim có suất sắp tới: {row.upcoming_count}.",
        ]
        if top:
            lines.append(f"Top phim {rng['label'].lower()}:")
            for m in top:
                lines.append(f"- {m.Title}: {m.tickets} vé, {_fmt_money(m.revenue)}")
        if no_show:
            lines.append("Phim chưa có suất sắp tới: " + ", ".join(no_show) + ".")

        return {"reply": "\n".join(lines), "actions": [_admin_action("Quản lý phim", "Movies"), _admin_action("Tạo suất chiếu", "Showtimes")]}
    except Exception as e:
        return {"reply": f"Lỗi tải phim: {e}", "actions": []}


def _rooms(now: datetime) -> dict:
    today_start = now.date()
    today_end   = today_start + timedelta(days=1)
    sql_rooms = """
        SELECT r.Id, r.Name,
               ISNULL(r.Status, N'Sẵn sàng') AS status,
               (SELECT COUNT(*) FROM Seats s WHERE s.RoomId=r.Id AND s.IsActive=1) AS active_seats,
               (SELECT COUNT(*) FROM Showtimes st WHERE st.RoomId=r.Id AND st.StartTime >= ? AND st.StartTime < ?) AS shows_today
        FROM Rooms r ORDER BY r.Name
    """
    sql_busy = """
        SELECT TOP 3 s.Id, s.StartTime, ISNULL(m.Title,'Phim chua ro') AS title,
               ISNULL(r.Name,'N/A') AS room_name,
               (r.SeatRows * r.SeatCols) AS total,
               (SELECT COUNT(*) FROM BookingSeats bs
                INNER JOIN Bookings b ON b.Id=bs.BookingId
                WHERE b.ShowtimeId=s.Id AND b.PaymentStatus IN ('Paid','PendingPayment','CheckedIn')
               ) AS sold
        FROM Showtimes s
        LEFT JOIN Movies m ON m.Id=s.MovieId
        LEFT JOIN Rooms  r ON r.Id=s.RoomId
        WHERE s.StartTime >= ? AND s.StartTime < ?
        ORDER BY sold DESC
    """
    try:
        with get_conn() as conn:
            c = conn.cursor()
            c.execute(sql_rooms, today_start, today_end); rooms = c.fetchall()
            c.execute(sql_busy,  today_start, today_end); busy  = c.fetchall()

        lines = ["Tổng hợp phòng chiếu:"]
        for r in rooms:
            name = r.Name if normalize(r.Name).startswith("phong") else f"Phòng {r.Name}"
            lines.append(f"- {name}: {r.status}, {r.active_seats} ghế hoạt động, {r.shows_today} suất hôm nay.")
        if busy:
            lines.append("Suất lấp đầy cao hôm nay:")
            for s in busy:
                total = int(s.total or 0); sold = int(s.sold or 0); avail = total - sold
                room = s.room_name if normalize(s.room_name).startswith("phong") else f"Phòng {s.room_name}"
                lines.append(f"- {s.StartTime.strftime('%H:%M')} {room} | {s.title} | còn {avail}/{total} ghế.")

        return {"reply": "\n".join(lines), "actions": [_admin_action("Quản lý phòng", "Rooms")]}
    except Exception as e:
        return {"reply": f"Lỗi tải phòng: {e}", "actions": []}


def _food(message: str, nm: str, now: datetime) -> dict:
    rng = _build_report_range(message, nm, now, "month")
    sql_menu = "SELECT Category FROM FoodAndDrinks ORDER BY Category"
    sql_top  = """
        SELECT TOP 5 ISNULL(f.Name,'Món #'+CAST(bc.FoodAndDrinkId AS NVARCHAR)) AS name,
               SUM(bc.Quantity) AS qty, SUM(bc.Quantity * bc.UnitPrice) AS revenue
        FROM BookingConcessions bc
        LEFT JOIN FoodAndDrinks f ON f.Id=bc.FoodAndDrinkId
        INNER JOIN Bookings b ON b.Id=bc.BookingId
        WHERE b.PaymentStatus IN ('Paid','CheckedIn') AND b.CreatedAt >= ? AND b.CreatedAt < ?
        GROUP BY bc.FoodAndDrinkId, f.Name ORDER BY revenue DESC
    """
    sql_total = "SELECT COUNT(*) FROM FoodAndDrinks"
    try:
        with get_conn() as conn:
            c = conn.cursor()
            c.execute(sql_total); total = c.fetchone()[0]
            c.execute(sql_menu);  cats = {}
            for r in c.fetchall():
                cats[r.Category or "Khác"] = cats.get(r.Category or "Khác", 0) + 1
            c.execute(sql_top, rng["start_utc"], rng["end_utc"]); top = c.fetchall()

        lines = [f"Tổng hợp đồ ăn & thức uống:", f"- Menu hiện có: {total} món/combo."]
        for cat, count in cats.items():
            lines.append(f"- {cat}: {count} món.")
        if top:
            lines.append(f"Top bán {rng['label'].lower()}:")
            for item in top:
                lines.append(f"- {item.name}: {item.qty} phần, {_fmt_money(item.revenue)}")

        return {"reply": "\n".join(lines), "actions": [_admin_action("Quản lý đồ ăn", "FoodAndDrinks")]}
    except Exception as e:
        return {"reply": f"Lỗi tải đồ ăn: {e}", "actions": []}


_ROLE_CHANGE_KW = [
    "len lam", "xuong lam", "ha chuc", "giang chuc", "nang cap", "thang chuc",
    "doi vai tro", "giao chuc vu", "chuyen sang vai tro", "chuyen sang",
    "cap nhat vai tro", "sua vai tro", "sua nhan vien", "thanh nhan vien",
    "thanh quan ly",
]

# Map từ keyword (dạng đã normalize) → tên vai trò lưu trong DB
_ROLE_MAP = {
    "quan ly ca":       "Quản lý ca",
    "quan ly":          "Quản lý ca",
    "nhan vien ban ve": "Nhân viên bán vé",
    "nhan vien":        "Nhân viên bán vé",
}


def _detect_target_staff_role(nm: str) -> Optional[str]:
    # Ưu tiên vai trò nằm sau từ khóa chuyển đổi để tránh nhầm vai trò hiện tại.
    transition_patterns = [
        r"(?:len\s+lam|xuong\s+lam|ha\s+chuc\s+(?:xuong\s+)?|giang\s+chuc\s+(?:xuong\s+)?|"
        r"doi\s+vai\s+tro\s+(?:thanh\s+|sang\s+)?|chuyen\s+sang(?:\s+vai\s+tro)?|"
        r"giao\s+chuc\s+vu|cap\s+nhat\s+vai\s+tro\s+(?:thanh\s+|sang\s+)?|"
        r"sua\s+vai\s+tro\s+(?:thanh\s+|sang\s+)?|thanh)\s+(.+)$",
    ]
    for pattern in transition_patterns:
        match = re.search(pattern, nm)
        if not match:
            continue
        target_text = match.group(1)
        for kw, role in _ROLE_MAP.items():
            if kw in target_text:
                return role

    if any(kw in nm for kw in ["xuong lam", "ha chuc", "giang chuc"]):
        return "Nhân viên bán vé"
    if any(kw in nm for kw in ["len lam", "nang cap", "thang chuc"]):
        return "Quản lý ca"

    for kw, role in _ROLE_MAP.items():
        if kw in nm:
            return role
    return None


def _staff(message: str, nm: str) -> dict:
    # Nếu người dùng muốn thay đổi vai trò → chuyển sang _staff_set_role
    if any(kw in nm for kw in _ROLE_CHANGE_KW):
        return _staff_set_role(message, nm)

    sql = "SELECT Id, Role, FullName FROM Staff ORDER BY Role, FullName"
    try:
        with get_conn() as conn:
            c = conn.cursor()
            c.execute(sql)
            rows = c.fetchall()

        from collections import Counter
        role_counts = Counter(r.Role or "Chưa rõ vai trò" for r in rows)
        recent = [f"{r.FullName} (ID:{r.Id}, {r.Role or '—'})" for r in rows[-5:]]

        lines = ["Tổng hợp nhân viên:", f"- Tổng: {len(rows)} người."]
        for role, cnt in sorted(role_counts.items()):
            lines.append(f"- {role}: {cnt} người.")
        if recent:
            lines.append("Danh sách gần đây: " + ", ".join(recent) + ".")
        lines.append("(Để thay đổi vai trò: 'cho nhân viên ID X lên làm quản lý ca')")

        return {
            "reply": "\n".join(lines),
            "actions": [_admin_action("Quản lý nhân viên", "Staff"), _admin_action("Chấm công", "Attendance")],
        }
    except Exception as e:
        return {"reply": f"Lỗi tải nhân viên: {e}", "actions": []}


def _staff_set_role(message: str, nm: str) -> dict:
    # 1. Xác định vai trò mới
    new_role = _detect_target_staff_role(nm)
    if new_role is None:
        return {
            "reply": "Vui lòng cho biết vai trò muốn gán.\nVí dụ: 'Quản lý ca' hoặc 'Nhân viên bán vé'.",
            "actions": [],
        }

    # 2. Tìm nhân viên theo ID hoặc tên
    id_match   = re.search(r'\bid\s*[=:]?\s*(\d+)', message, re.IGNORECASE)
    # Tìm tên sau từ "tên" / "ten" / "có tên"
    name_match = re.search(r'(?:co\s+)?ten\s+(\S+)', nm)

    try:
        with get_conn() as conn:
            c = conn.cursor()

            if id_match:
                staff_id = int(id_match.group(1))
                c.execute("SELECT Id, FullName, Role FROM Staff WHERE Id = ?", staff_id)
                staff = c.fetchone()
                if staff is None:
                    return {"reply": f"Không tìm thấy nhân viên với ID {staff_id}.", "actions": []}

            elif name_match:
                keyword = name_match.group(1).strip()
                c.execute("SELECT Id, FullName, Role FROM Staff WHERE FullName LIKE ?", f"%{keyword}%")
                rows = c.fetchall()
                if not rows:
                    return {"reply": f"Không tìm thấy nhân viên có tên chứa '{keyword}'.", "actions": []}
                if len(rows) > 1:
                    names = ", ".join(f"{r.FullName} (ID:{r.Id})" for r in rows)
                    return {
                        "reply": f"Tìm thấy {len(rows)} nhân viên: {names}.\nVui lòng chỉ định thêm ID để chính xác hơn.",
                        "actions": [],
                    }
                staff = rows[0]

            else:
                return {
                    "reply": "Vui lòng cung cấp ID hoặc tên nhân viên.\nVí dụ: 'cho nhân viên ID 6 lên làm quản lý ca'.",
                    "actions": [],
                }

            # 3. Cập nhật vai trò
            old_role = staff.Role or "Chưa rõ"
            c.execute("UPDATE Staff SET Role = ? WHERE Id = ?", new_role, staff.Id)
            conn.commit()

        return {
            "reply": (
                f"✅ Đã cập nhật vai trò thành công!\n"
                f"- Nhân viên: {staff.FullName} (ID: {staff.Id})\n"
                f"- Vai trò cũ: {old_role}\n"
                f"- Vai trò mới: {new_role}"
            ),
            "actions": [_admin_action("Quản lý nhân viên", "Staff")],
        }
    except Exception as e:
        return {"reply": f"Lỗi cập nhật vai trò: {e}", "actions": []}


def _customers(message: str, nm: str, now: datetime) -> dict:
    rng = _build_report_range(message, nm, now, "month")
    today_start = _vn_to_utc(datetime.combine(now.date(), datetime.min.time()))
    today_end   = today_start + timedelta(days=1)
    sql = """
        SELECT COUNT(*) AS total,
               (SELECT COUNT(*) FROM Users WHERE CreatedAt >= ? AND CreatedAt < ?) AS today_new,
               (SELECT COUNT(*) FROM Users WHERE CreatedAt >= ? AND CreatedAt < ?) AS range_new
        FROM Users
    """
    sql_recent = "SELECT TOP 5 FullName, Email, CreatedAt FROM Users ORDER BY CreatedAt DESC"
    try:
        with get_conn() as conn:
            c = conn.cursor()
            c.execute(sql, today_start, today_end, rng["start_utc"], rng["end_utc"])
            row = c.fetchone()
            c.execute(sql_recent); recent = c.fetchall()

        lines = [
            "Tổng hợp khách hàng:",
            f"- Tổng tài khoản khách: {row.total}.",
            f"- Khách mới hôm nay: {row.today_new}; {rng['label'].lower()}: {row.range_new}.",
        ]
        if recent:
            lines.append("Khách mới gần đây:")
            for u in recent:
                lines.append(f"- {u.FullName} | {u.Email} | {u.CreatedAt.strftime('%d/%m %H:%M')}.")

        return {"reply": "\n".join(lines), "actions": [_admin_action("Bảng điều khiển", "Index")]}
    except Exception as e:
        return {"reply": f"Lỗi tải khách hàng: {e}", "actions": []}


def _counter_sales(message: str, nm: str, now: datetime) -> dict:
    rng = _build_report_range(message, nm, now, "day")
    sql = """
        SELECT COUNT(*) AS cnt,
               ISNULL(SUM(b.GrandTotal),0) AS total_rev,
               ISNULL(SUM(b.TotalSeatPrice),0) AS seat_rev,
               ISNULL(SUM(b.TotalConcessionPrice),0) AS food_rev,
               ISNULL(SUM(bs.seat_cnt),0) AS tickets,
               ISNULL(SUM(bc.food_qty),0) AS concessions
        FROM Bookings b
        LEFT JOIN (SELECT BookingId, COUNT(*) seat_cnt FROM BookingSeats GROUP BY BookingId) bs ON bs.BookingId=b.Id
        LEFT JOIN (SELECT BookingId, SUM(Quantity) food_qty FROM BookingConcessions GROUP BY BookingId) bc ON bc.BookingId=b.Id
        WHERE (b.UserId IS NULL OR b.UserId=0)
          AND b.PaymentStatus IN ('Paid','CheckedIn')
          AND b.CreatedAt >= ? AND b.CreatedAt < ?
    """
    sql_top = """
        SELECT TOP 5 ISNULL(m.Title,'Phim chưa rõ') AS title,
               COUNT(bs.Id) AS tickets, ISNULL(SUM(b.GrandTotal),0) AS revenue
        FROM Bookings b
        INNER JOIN Showtimes s ON s.Id=b.ShowtimeId
        INNER JOIN Movies m ON m.Id=s.MovieId
        LEFT  JOIN BookingSeats bs ON bs.BookingId=b.Id
        WHERE (b.UserId IS NULL OR b.UserId=0)
          AND b.PaymentStatus IN ('Paid','CheckedIn')
          AND b.CreatedAt >= ? AND b.CreatedAt < ?
        GROUP BY m.Title ORDER BY revenue DESC
    """
    try:
        with get_conn() as conn:
            c = conn.cursor()
            c.execute(sql, rng["start_utc"], rng["end_utc"]); row = c.fetchone()
            c.execute(sql_top, rng["start_utc"], rng["end_utc"]); top = c.fetchall()

        lines = [
            f"Bán vé tại quầy {rng['label'].lower()}:",
            f"- {row.cnt} hóa đơn, {int(row.tickets)} vé, {int(row.concessions)} phần đồ ăn/nước.",
            f"- Doanh thu vé: {_fmt_money(row.seat_rev)}; đồ ăn/nước: {_fmt_money(row.food_rev)}; tổng: {_fmt_money(row.total_rev)}.",
        ]
        if top:
            lines.append("Top phim bán tại quầy:")
            for m in top:
                lines.append(f"- {m.title}: {m.tickets} vé, {_fmt_money(m.revenue)}")

        return {"reply": "\n".join(lines), "actions": [{"type": "open_url", "label": "Bán vé tại quầy", "url": "/Staff/Staff/Sales"}]}
    except Exception as e:
        return {"reply": f"Lỗi tải dữ liệu quầy: {e}", "actions": []}


def _chatbot_training() -> dict:
    sql_active = "SELECT COUNT(*) FROM ChatbotKnowledge WHERE IsActive=1"
    sql_total  = "SELECT COUNT(*) FROM ChatbotKnowledge"
    try:
        with get_conn() as conn:
            c = conn.cursor()
            c.execute(sql_active); active = c.fetchone()[0]
            c.execute(sql_total);  total  = c.fetchone()[0]
    except Exception:
        active = total = 0

    lines = [
        "Huấn luyện chatbot:",
        f"- Đang có {active}/{total} mục kiến thức đang bật.",
        "- Nên thêm FAQ theo nhóm: chính sách vé, khuyến mãi, quy định rạp, hướng dẫn thanh toán, hỗ trợ lỗi thường gặp.",
        "- Với dữ liệu lịch chiếu, ghế, vé, doanh thu: bot đọc trực tiếp từ database nên không cần nhập tay.",
    ]
    return {"reply": "\n".join(lines), "actions": [_admin_action("Huấn luyện Chatbot", "ChatbotKnowledge")]}


def _project_data(now: datetime) -> dict:
    sql = """
        SELECT
          (SELECT COUNT(*) FROM Movies)       AS movies,
          (SELECT COUNT(*) FROM Showtimes)    AS showtimes,
          (SELECT COUNT(*) FROM Rooms)        AS rooms,
          (SELECT COUNT(*) FROM Seats)        AS seats,
          (SELECT COUNT(*) FROM Bookings)     AS bookings,
          (SELECT COUNT(*) FROM BookingSeats) AS booking_seats,
          (SELECT COUNT(*) FROM BookingConcessions) AS booking_concs,
          (SELECT COUNT(*) FROM Users)        AS users,
          (SELECT COUNT(*) FROM Staff)        AS staffs,
          (SELECT COUNT(*) FROM FoodAndDrinks) AS foods
    """
    try:
        with get_conn() as conn:
            c = conn.cursor()
            c.execute(sql); row = c.fetchone()
            try:
                c.execute("SELECT COUNT(*) FROM ChatbotKnowledge"); kb = c.fetchone()[0]
            except Exception:
                kb = 0

        lines = [
            "Bản đồ dữ liệu chatbot đang hiểu:",
            f"- Phim/suất/phòng/ghế: {row.movies} phim, {row.showtimes} suất, {row.rooms} phòng, {row.seats} ghế.",
            f"- Vé/thanh toán: {row.bookings} booking, {row.booking_seats} ghế ghi nhận, {row.booking_concs} dòng đồ ăn.",
            f"- Khách/nhân viên: {row.users} khách hàng, {row.staffs} nhân viên.",
            f"- Đồ ăn & chatbot knowledge: {row.foods} món/combo, {kb} mục FAQ/knowledge.",
            "- Các thao tác ghi/xóa dữ liệu thực hiện qua nút quản lý; chatbot chỉ tổng hợp và điều hướng.",
        ]
        return {"reply": "\n".join(lines), "actions": _admin_quick_actions()}
    except Exception as e:
        return {"reply": f"Lỗi tải dữ liệu: {e}", "actions": []}


# ─────────────────────────────────────────────────────────────────────────────
#  Showtime creation helpers
# ─────────────────────────────────────────────────────────────────────────────
_MOVIE_STOP_WORDS = {"phim", "dien", "anh", "tham", "lung", "danh", "cua",
                     "nhung", "mot", "voi", "cho", "the", "and", "movie"}


def _resolve_movie_id(nm: str) -> Optional[int]:
    """Port of ResolveRequestedMovieId — fuzzy title match."""
    try:
        with get_conn() as conn:
            c = conn.cursor()
            c.execute("SELECT Id, Title FROM Movies")
            movies = c.fetchall()
    except Exception:
        return None

    best_score = 0
    best_id = None
    for movie in movies:
        title = normalize(movie.Title or "")
        if not title:
            continue
        score = 100 if title in nm else 0
        tokens = [t for t in re.split(r"[^a-z0-9]+", title)
                  if len(t) >= 3 and t not in _MOVIE_STOP_WORDS]
        for token in set(tokens):
            if token in nm:
                score += 3 if len(token) >= 5 else 1
        if score > best_score:
            best_score = score
            best_id = movie.Id

    return best_id if best_score >= 3 else None


def _resolve_room(nm: str) -> Optional[dict]:
    """Port of ResolveRequestedRoom — find room by name keyword."""
    try:
        with get_conn() as conn:
            c = conn.cursor()
            c.execute("SELECT Id, Name, ISNULL(Status, N'Sẵn sàng') AS Status FROM Rooms")
            rooms = c.fetchall()
    except Exception:
        return None

    if not rooms:
        return None

    m = re.search(r"\b(?:phong|room)\s*(?P<room>[a-z0-9]+)\b", nm)
    requested = m.group("room") if m else None

    if requested:
        for r in rooms:
            rn = normalize(r.Name or "")
            token = re.sub(r"\b(phong|room)\b", "", rn).strip()
            if (token == requested
                    or rn == f"phong {requested}"
                    or rn == f"room {requested}"):
                return {"id": r.Id, "name": r.Name, "status": r.Status}

    for r in rooms:
        rn = normalize(r.Name or "")
        if rn and rn in nm:
            return {"id": r.Id, "name": r.Name, "status": r.Status}

    return None


def _extract_price(nm: str) -> Optional[float]:
    """Port of ExtractRequestedPrice."""
    m = re.search(
        r"\b(?:gia|gia ve|price)\s*(?P<value>\d{1,3}(?:[.,]\d{3})+|\d+)\s*(?P<unit>k|nghin|ngan)?\b", nm)
    if not m:
        m = re.search(r"\b(?P<value>\d{1,3}(?:[.,]\d{3})+|\d+)\s*(?P<unit>k|nghin|ngan)\b", nm)
    if not m:
        return None
    raw = m.group("value").replace(".", "").replace(",", "")
    try:
        value = float(raw)
    except ValueError:
        return None
    unit = (m.group("unit") or "").lower()
    if unit in ("k", "nghin", "ngan"):
        value *= 1000
    return value if value >= 0 else None


def _extract_time(message: str) -> Optional[tuple]:
    """Port of ExtractRequestedTime — returns (hour, minute_or_None) or None."""
    nm = normalize(message)
    for m in re.finditer(
            r"(?<!\d)(?P<h>[01]?\d|2[0-3])\s*(?P<sep>:|h|gio)\s*(?P<m>[0-5]?\d)?(?!\d)", nm):
        try:
            hour = int(m.group("h"))
        except (ValueError, TypeError):
            continue
        minute = None
        if m.group("m"):
            try:
                minute = int(m.group("m"))
            except ValueError:
                pass
        return (hour, minute)
    return None


def _is_create_showtime_q(nm: str) -> bool:
    create_verb = any(kw in nm for kw in ["tao", "them", "add", "create"])
    return create_verb and any(kw in nm for kw in ["suat chieu", "suat", "lich chieu", "gio chieu", "suat moi"])


def _is_bulk_create_q(nm: str) -> bool:
    if not _is_create_showtime_q(nm):
        return False
    # Nếu user đã ghi giờ cụ thể (HH:mm / HHh / HH gio) → đây là single showtime, không phải bulk
    has_specific_time = bool(re.search(
        r"(?<!\d)(?:[01]?\d|2[0-3])\s*(?::|h|gio)\s*(?:[0-5]\d)?(?!\d)", nm))
    if has_specific_time:
        return False
    return (any(kw in nm for kw in ["tat ca phim", "cac phim", "hang loat", "bulk", "moi ngay"])
            or bool(re.search(r"\b\d{1,2}\s*ngay\s*(?:toi|tiep theo|sap toi|ke tiep)\b", nm))
            or bool(re.search(r"\b\d{1,2}\s*suat(?:\s*chieu)?\b", nm)))


def _is_all_now_showing_q(nm: str) -> bool:
    # "phim bất kỳ" / "phim nào cũng được" → treat as all now-showing
    if any(kw in nm for kw in ["phim bat ky", "bat ky phim", "phim nao cung", "phim gi cung"]):
        return True
    mentions_all = any(kw in nm for kw in ["tat ca", "cac phim", "moi phim", "tung phim"])
    return (mentions_all and "phim" in nm
            and any(kw in nm for kw in ["dang chieu", "hien dang chieu", "now showing"]))


def _extract_bulk_days(nm: str) -> int:
    m = re.search(r"\b(?P<days>\d{1,2})\s*ngay\s*(?:toi|tiep theo|sap toi|ke tiep)\b", nm)
    if m:
        return max(1, min(int(m.group("days")), 14))
    return 1


def _extract_bulk_per_day(nm: str) -> Optional[int]:
    patterns = [
        r"\bmoi ngay\b.*?\b(?P<count>\d{1,2})\s*suat",
        r"\b(?P<count>\d{1,2})\s*suat\s*/\s*ngay\b",
        r"\b(?P<count>\d{1,2})\s*suat\s*mot ngay\b",
        r"\b(?P<count>\d{1,2})\s*suat\s*moi ngay\b",
    ]
    for pattern in patterns:
        m = re.search(pattern, nm)
        if m:
            return max(1, min(int(m.group("count")), 20))
    return None


def _extract_bulk_total(nm: str) -> Optional[int]:
    m = re.search(r"\b(?:tao|them|add|create)?\s*(?P<count>\d{1,2})\s*suat(?:\s*chieu)?\b", nm)
    if m:
        return max(1, min(int(m.group("count")), 60))
    return None


def _resolve_bulk_start(message: str, nm: str, now: datetime):
    requested = extract_requested_date(message, now)
    if requested is not None:
        return requested.date() if hasattr(requested, "date") else requested
    if "tiep theo" in nm and "hom nay" not in nm:
        return (now + timedelta(days=1)).date()
    return now.date()


def _load_now_showing_movies(now: datetime) -> list:
    today = now.date()
    sql = "SELECT Id, Title, DurationMinutes FROM Movies WHERE ReleaseDate IS NULL OR ReleaseDate <= ? ORDER BY Title"
    try:
        with get_conn() as conn:
            c = conn.cursor()
            c.execute(sql, today)
            return [{"id": r.Id, "title": r.Title, "duration": int(r.DurationMinutes or 0)}
                    for r in c.fetchall()]
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────────────────────
#  Bulk scheduling algorithm
# ─────────────────────────────────────────────────────────────────────────────

# In-memory pending plan storage keyed by user_id (int, 0 = anonymous)
_pending_plans: dict = {}
_PLAN_EXPIRE_MINUTES = 15


def _plan_key(user_id) -> int:
    return int(user_id) if user_id else 0


def _get_pending_plan(user_id) -> Optional[dict]:
    key = _plan_key(user_id)
    plan = _pending_plans.get(key)
    if plan is None:
        return None
    if datetime.utcnow() - plan["created_at"] > timedelta(minutes=_PLAN_EXPIRE_MINUTES):
        _pending_plans.pop(key, None)
        return None
    return plan


def _set_pending_plan(user_id, items: list, price_note: str, start, end):
    _pending_plans[_plan_key(user_id)] = {
        "items": items, "price_note": price_note,
        "start": start, "end": end,
        "created_at": datetime.utcnow(),
    }


def _clear_pending_plan(user_id):
    _pending_plans.pop(_plan_key(user_id), None)


def _load_schedule_slots(start: datetime, end: datetime) -> list:
    sql = """
        SELECT s.StartTime, ISNULL(m.DurationMinutes,0) AS dur,
               s.MovieId, s.RoomId,
               ISNULL(m.Title,'Phim chưa rõ') AS movie_title,
               ISNULL(r.Name,'Phòng chưa rõ') AS room_name,
               s.BasePrice
        FROM Showtimes s
        LEFT JOIN Movies m ON m.Id = s.MovieId
        LEFT JOIN Rooms  r ON r.Id = s.RoomId
        WHERE s.StartTime >= ? AND s.StartTime < ?
    """
    try:
        with get_conn() as conn:
            c = conn.cursor()
            c.execute(sql, start, end)
            return [{"movie_id": r.MovieId, "movie_title": r.movie_title,
                     "room_id": r.RoomId, "room_name": r.room_name,
                     "start_time": r.StartTime, "duration": int(r.dur or 0),
                     "base_price": float(r.BasePrice or 0)}
                    for r in c.fetchall()]
    except Exception:
        return []


def _find_schedule_conflict(room_id: int, new_start: datetime, duration_minutes: int, slots: list) -> Optional[dict]:
    """Port of TryFindScheduleConflict."""
    new_end = new_start + timedelta(minutes=duration_minutes)
    for slot in slots:
        if slot["room_id"] != room_id:
            continue
        existing_end = slot["start_time"] + timedelta(minutes=slot["duration"])
        separated = (new_start >= existing_end + timedelta(minutes=30)
                     or slot["start_time"] >= new_end + timedelta(minutes=30))
        if not separated:
            return slot
    return None


def _find_default_price(movie_id: int) -> float:
    sql = "SELECT TOP 1 BasePrice FROM Showtimes WHERE MovieId = ? ORDER BY StartTime DESC"
    try:
        with get_conn() as conn:
            c = conn.cursor()
            c.execute(sql, movie_id)
            row = c.fetchone()
            return float(row.BasePrice) if row else 70000.0
    except Exception:
        return 70000.0


def _bulk_candidate_times(sequence: int, daily_total: int) -> list:
    """Port of BuildBulkCandidateTimes — returns ordered list of dt_time objects."""
    if daily_total <= 1:
        preferred = [dt_time(19, 0)]
    elif daily_total == 2:
        preferred = [dt_time(13, 0), dt_time(19, 0)]
    elif daily_total == 3:
        preferred = [dt_time(10, 0), dt_time(14, 0), dt_time(19, 0)]
    elif daily_total == 4:
        preferred = [dt_time(9, 30), dt_time(13, 0), dt_time(16, 30), dt_time(20, 0)]
    else:
        preferred = [dt_time(9, 0), dt_time(10, 30), dt_time(12, 0), dt_time(13, 30),
                     dt_time(15, 0), dt_time(16, 30), dt_time(18, 0), dt_time(19, 30),
                     dt_time(21, 0), dt_time(22, 30)]

    offset = sequence % len(preferred)
    ordered = preferred[offset:] + preferred[:offset]

    seen = set(ordered)
    fallback = []
    for h in range(8, 23):
        for mn in (0, 30):
            t = dt_time(h, mn)
            if t not in seen and t <= dt_time(22, 30):
                seen.add(t)
                fallback.append(t)

    return ordered + fallback


def _build_bulk_needs(movies, start_date, days, per_day_count, total_count, per_movie_mode, price) -> list:
    """Port of BuildBulkShowtimeNeeds."""
    needs = []
    movie_cursor = 0

    if per_day_count is not None:
        for day_idx in range(days):
            day = start_date + timedelta(days=day_idx)
            if per_movie_mode:
                for movie in movies:
                    default_price = price if price is not None else _find_default_price(movie["id"])
                    for seq in range(per_day_count):
                        needs.append({"movie_id": movie["id"], "movie_title": movie["title"],
                                      "duration": movie["duration"], "date": day, "seq": seq,
                                      "daily_total": per_day_count, "price": default_price})
            else:
                for seq in range(per_day_count):
                    movie = movies[movie_cursor % len(movies)]
                    movie_cursor += 1
                    default_price = price if price is not None else _find_default_price(movie["id"])
                    needs.append({"movie_id": movie["id"], "movie_title": movie["title"],
                                  "duration": movie["duration"], "date": day, "seq": seq,
                                  "daily_total": per_day_count, "price": default_price})
        return needs

    # total_count mode
    remaining = total_count or 0
    for day_idx in range(days):
        if remaining <= 0:
            break
        days_left = days - day_idx
        slots_today = -(-remaining // days_left)  # ceiling division
        day = start_date + timedelta(days=day_idx)
        for seq in range(slots_today):
            movie = movies[movie_cursor % len(movies)]
            movie_cursor += 1
            default_price = price if price is not None else _find_default_price(movie["id"])
            needs.append({"movie_id": movie["id"], "movie_title": movie["title"],
                          "duration": movie["duration"], "date": day, "seq": seq,
                          "daily_total": slots_today, "price": default_price})
        remaining -= slots_today

    return needs


def _try_schedule(need, rooms, slots, now) -> tuple:
    """Port of TryScheduleBulkShowtime — returns (item_dict, None) or (None, skip_reason)."""
    candidate_times = _bulk_candidate_times(need["seq"], need["daily_total"])
    n = len(rooms)
    offset = need["seq"] % n
    rotated = rooms[offset:] + rooms[:offset]

    for t in candidate_times:
        start_time = datetime.combine(need["date"], t)
        if start_time < now + timedelta(minutes=15):
            continue
        for room in rotated:
            if room.get("status") in ("Bảo trì", "Tạm đóng"):
                continue
            if _find_schedule_conflict(room["id"], start_time, need["duration"], slots) is not None:
                continue
            return ({"movie_id": need["movie_id"], "movie_title": need["movie_title"],
                     "room_id": room["id"], "room_name": room["name"],
                     "start_time": start_time, "duration": need["duration"], "price": need["price"]},
                    None)

    skip = (f"{need['movie_title']} ngày {need['date'].strftime('%d/%m/%Y')}: "
            "không còn khung giờ/phòng trống phù hợp.")
    return None, skip


def _build_bulk_plan(movies, rooms, start_date, days, per_day_count, total_count, per_movie_mode, price, now) -> tuple:
    """Port of BuildBulkShowtimePlan — returns (plan_items, skipped_reasons)."""
    slots = _load_schedule_slots(
        datetime.combine(start_date - timedelta(days=1), datetime.min.time()),
        datetime.combine(start_date + timedelta(days=days + 1), datetime.min.time()),
    )
    needs = _build_bulk_needs(movies, start_date, days, per_day_count, total_count, per_movie_mode, price)

    plan_items: list = []
    skipped: list = []
    for need in needs:
        item, skip_reason = _try_schedule(need, rooms, slots, now)
        if item is not None:
            plan_items.append(item)
            slots.append({"movie_id": item["movie_id"], "movie_title": item["movie_title"],
                          "room_id": item["room_id"], "room_name": item["room_name"],
                          "start_time": item["start_time"], "duration": item["duration"],
                          "base_price": item["price"]})
        else:
            skipped.append(skip_reason)

    return plan_items, skipped


def _fmt_item(item) -> str:
    room_name = item["room_name"] if normalize(item["room_name"]).startswith("phong") else f"Phòng {item['room_name']}"
    return f"{item['start_time'].strftime('%d/%m/%Y %H:%M')} | {item['movie_title']} | {room_name} | {_fmt_money(item['price'])}"


def _save_bulk_plan(plan_items: list, now: datetime) -> tuple:
    """Port of SaveBulkShowtimePlan — returns (created_items, skipped_reasons)."""
    if not plan_items:
        return [], []

    start = min(i["start_time"] for i in plan_items).date() - timedelta(days=1)
    end = max(i["start_time"] for i in plan_items).date() + timedelta(days=2)
    slots = _load_schedule_slots(datetime.combine(start, datetime.min.time()),
                                 datetime.combine(end, datetime.min.time()))
    created: list = []
    skipped: list = []

    try:
        with get_conn() as conn:
            c = conn.cursor()
            c.execute("SELECT Id, ISNULL(Status, N'Sẵn sàng') AS Status FROM Rooms")
            room_status_map = {r.Id: r.Status for r in c.fetchall()}

            for item in sorted(plan_items, key=lambda i: i["start_time"]):
                if item["start_time"] < now + timedelta(minutes=15):
                    skipped.append(_fmt_item(item) + ": đã qua hoặc quá sát giờ hiện tại.")
                    continue

                room_status = room_status_map.get(item["room_id"], "Sẵn sàng")
                if room_status in ("Bảo trì", "Tạm đóng"):
                    skipped.append(_fmt_item(item) + f": phòng đang ở trạng thái {room_status}.")
                    continue

                conflict = _find_schedule_conflict(item["room_id"], item["start_time"], item["duration"], slots)
                if conflict:
                    skipped.append(
                        _fmt_item(item) + f": trùng với {conflict['movie_title']} "
                        f"{conflict['start_time'].strftime('%d/%m %H:%M')}.")
                    continue

                c.execute(
                    "INSERT INTO Showtimes (MovieId, RoomId, StartTime, BasePrice) VALUES (?,?,?,?)",
                    item["movie_id"], item["room_id"], item["start_time"], item["price"])
                created.append(item)
                slots.append({"movie_id": item["movie_id"], "movie_title": item["movie_title"],
                              "room_id": item["room_id"], "room_name": item["room_name"],
                              "start_time": item["start_time"], "duration": item["duration"],
                              "base_price": item["price"]})

            if created:
                conn.commit()
    except Exception as e:
        skipped.append(f"Lỗi lưu suất chiếu: {e}")

    return created, skipped


# ─────────────────────────────────────────────────────────────────────────────
#  Showtime creation / deletion reply builders
# ─────────────────────────────────────────────────────────────────────────────
def _delete_showtimes(message: str, nm: str, now: datetime) -> dict:
    """Xóa suất chiếu theo ngày — chỉ xóa suất chưa bán vé."""
    requested = extract_requested_date(message, now) or datetime.combine(now.date(), datetime.min.time())
    day_start = requested.replace(hour=0, minute=0, second=0)
    day_end   = day_start + timedelta(days=1)

    # Tìm suất trong ngày (dùng derived table thay correlated subquery để tránh lỗi pymssql)
    sql_find = """
        SELECT s.Id, s.StartTime,
               ISNULL(m.Title, N'Phim chua ro') AS title,
               ISNULL(r.Name, N'N/A') AS room_name,
               ISNULL(sold.cnt, 0) AS sold_seats
        FROM Showtimes s
        LEFT JOIN Movies m ON m.Id = s.MovieId
        LEFT JOIN Rooms  r ON r.Id = s.RoomId
        LEFT JOIN (
            SELECT b.ShowtimeId, COUNT(DISTINCT bs.Id) AS cnt
            FROM Bookings b
            INNER JOIN BookingSeats bs ON bs.BookingId = b.Id
            WHERE b.PaymentStatus IN (N'Paid', N'PendingPayment', N'CheckedIn')
            GROUP BY b.ShowtimeId
        ) sold ON sold.ShowtimeId = s.Id
        WHERE s.StartTime >= ? AND s.StartTime < ?
        ORDER BY s.StartTime
    """
    try:
        with get_conn() as conn:
            c = conn.cursor()
            c.execute(sql_find, day_start, day_end)
            rows = c.fetchall()
    except Exception as e:
        return {"reply": f"Lỗi tải suất chiếu: {e}", "actions": []}

    if not rows:
        return {
            "reply": f"Không có suất chiếu nào trong ngày {day_start.strftime('%d/%m/%Y')} để xóa.",
            "actions": [_admin_action("Quản lý suất chiếu", "Showtimes")],
        }

    can_delete  = [r for r in rows if int(r.sold_seats or 0) == 0]
    has_tickets = [r for r in rows if int(r.sold_seats or 0) > 0]

    if not can_delete:
        lines = [f"Không thể xóa: tất cả {len(rows)} suất ngày {day_start.strftime('%d/%m/%Y')} đều đã có vé đặt."]
        for r in has_tickets[:5]:
            room = r.room_name if normalize(r.room_name).startswith("phong") else f"Phòng {r.room_name}"
            lines.append(f"- {r.StartTime.strftime('%H:%M')} | {r.title} | {room} | {r.sold_seats} vé")
        return {"reply": "\n".join(lines), "actions": [_admin_action("Quản lý suất chiếu", "Showtimes")]}

    ids_to_delete = [r.Id for r in can_delete]
    try:
        with get_conn() as conn:
            c = conn.cursor()
            placeholders = ",".join(["?"] * len(ids_to_delete))
            c.execute(f"DELETE FROM Showtimes WHERE Id IN ({placeholders})", *ids_to_delete)
            conn.commit()
    except Exception as e:
        return {"reply": f"Lỗi xóa suất chiếu: {e}", "actions": []}

    lines = [f"Đã xóa {len(can_delete)} suất chiếu ngày {day_start.strftime('%d/%m/%Y')}."]
    for r in can_delete[:8]:
        room = r.room_name if normalize(r.room_name).startswith("phong") else f"Phòng {r.room_name}"
        lines.append(f"- {r.StartTime.strftime('%H:%M')} | {r.title} | {room}")
    if has_tickets:
        lines.append(f"Giữ lại {len(has_tickets)} suất đã có vé đặt (không thể xóa).")

    return {"reply": "\n".join(lines), "actions": [_admin_action("Quản lý suất chiếu", "Showtimes")]}


def _create_showtime(message: str, nm: str, now: datetime) -> dict:
    """Port of BuildAdminCreateShowtimeReply — single showtime creation."""
    movie_id = _resolve_movie_id(nm)
    room_dict = _resolve_room(nm)
    requested_date = extract_requested_date(message, now)
    requested_time = _extract_time(message)
    price = _extract_price(nm)

    movie = None
    if movie_id:
        try:
            with get_conn() as conn:
                c = conn.cursor()
                c.execute("SELECT Id, Title, DurationMinutes FROM Movies WHERE Id = ?", movie_id)
                movie = c.fetchone()
        except Exception:
            pass

    missing = []
    if not movie:
        missing.append("phim")
    if not requested_date:
        missing.append("ngày")
    if not requested_time or requested_time[1] is None:
        missing.append("giờ dạng HH:mm")
    if not room_dict:
        missing.append("phòng")
    if price is None:
        missing.append("giá vé")

    if missing:
        return {
            "reply": ("Mình chưa đủ dữ liệu để tạo suất chiếu. Bạn cần gửi đủ: "
                      + ", ".join(missing)
                      + ". Ví dụ: \"tạo suất chiếu Conan ngày 20/05/2026 19:00 phòng 5 giá 50000\"."),
            "actions": [_admin_action("Quản lý suất chiếu", "Showtimes")],
        }

    if room_dict.get("status") in ("Bảo trì", "Tạm đóng"):
        room_name = room_dict["name"] if normalize(room_dict["name"]).startswith("phong") else f"Phòng {room_dict['name']}"
        return {
            "reply": f"{room_name} đang ở trạng thái \"{room_dict['status']}\", không thể tạo suất chiếu. Vui lòng chọn phòng khác hoặc cập nhật trạng thái phòng.",
            "actions": [_admin_action("Quản lý phòng", "Rooms")],
        }

    hour, minute = requested_time
    start_time = datetime.combine(
        requested_date.date() if hasattr(requested_date, "date") else requested_date,
        dt_time(hour, minute or 0))

    if start_time < now:
        return {
            "reply": f"Không thể tạo suất chiếu ở thời gian đã qua: {start_time.strftime('%d/%m/%Y %H:%M')}.",
            "actions": [_admin_action("Quản lý suất chiếu", "Showtimes")],
        }

    # Conflict check
    try:
        with get_conn() as conn:
            c = conn.cursor()
            c.execute(
                "SELECT s.StartTime, ISNULL(m.DurationMinutes,0) AS dur, ISNULL(m.Title,'Phim chua ro') AS title"
                " FROM Showtimes s LEFT JOIN Movies m ON m.Id=s.MovieId WHERE s.RoomId=?",
                room_dict["id"])
            existing = c.fetchall()
    except Exception as e:
        return {"reply": f"Lỗi kiểm tra lịch chiếu: {e}", "actions": []}

    duration = int(movie.DurationMinutes or 0)
    new_end = start_time + timedelta(minutes=duration)
    for ex in existing:
        ex_end = ex.StartTime + timedelta(minutes=int(ex.dur or 0))
        separated = (start_time >= ex_end + timedelta(minutes=30)
                     or ex.StartTime >= new_end + timedelta(minutes=30))
        if not separated:
            room_name = room_dict["name"] if normalize(room_dict["name"]).startswith("phong") else f"Phòng {room_dict['name']}"
            return {
                "reply": (f"Không tạo được vì trùng lịch với phim \"{ex.title}\" tại {room_name}, "
                          f"khung {ex.StartTime.strftime('%d/%m/%Y %H:%M')} - {ex_end.strftime('%H:%M')}. "
                          "Mỗi suất cùng phòng cần cách nhau ít nhất 30 phút."),
                "actions": [_admin_action("Quản lý suất chiếu", "Showtimes")],
            }

    # Insert
    try:
        with get_conn() as conn:
            c = conn.cursor()
            c.execute(
                "INSERT INTO Showtimes (MovieId, RoomId, StartTime, BasePrice) VALUES (?,?,?,?)",
                movie.Id, room_dict["id"], start_time, price)
            conn.commit()
    except Exception as e:
        return {"reply": f"Lỗi tạo suất chiếu: {e}", "actions": [_admin_action("Quản lý suất chiếu", "Showtimes")]}

    room_name = room_dict["name"] if normalize(room_dict["name"]).startswith("phong") else f"Phòng {room_dict['name']}"
    return {
        "reply": (f"Đã tạo suất chiếu thành công:\n"
                  f"- Phim: {movie.Title}\n"
                  f"- Thời gian: {start_time.strftime('%d/%m/%Y %H:%M')}\n"
                  f"- Phòng: {room_name}\n"
                  f"- Giá vé: {_fmt_money(price)}"),
        "actions": [_admin_action("Quản lý suất chiếu", "Showtimes")],
    }


def _bulk_create_showtime(message: str, nm: str, now: datetime, user_id) -> dict:
    """Port of BuildAdminBulkCreateShowtimeReply."""
    per_day_count = _extract_bulk_per_day(nm)
    total_count = None if per_day_count is not None else _extract_bulk_total(nm)
    movie_id = None if _is_all_now_showing_q(nm) else _resolve_movie_id(nm)
    # resolve_all: explicit "all movies" request OR no specific movie found but a count was given
    resolve_all = (movie_id is None and (per_day_count is not None or total_count is not None
                                          or _is_all_now_showing_q(nm)))

    if resolve_all:
        target_movies = _load_now_showing_movies(now)
        movie_label = f"tất cả phim đang chiếu ({len(target_movies)} phim)"
    else:
        target_movies = []
        movie_label = ""
        if movie_id:
            try:
                with get_conn() as conn:
                    c = conn.cursor()
                    c.execute("SELECT Id, Title, DurationMinutes FROM Movies WHERE Id = ?", movie_id)
                    row = c.fetchone()
                    if row:
                        target_movies = [{"id": row.Id, "title": row.Title,
                                          "duration": int(row.DurationMinutes or 0)}]
                        movie_label = row.Title
            except Exception as e:
                return {"reply": f"Lỗi tải phim: {e}", "actions": [_admin_action("Quản lý suất chiếu", "Showtimes")]}

    days = _extract_bulk_days(nm)
    start_date = _resolve_bulk_start(message, nm, now)
    price = _extract_price(nm)
    room_dict = _resolve_room(nm)

    missing = []
    if not target_movies:
        missing.append("tất cả phim đang chiếu" if resolve_all else "phim")
    if per_day_count is None and total_count is None:
        missing.append("số suất cần tạo")

    if missing:
        return {
            "reply": ("Mình chưa đủ dữ liệu để lập lịch hàng loạt. Bạn cần gửi: "
                      + ", ".join(missing)
                      + ". Ví dụ: \"tạo 10 suất chiếu cho tất cả phim đang chiếu hôm nay\""
                      " hoặc \"tạo suất chiếu Hoàng Tử Quỷ trong 3 ngày tới, mỗi ngày 3 suất\"."),
            "actions": [_admin_action("Quản lý suất chiếu", "Showtimes")],
        }

    try:
        with get_conn() as conn:
            c = conn.cursor()
            if room_dict:
                c.execute("SELECT Id, Name, ISNULL(Status, N'Sẵn sàng') AS Status FROM Rooms WHERE Id = ?", room_dict["id"])
            else:
                c.execute("SELECT Id, Name, ISNULL(Status, N'Sẵn sàng') AS Status FROM Rooms ORDER BY Id")
            room_rows = c.fetchall()
    except Exception as e:
        return {"reply": f"Lỗi tải phòng: {e}", "actions": [_admin_action("Quản lý phòng", "Rooms")]}

    rooms = [{"id": r.Id, "name": r.Name, "status": r.Status}
             for r in room_rows
             if r.Status not in ("Bảo trì", "Tạm đóng")]

    if not rooms:
        msg = (f"Phòng {room_dict['name']} đang ở trạng thái Bảo trì/Tạm đóng, không thể tạo suất chiếu." if room_dict
               else "Hiện không có phòng nào ở trạng thái sẵn sàng để bot lập lịch suất chiếu.")
        return {"reply": msg, "actions": [_admin_action("Quản lý phòng", "Rooms")]}

    per_movie_mode = len(target_movies) == 1 or "moi phim" in nm or "tung phim" in nm
    if per_day_count is not None:
        est_total = days * per_day_count * (len(target_movies) if per_movie_mode else 1)
    else:
        est_total = total_count or 0

    if est_total > 60:
        return {
            "reply": (f"Yêu cầu này sẽ tạo {est_total} suất, vượt giới hạn an toàn 60 suất/lần. "
                      "Bạn nên chia nhỏ theo phim hoặc theo ngày để dễ kiểm tra lịch."),
            "actions": [_admin_action("Quản lý suất chiếu", "Showtimes")],
        }

    plan_items, skipped = _build_bulk_plan(
        target_movies, rooms, start_date, days,
        per_day_count, total_count, per_movie_mode, price, now)

    if not plan_items:
        skip_text = "\n- ".join(skipped[:6]) if skipped else "không tìm được khung giờ trống"
        return {
            "reply": f"Mình chưa tìm được khung giờ/phòng trống để lập lịch. Lý do chính:\n- {skip_text}",
            "actions": [_admin_action("Quản lý suất chiếu", "Showtimes")],
        }

    end_date = start_date + timedelta(days=days - 1)
    price_note = (f"Giá vé áp dụng: {_fmt_money(price)}/suất." if price is not None
                  else "Giá vé tự lấy theo suất gần nhất của từng phim; phim chưa có suất dùng 70.000đ.")
    _set_pending_plan(user_id, plan_items, price_note, start_date, end_date)

    def _fmt_room_name(r):
        return r["name"] if normalize(r["name"]).startswith("phong") else f"Phòng {r['name']}"

    lines = [
        f"Mình đã lập kế hoạch tạo {len(plan_items)}/{est_total} suất chiếu.",
        f"- Khoảng ngày: {start_date.strftime('%d/%m/%Y')} - {end_date.strftime('%d/%m/%Y')}.",
        f"- Phim: {movie_label}.",
        f"- Phòng dùng: {', '.join(_fmt_room_name(r) for r in rooms)}.",
        f"- {price_note}",
        "Các suất dự kiến:",
    ]
    for item in plan_items[:12]:
        lines.append(f"- {_fmt_item(item)}")
    if len(plan_items) > 12:
        lines.append(f"- ... còn {len(plan_items) - 12} suất khác.")
    if skipped:
        lines.append(f"Không xếp được {len(skipped)} yêu cầu:")
        for r in skipped[:5]:
            lines.append(f"- {r}")
    lines.append("Nếu đúng, nhắn: `xác nhận tạo suất`. Muốn bỏ kế hoạch, nhắn: `hủy tạo suất`.")

    return {"reply": "\n".join(lines), "actions": [_admin_action("Quản lý suất chiếu", "Showtimes")]}


def _bulk_confirm(user_id, now: datetime) -> dict:
    """Port of BuildAdminConfirmBulkShowtimeReply."""
    plan = _get_pending_plan(user_id)
    if not plan:
        return {
            "reply": "Không có kế hoạch lịch chiếu nào đang chờ xác nhận (hoặc đã hết hạn 15 phút). "
                     "Bạn hãy lập lại kế hoạch bằng cách yêu cầu tạo suất chiếu.",
            "actions": [_admin_action("Quản lý suất chiếu", "Showtimes")],
        }

    _clear_pending_plan(user_id)
    created, skipped = _save_bulk_plan(plan["items"], now)

    lines = [f"Đã lưu thành công {len(created)}/{len(plan['items'])} suất chiếu."]
    for item in created[:10]:
        lines.append(f"- {_fmt_item(item)}")
    if len(created) > 10:
        lines.append(f"- ... còn {len(created) - 10} suất khác.")
    if skipped:
        lines.append(f"Bỏ qua {len(skipped)} suất do xung đột hoặc phòng thay đổi trạng thái:")
        for r in skipped[:5]:
            lines.append(f"- {r}")

    return {"reply": "\n".join(lines), "actions": [_admin_action("Quản lý suất chiếu", "Showtimes")]}


def _bulk_cancel(user_id) -> dict:
    """Port of BuildAdminCancelBulkShowtimeReply."""
    _clear_pending_plan(user_id)
    return {
        "reply": "Đã hủy kế hoạch tạo suất chiếu hàng loạt. Chưa có suất nào được lưu.",
        "actions": [_admin_action("Quản lý suất chiếu", "Showtimes")],
    }


def _is_bulk_confirm_q(nm: str, has_plan: bool) -> bool:
    confirms = (nm in ("ok", "oke", "dong y", "xac nhan")
                or any(kw in nm for kw in ["xac nhan tao suat", "xac nhan tao lich", "tao di", "tao luon", "chot"]))
    explicit = any(kw in nm for kw in ["xac nhan tao suat", "xac nhan tao lich"])
    return confirms and (has_plan or explicit)


def _is_bulk_cancel_q(nm: str, has_plan: bool) -> bool:
    explicit = (any(kw in nm for kw in ["huy tao suat", "huy ke hoach", "khong tao", "bo qua"])
                or nm == "huy")
    return explicit and (has_plan or any(kw in nm for kw in ["tao suat", "ke hoach"]))


# ─────────────────────────────────────────────────────────────────────────────
#  Main entry point
# ─────────────────────────────────────────────────────────────────────────────
def try_build_admin_reply(message: str, role: Optional[str], now: datetime, user_id=None) -> Optional[dict]:
    """Called from main.py when pageContext.area/mode == 'admin'. Returns None → fall through to Gemini."""
    if not role or role.lower() != "admin":
        return {
            "reply": "Mình chỉ hỗ trợ dữ liệu quản trị khi bạn đăng nhập bằng tài khoản Admin.",
            "actions": [{"type": "open_url", "label": "Đăng nhập Admin", "url": "/User/Login"}]
        }

    nm = _expand_admin(expand_synonyms(normalize(message)))
    pending = _get_pending_plan(user_id)
    has_plan = pending is not None

    if _is_admin_project_data_q(nm):
        return _project_data(now)
    if _is_bulk_cancel_q(nm, has_plan):
        return _bulk_cancel(user_id)
    if _is_bulk_confirm_q(nm, has_plan):
        return _bulk_confirm(user_id, now)
    if _is_delete_showtime_q(nm):
        return _delete_showtimes(message, nm, now)
    if _is_bulk_create_q(nm):
        return _bulk_create_showtime(message, nm, now, user_id)
    if _is_create_showtime_q(nm):
        return _create_showtime(message, nm, now)
    if _is_admin_chatbot_training_q(nm):
        return _chatbot_training()
    if _is_admin_counter_sale_q(nm):
        return _counter_sales(message, nm, now)
    if _is_admin_attendance_q(nm):
        return {
            "reply": "Mình chưa hỗ trợ xem chấm công qua chatbot Python. Bạn vào trang Chấm công để xem chi tiết.",
            "actions": [_admin_action("Chấm công", "Attendance")]
        }
    if _is_admin_payment_q(nm):
        return _payment(message, nm, now)
    if _is_admin_showtime_q(nm):
        return _showtime(message, now)
    if _is_admin_movie_q(nm):
        return _movies(message, nm, now)
    if _is_admin_room_q(nm):
        return _rooms(now)
    if _is_admin_food_q(nm):
        return _food(message, nm, now)
    if _is_admin_staff_q(nm):
        return _staff(message, nm)
    if _is_admin_customer_q(nm):
        return _customers(message, nm, now)

    # Default: dashboard overview
    return _dashboard(message, nm, now)
