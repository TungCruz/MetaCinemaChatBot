"""
Port of ChatController.cs TryBuildRoutedReply — customer-facing intents only.
Admin/staff intents are still handled by C# (not ported yet).
"""
import logging
import re
import time as _time
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional
from db import get_conn

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  Text normalizer — port of NormalizeText()
# ─────────────────────────────────────────────────────────────────────────────
def normalize(text: str) -> str:
    if not text:
        return ""
    text = text.lower().replace("đ", "d").replace("Đ", "d")
    nfkd = unicodedata.normalize("NFD", text)
    stripped = "".join(c for c in nfkd if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", stripped).strip()


# ─────────────────────────────────────────────────────────────────────────────
#  Synonym expansion — maps nhiều cách nói khác nhau → keyword chuẩn
#  Tất cả string đã ở dạng normalize() (không dấu, lowercase, single space).
#  Mỗi tuple: ([variants], canonical_keyword_đã_có_trong_intent_detectors)
# ─────────────────────────────────────────────────────────────────────────────
_SYNONYM_GROUPS: list[tuple[list[str], str]] = [
    # ── Lịch chiếu / đặt vé ───────────────────────────────────────────────
    (["ra rap", "den rap", "di rap", "muon di xem phim",
      "muon dat ve", "book ve", "dat cho", "mua cho",
      "buoi chieu", "phim chieu luc", "co suat nao", "bao gio chieu",
      "khi nao chieu", "chieu o dau", "lich toi nay",
      "lich chieu toi nay", "co lich nao", "con lich nao"], "lich chieu"),

    # ── Danh sách phim đang chiếu ─────────────────────────────────────────
    (["xem gi", "xem phim gi", "co gi xem", "co gi hay",
      "hom nay co gi", "co gi chieu", "phim nao co",
      "dang co phim gi", "phim tuan nay", "cuoi tuan co gi",
      "phim gi co", "co phim gi khong", "toi nay chieu gi",
      "toi nay xem gi", "hom nay chieu gi", "chieu nay co gi",
      "dem nay co gi"], "phim gi"),

    # ── Nội dung / thông tin phim ─────────────────────────────────────────
    (["cot truyen", "tom tat phim", "tom tat noi dung",
      "noi dung chinh", "kich ban", "nhan vat chinh",
      "ke ve cai gi", "phim ke ve"], "noi dung phim"),
    (["co hay khong", "hay khong", "nen xem khong",
      "dang xem khong", "co dang xem khong", "tot khong",
      "nhu the nao", "the nao", "cam nhan", "danh gia",
      "rating", "diem so", "diem imdb"], "review phim"),

    # ── Đồ ăn / thức uống ────────────────────────────────────────────────
    (["bong ngo"], "bap"),                               # bỏng ngô = bắp
    (["thuc an", "an gi", "do co san", "do an nhe",
      "mon an"], "do an"),
    (["thuc uong", "nuoc ngot", "coca cola", "coca",
      "giai khat", "pepsi"], "nuoc"),
    (["keo", "nachos", "chip", "banh vat"], "snack"),

    # ── Chính sách / quy định ────────────────────────────────────────────
    (["tra lai tien", "tra lai", "refund",
      "hoan lai tien", "hoan lai"], "hoan tien"),
    (["huy bo", "huy dat", "huy lich", "khong di xem nua",
      "muon huy"], "huy ve"),
    (["noi quy", "quy che", "quy tac",
      "the le", "dieu kien vao rap"], "quy dinh"),
    (["mang tu ngoai", "mang vao", "dem vao rap",
      "tu ngoai vao", "mang theo do an"], "mang do an"),
    (["so dien thoai", "so phone", "lien he",
      "lien lac", "goi dien", "contact", "cham soc khach hang",
      "cskh", "support"], "hotline"),
    (["o dau", "o cho nao", "duong nao", "vi tri rap",
      "gap o dau", "rap o dau", "tim rap", "ban do",
      "chi nhanh"], "dia chi"),
    (["sale", "voucher", "ma giam", "giam gia",
      "discount", "ma uu dai", "ma khuyen mai"], "khuyen mai"),
    (["bao nhieu tuoi", "may tuoi", "gioi han tuoi",
      "tuoi toi thieu", "kiem tra tuoi", "quy dinh tuoi"], "do tuoi"),

    # ── Vé của tôi ───────────────────────────────────────────────────────
    (["xem ve", "kiem tra ve", "check ve", "ve cua minh",
      "ve toi dat", "lich su dat", "lich su mua",
      "lich su booking", "don hang cua toi",
      "ve da mua", "booking cua toi", "tim ve"], "ve cua toi"),

    # ── Vấn đề thanh toán ────────────────────────────────────────────────
    (["mat tien", "bi mat tien", "chuyen tien roi",
      "da chuyen khoan roi", "tra tien roi",
      "tru mat tien roi"], "bi tru tien"),
    (["sao khong co ve", "sao chua co ve",
      "chua nhan duoc ve", "ve dau roi",
      "tim khong thay ve", "khong thay ve dau"], "chua nhan ve"),
    (["tra tien", "momo", "zalopay",
      "ngan hang", "internet banking"], "chuyen khoan"),

    # ── Hướng dẫn đặt vé ─────────────────────────────────────────────────
    (["huong dan dat ve", "cach dat ve", "dat ve nhu the nao",
      "mua ve nhu the nao", "quy trinh dat ve",
      "buoc dat ve", "lam the nao de dat ve",
      "huong dan mua ve", "tu dat ve"], "dat ve"),

    # ── Ghế / chỗ ngồi ───────────────────────────────────────────────────
    (["vi tri ngoi", "cho ngoi", "vi tri ghe",
      "chon vi tri"], "ghe"),
    (["ghe trong con", "ghe con", "con ghe nao",
      "con cho ngoi", "con bao nhieu ghe"], "cho trong"),

    # ── Teen-code / Casual speech ─────────────────────────────────────────
    # "coi phim" = muốn đi xem → tra lịch chiếu
    (["coi phim", "muon coi", "di coi", "ra coi",
      "coi not", "coi bom tan"], "lich chieu"),
    # "coi gì" = xem phim gì đang chiếu
    (["coi gi", "coi phim gi", "chieu gi vay",
      "co gi coi", "tap phim gi"], "phim gi"),
    # Đánh giá tích cực / hỏi review
    (["phim xin", "phim dinh", "phim hot", "phim hay vl",
      "nhieu nguoi khen", "phim trend", "phim noi tieng"], "review phim"),
    # Phim sắp ra / ngày khởi chiếu
    (["sap ra chua", "bao gio ra rap", "khi nao ra rap",
      "sap duoc chieu chua", "ngay khoi chieu", "khi nao co lich",
      "bao gio co suat", "dự kien chieu", "du kien chieu"], "sap chieu"),
]


def _apply_synonyms(nm: str, groups: list[tuple[list[str], str]]) -> str:
    """Shared loop: append canonical keywords from a synonym group table to nm."""
    extras = [c for variants, c in groups if c not in nm and any(v in nm for v in variants)]
    return (nm + " " + " ".join(extras)) if extras else nm


def expand_synonyms(nm: str) -> str:
    """Áp dụng _SYNONYM_GROUPS (customer context) vào nm đã normalize."""
    return _apply_synonyms(nm, _SYNONYM_GROUPS)


# Keywords that signal an explicit "how-to" booking question
# (used in try_build_routed_reply to prioritise booking guide over showtime)
_GUIDANCE_HINTS: list[str] = [
    "huong dan", "cach dat", "cach mua", "nhu the nao de",
    "lam sao", "buoc", "quy trinh", "tu dat ve", "tu mua ve",
]

# Pronoun references to a previously mentioned movie ("phim đó", "phim này"…)
# Detected to inject last_movie_id from session into page_context
_MOVIE_REF_WORDS: list[str] = [
    "phim do", "phim nay", "phim kia", "phim ay",
    "bo phim do", "phim vua noi", "cai phim do",
]

# Words that signal a recommendation/review context — policy detector defers to Gemini for these
_RECOMMEND_WORDS: list[str] = [
    "goi y", "nen xem", "phu hop", "co hay khong", "hay khong",
    "danh gia", "review", "nen di", "nen chon", "thich hop",
    "cho tre em", "danh cho", "de xem", "xem cung", "nhat cho",
]

_MOVIE_TITLE_STOP_WORDS = {
    "phim", "dien", "anh", "tham", "lung", "danh", "cua",
    "nhung", "mot", "voi", "cho", "the", "and", "movie",
}

_AGE_QUESTION_WORDS = [
    "tuoi", "do tuoi", "c18", "c16", "c13", "phu hop",
    "tre em", "hoc sinh", "duoi 18", "duoi 16", "duoi 13",
    "bao nhieu tuoi", "may tuoi", "gioi han tuoi", "danh cho",
]


# ─────────────────────────────────────────────────────────────────────────────
#  Intent detectors — port of Is*Question() methods
# ─────────────────────────────────────────────────────────────────────────────
def asks_global_movie_list(nm: str) -> bool:
    # Loại trừ câu hỏi dạng "X trong phim nào" / "nhân vật X phim nào"
    # — đây là tra cứu phim theo nhân vật/diễn viên, KHÔNG phải "list suất chiếu"
    if "trong phim nao" in nm:
        return False
    if any(kw in nm for kw in ["nhan vat", "dien vien nao", "ai dong vai", "vai chinh la"]):
        return False
    return any(kw in nm for kw in ["phim gi", "phim nao", "dang chieu", "hom nay co phim", "toi nay co phim"])


def is_showtime_question(nm: str) -> bool:
    return any(kw in nm for kw in [
        "suat chieu", "lich chieu", "gio chieu", "chieu luc",
        "chieu may gio", "co suat", "dat ve", "mua ve", "con ve"
    ]) or asks_global_movie_list(nm)


def is_seat_status_question(nm: str) -> bool:
    mentions_seat = any(kw in nm for kw in ["ghe", "cho ngoi", "cho trong", "seat"])
    asks_avail = any(kw in nm for kw in ["con", "het", "trong", "bao nhieu", "kiem tra", "tinh trang"])
    mentions_avail = any(kw in nm for kw in ["con ve", "het ve", "ve trong", "con cho", "het cho"])
    mentions_time = any(kw in nm for kw in ["suat", "luc", ":"]) or bool(re.search(r"\d{1,2}\s*(h|gio)", nm))
    return (mentions_seat and (asks_avail or mentions_time)) or (mentions_avail and mentions_time)


def is_movie_question(nm: str) -> bool:
    # Matches list-all-movies queries and specific movie-info keywords; open-ended questions go to Gemini
    return asks_global_movie_list(nm) or any(kw in nm for kw in [
        "noi dung phim", "the loai", "dien vien", "dao dien",
        "phim hay", "review phim", "gioi thieu phim", "noi ve gi", "ke ve",
    ])


def is_food_question(nm: str) -> bool:
    return any(kw in nm for kw in ["do an", "do uong", "bap", "nuoc", "combo", "menu", "snack", "popcorn"])


def is_policy_question(nm: str) -> bool:
    # Exclude recommendation / review contexts — let Gemini handle those
    if any(kw in nm for kw in _RECOMMEND_WORDS):
        return False
    return any(kw in nm for kw in [
        "chinh sach", "quy dinh", "hoan tien", "huy ve", "doi suat", "doi ve",
        "c18", "c16", "do tuoi", "mang do an", "khuyen mai", "uu dai", "hotline", "dia chi"
    ])


def is_movie_age_question(nm: str) -> bool:
    return any(kw in nm for kw in _AGE_QUESTION_WORDS)


# ─────────────────────────────────────────────────────────────────────────────
#  Character / actor search — "Gojo Satoru là nhân vật trong phim nào?"
# ─────────────────────────────────────────────────────────────────────────────
def is_character_search_question(nm: str) -> bool:
    """Phát hiện câu hỏi tìm phim theo nhân vật hoặc diễn viên."""
    if "trong phim nao" in nm:
        return True
    if "phim nao co nhan vat" in nm or "phim nao co dien vien" in nm:
        return True
    if "xuat hien trong phim" in nm or "dong vai trong phim" in nm:
        return True
    if "la nhan vat" in nm and "phim" in nm:
        return True
    return False


_CHAR_STOPS = {
    "la", "nhan", "vat", "trong", "phim", "nao", "gi", "ai",
    "co", "o", "mot", "cua", "va", "de", "da", "thi", "voi",
    "hay", "hoac", "khi", "neu", "va", "cung", "rat",
}


def _extract_query_name(nm: str) -> Optional[str]:
    """Trích tên nhân vật / diễn viên từ câu hỏi đã normalize.
    Trả về chuỗi tên (dạng normalize) hoặc None nếu không tìm được."""
    patterns = [
        # "gojo satoru la nhan vat trong phim nao"
        r"^(.+?)\s+la\s+nhan\s+vat",
        # "gojo satoru xuat hien trong phim nao"
        r"^(.+?)\s+(?:xuat\s+hien|co\s+mat)\s+trong",
        # "nhan vat gojo satoru trong phim nao"
        r"nhan\s+vat\s+(.+?)\s+(?:trong|phim|o)",
        # "phim nao co nhan vat gojo satoru"
        r"phim\s+nao\s+co\s+nhan\s+vat\s+(.+?)$",
        r"phim\s+nao\s+co\s+dien\s+vien\s+(.+?)$",
        # "gojo satoru dong vai trong phim nao"
        r"^(.+?)\s+dong\s+vai\s+trong",
        # generic: "X trong phim nao"
        r"^(.+?)\s+trong\s+phim\s+nao",
    ]
    for pat in patterns:
        m = re.search(pat, nm)
        if m:
            raw = m.group(1).strip()
            words = [w for w in raw.split() if w not in _CHAR_STOPS and len(w) >= 2]
            if words:
                return " ".join(words)
    return None


def _search_movies_by_actor(actor_name: str, now: datetime) -> list:
    """Tìm phim theo tên nhân vật / diễn viên trong cột MainActors.
    Nếu không khớp MainActors thì thử Description làm fallback.
    Trả về list movie rows sắp xếp theo relevance."""
    if not actor_name or len(actor_name) < 2:
        return []
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT m.Id, m.Title, m.Genre, m.DurationMinutes, m.Rating,
                       m.Description, m.ReleaseDate, m.MainActors,
                       (SELECT MIN(s.StartTime) FROM Showtimes s
                        WHERE s.MovieId = m.Id AND s.StartTime >= ?) AS FirstShowtime
                FROM Movies m
                """,
                now,
            )
            movies = cursor.fetchall()
    except Exception:
        return []

    name_norm = normalize(actor_name)
    name_tokens = [t for t in name_norm.split() if len(t) >= 2]
    if not name_tokens:
        return []

    def _word_in(token: str, text: str) -> bool:
        """Kiểm tra token xuất hiện như một từ riêng (word boundary) trong text."""
        return bool(re.search(r'\b' + re.escape(token) + r'\b', text))

    qualifying = [t for t in name_tokens if len(t) >= 3]

    matched = []
    for movie in movies:
        actors_norm = normalize(movie.MainActors or "")
        desc_norm   = normalize(movie.Description or "")

        # Khớp cụm đầy đủ → ưu tiên cao nhất
        if name_norm in actors_norm:
            matched.append((len(name_tokens) * 10, len(name_tokens) * 10, movie))
            continue
        if name_norm in desc_norm:
            matched.append((len(name_tokens) * 4, 0, movie))
            continue

        # Khớp từng token — YÊU CẦU tất cả qualifying tokens đều phải xuất hiện
        actor_score = 0
        desc_score = 0
        all_found = True
        for t in qualifying:
            if _word_in(t, actors_norm):
                actor_score += 5 if len(t) >= 5 else 2
            elif _word_in(t, desc_norm):
                desc_score += 2 if len(t) >= 5 else 1
            else:
                all_found = False
                break

        if all_found and (actor_score + desc_score) > 0:
            matched.append((actor_score + desc_score, actor_score, movie))

    # Sắp xếp: MainActors match trước, sau đó theo total score
    matched.sort(key=lambda x: (-x[1], -x[0]))
    return [m for _, _, m in matched[:4]]


def _build_character_search_reply(message: str, now: datetime) -> Optional[dict]:
    """Xây reply cho câu hỏi 'X là nhân vật / diễn viên trong phim nào'.
    Tìm trong cột MainActors của Movies.
    Trả về dict nếu tìm được phim trong DB, None để rơi qua Gemini."""
    nm = expand_synonyms(normalize(message))
    if not is_character_search_question(nm):
        return None

    char_name = _extract_query_name(nm)
    if not char_name:
        return None

    movies = _search_movies_by_actor(char_name, now)
    if not movies:
        # Không có trong MainActors/Description → Gemini sẽ trả lời
        return None

    display_name = char_name.title()
    lines = [f'Mình tìm thấy phim tại MetaCinema có diễn viên / nhân vật **{display_name}**:']
    actions = []
    for m in movies:
        actors_str = (m.MainActors or "").strip()
        showtime_part = ""
        if m.FirstShowtime:
            showtime_part = f" | Suất gần nhất: {m.FirstShowtime.strftime('%d/%m %H:%M')}"
        elif m.ReleaseDate:
            rd = m.ReleaseDate.date() if isinstance(m.ReleaseDate, datetime) else m.ReleaseDate
            showtime_part = f" | Dự kiến chiếu: {rd.strftime('%d/%m/%Y')}"

        cast_hint = f" ({actors_str[:60]})" if actors_str else ""
        lines.append(f"- **{m.Title}**{cast_hint}{showtime_part}")
        actions.append({
            "type": "view_movie",
            "label": f"Xem {m.Title[:22]}",
            "url": f"/Movies/Details/{m.Id}",
            "movieId": m.Id,
        })

    # Thêm nút đặt vé cho phim đang có suất chiếu
    for m in [mv for mv in movies if mv.FirstShowtime][:2]:
        next_sts = _next_showtimes_for_movie(m.Id, now, limit=2)
        actions.extend(_build_showtime_actions(next_sts))

    return {
        "reply": "\n".join(lines),
        "actions": actions[:6],
    }


# Các từ thông thường trong tên phim, không dùng để khớp
_TITLE_STOPWORDS = {
    "cua", "va", "la", "mot", "trong", "nao", "co", "hay", "bat",
    "cung", "de", "du", "thi", "voi", "cho", "ma", "khi", "theo",
    "len", "xuong", "ra", "vao", "anh", "phim", "the", "and", "of",
}


def append_related_movies_to_gemini_reply(
    message: str, gemini_reply: str, now: datetime
) -> Optional[dict]:
    """Sau khi Gemini trả lời câu hỏi nhân vật, rà soát DB tìm phim liên quan.
    Nếu tìm thấy, gắn thêm phần đề xuất vào cuối reply Gemini và trả về dict mới.
    Trả về None nếu không phải câu hỏi nhân vật hoặc không tìm thấy phim phù hợp."""
    nm = expand_synonyms(normalize(message))
    if not is_character_search_question(nm):
        return None

    reply_norm = normalize(gemini_reply)

    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT m.Id, m.Title, m.ReleaseDate, m.MainActors,
                       (SELECT MIN(s.StartTime) FROM Showtimes s
                        WHERE s.MovieId = m.Id AND s.StartTime >= ?) AS FirstShowtime
                FROM Movies m
                """,
                now,
            )
            movies = cursor.fetchall()
    except Exception:
        return None

    def _word_in_reply(token: str) -> bool:
        return bool(re.search(r'\b' + re.escape(token) + r'\b', reply_norm))

    matched = []
    for movie in movies:
        title_norm = normalize(movie.Title)
        tokens = [t for t in title_norm.split()
                  if len(t) >= 3 and t not in _TITLE_STOPWORDS]
        sig   = [t for t in tokens if len(t) >= 5]
        short = [t for t in tokens if 3 <= len(t) < 5]

        sig_hits   = sum(1 for t in sig   if _word_in_reply(t))
        short_hits = sum(1 for t in short if _word_in_reply(t))

        # Khớp nếu ≥1 từ dài (≥5 ký tự) hoặc ≥2 từ ngắn (3–4 ký tự) xuất hiện trong reply
        if sig_hits >= 1 or short_hits >= 2:
            matched.append(movie)

    if not matched:
        return None

    lines = [gemini_reply, "", "---", "Tại **MetaCinema** hiện có phim liên quan:"]
    actions = []
    for m in matched[:3]:
        showtime_part = ""
        if m.FirstShowtime:
            showtime_part = f" | Suất gần nhất: {m.FirstShowtime.strftime('%d/%m %H:%M')}"
        elif m.ReleaseDate:
            rd = m.ReleaseDate.date() if isinstance(m.ReleaseDate, datetime) else m.ReleaseDate
            showtime_part = f" | Dự kiến chiếu: {rd.strftime('%d/%m/%Y')}"
        lines.append(f"- **{m.Title}**{showtime_part}")
        actions.append({
            "type": "view_movie",
            "label": f"Xem {m.Title[:22]}",
            "url": f"/Movies/Details/{m.Id}",
            "movieId": m.Id,
        })

    for m in [mv for mv in matched if mv.FirstShowtime][:2]:
        next_sts = _next_showtimes_for_movie(m.Id, now, limit=2)
        actions.extend(_build_showtime_actions(next_sts))

    return {
        "reply": "\n".join(lines),
        "actions": actions[:6],
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Genre filter — "đề xuất phim Hoạt Hình", "phim Action", …
# ─────────────────────────────────────────────────────────────────────────────

# Normalized keyword → Vietnamese genre name (dùng cho DB LIKE query)
_GENRE_MAP: dict[str, str] = {
    "hoat hinh":        "Hoạt Hình",
    "anime":            "Hoạt Hình",
    "hanh dong":        "Hành Động",
    "action":           "Hành Động",
    "kinh di":          "Kinh Dị",
    "horror":           "Kinh Dị",
    "tinh cam":         "Tình Cảm",
    "romance":          "Tình Cảm",
    "lang man":         "Tình Cảm",
    "hai huoc":         "Hài Hước",
    "comedy":           "Hài Hước",
    "vien tuong":       "Viễn Tưởng",
    "sci fi":           "Viễn Tưởng",
    "khoa hoc":         "Viễn Tưởng",
    "phieu luu":        "Phiêu Lưu",
    "adventure":        "Phiêu Lưu",
    "gia dinh":         "Gia Đình",
    "family":           "Gia Đình",
    "tam ly":           "Tâm Lý",
    "thriller":         "Hồi Hộp",
    "hoi hop":          "Hồi Hộp",
    "bi an":            "Bí Ẩn",
    "mystery":          "Bí Ẩn",
    "lich su":          "Lịch Sử",
    "chien tranh":      "Chiến Tranh",
    "sieu anh hung":    "Siêu Anh Hùng",
    "superhero":        "Siêu Anh Hùng",
}

# Trigger words yêu cầu ngữ cảnh "phim/đề xuất/…" để tránh false positive
_GENRE_TRIGGERS: list[str] = [
    "phim", "de xuat", "goi y", "co phim", "xem gi", "muon xem",
    "tim phim", "the loai", "xem phim", "chieu phim",
]


def is_genre_question(nm: str) -> Optional[str]:
    """Phát hiện câu hỏi đề xuất theo thể loại.
    Trả về tên thể loại (Vietnamese) nếu khớp, None nếu không."""
    if not any(kw in nm for kw in _GENRE_TRIGGERS):
        return None
    for key, genre in _GENRE_MAP.items():
        if key in nm:
            return genre
    return None


def _build_genre_reply(genre: str, now: datetime) -> dict:
    """Trả danh sách TẤT CẢ phim thuộc thể loại, bao gồm cả phim chưa có lịch."""
    sql = """
        SELECT m.Id, m.Title, m.Genre, m.DurationMinutes, m.Rating,
               m.ReleaseDate, m.Description, m.MainActors,
               (SELECT MIN(s.StartTime) FROM Showtimes s
                WHERE s.MovieId = m.Id AND s.StartTime >= ?) AS FirstShowtime
        FROM Movies m
        WHERE m.Genre LIKE ?
        ORDER BY FirstShowtime, m.ReleaseDate, m.Title
    """
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, now, f"%{genre}%")
            movies = cursor.fetchall()
    except Exception as e:
        return {"reply": f"Không thể tải danh sách phim: {e}", "actions": []}

    if not movies:
        return {
            "reply": f"Hiện MetaCinema chưa có phim thể loại **{genre}** nào trong hệ thống.",
            "actions": [],
        }

    lines = [f"MetaCinema có {len(movies)} phim thể loại **{genre}**:"]
    actions = []
    for m in movies:
        if m.FirstShowtime:
            time_part = f" | Suất gần nhất: {m.FirstShowtime.strftime('%d/%m %H:%M')}"
        elif m.ReleaseDate:
            rd = m.ReleaseDate.date() if isinstance(m.ReleaseDate, datetime) else m.ReleaseDate
            time_part = f" | Dự kiến chiếu: {rd.strftime('%d/%m/%Y')}"
        else:
            time_part = ""
        lines.append(f"- **{m.Title}**{time_part}")
        actions.append({
            "type": "view_movie",
            "label": f"Xem {m.Title[:22]}",
            "url": f"/Movies/Details/{m.Id}",
            "movieId": m.Id,
        })

    # Nút đặt vé cho tối đa 2 phim đang có suất
    for m in [mv for mv in movies if mv.FirstShowtime][:2]:
        next_sts = _next_showtimes_for_movie(m.Id, now, limit=2)
        actions.extend(_build_showtime_actions(next_sts))

    return {
        "reply": "\n".join(lines),
        "actions": actions[:8],
    }


def is_my_tickets_question(nm: str) -> bool:
    return any(kw in nm for kw in [
        "ve cua toi", "ve toi", "ve da dat", "lich su ve",
        "don ve", "ma ve", "qr ve", "ticket cua toi", "booking cua toi"
    ])


def is_payment_help_question(nm: str) -> bool:
    mentions_payment = any(kw in nm for kw in ["thanh toan", "payos", "qr", "chuyen khoan", "tru tien"])
    mentions_issue = any(kw in nm for kw in [
        "loi", "that bai", "khong duoc", "chua thanh toan", "thanh toan chua",
        "chua nhan ve", "khong nhan duoc ve", "khong thay ve",
        "tiep tuc", "dang cho", "cho thanh toan", "qua han", "bi tru tien"
    ])
    return mentions_payment and mentions_issue


def is_booking_guide_question(nm: str) -> bool:
    return any(kw in nm for kw in ["dat ve", "mua ve", "thanh toan", "payos", "qr", "chon ghe", "giu ghe"])


# ─────────────────────────────────────────────────────────────────────────────
#  Date/time extractors
# ─────────────────────────────────────────────────────────────────────────────
def extract_requested_date(message: str, now: datetime) -> Optional[datetime]:
    nm = normalize(message)
    today = now.date()

    if "hom nay" in nm or "toi nay" in nm or "dem nay" in nm:
        return datetime.combine(today, datetime.min.time())
    if "ngay mai" in nm or "mai" in nm:
        return datetime.combine(today + timedelta(days=1), datetime.min.time())

    # Match date patterns — skip time-like tokens (hh:mm already separated by ':')
    # Only accept day/month[/year] separated by / or -
    # Use finditer to avoid matching the first occurrence blindly
    current_year = today.year
    for match in re.finditer(r"(?<!\d)(\d{1,2})[/\-](\d{1,2})(?:[/\-](\d{2,4}))?(?!\d)", message):
        try:
            day, month = int(match.group(1)), int(match.group(2))
            year = int(match.group(3)) if match.group(3) else current_year
            if year < 100:
                year += 2000
            # Sanity check: year must be within ±2 years of current year
            if not (current_year - 1 <= year <= current_year + 2):
                continue
            # Sanity check: valid calendar date
            d = datetime(year, month, day)
            return d
        except ValueError:
            continue

    day_map = {
        "thu hai": 0, "thu 2": 0,
        "thu ba": 1, "thu 3": 1,
        "thu tu": 2, "thu 4": 2,
        "thu nam": 3, "thu 5": 3,
        "thu sau": 4, "thu 6": 4,
        "thu bay": 5, "thu 7": 5,
        "chu nhat": 6, "cn": 6,
    }
    for label, wd in day_map.items():
        if label in nm:
            days_ahead = (wd - today.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7
            return datetime.combine(today + timedelta(days=days_ahead), datetime.min.time())

    return None


# ─────────────────────────────────────────────────────────────────────────────
#  DB helpers
# ─────────────────────────────────────────────────────────────────────────────
def _format_money(price) -> str:
    try:
        return f"{int(price):,}đ".replace(",", ".")
    except Exception:
        return f"{price}đ"


def _format_room(name: str) -> str:
    if not name:
        return "Phòng chưa rõ"
    nm = normalize(name)
    return name if nm.startswith("phong") else "Phòng " + name


def _format_showtime_item(row) -> str:
    return f"{row.StartTime.strftime('%H:%M')} ({_format_room(row.RoomName)}, {_format_money(row.BasePrice)})"


def _build_showtime_actions(rows: list) -> list:
    seen = set()
    actions = []
    for row in rows:
        sid = row.ShowtimeId
        if sid in seen:
            continue
        seen.add(sid)
        actions.append({
            "type": "book_showtime",
            "label": f"Đặt vé {row.StartTime.strftime('%d/%m %H:%M')}",
            "url": f"/Booking/SelectSeat?showtimeId={sid}",
            "movieId": row.MovieId,
            "showtimeId": sid,
            "movieTitle": row.MovieTitle,
            "showtimeLabel": f"{row.StartTime.strftime('%d/%m/%Y %H:%M')} - {_format_room(row.RoomName)}",
            "roomName": row.RoomName or "",
            "price": float(row.BasePrice or 0),
        })
    return actions[:6]


def _resolve_requested_movie(nm: str):
    if not nm:
        return None

    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT Id, Title, Genre, DurationMinutes, Rating, Description,
                       ReleaseDate, Director, MainActors
                FROM Movies
                """
            )
            movies = cursor.fetchall()
    except Exception:
        return None

    best_score, best_movie = 0, None
    for movie in movies:
        title = normalize(movie.Title or "")
        if not title:
            continue

        score = 100 if title in nm else 0
        tokens = [
            t for t in re.split(r"[^a-z0-9]+", title)
            if len(t) >= 3 and t not in _MOVIE_TITLE_STOP_WORDS
        ]
        for token in set(tokens):
            if token in nm:
                score += 3 if len(token) >= 5 else 1

        if score > best_score:
            best_score, best_movie = score, movie

    return best_movie if best_score >= 3 else None


def _get_movie(movie_id: int):
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT Id, Title, Genre, DurationMinutes, Rating, Description,
                       Director, MainActors, ReleaseDate
                FROM Movies
                WHERE Id = ?
                """,
                int(movie_id),
            )
            return cursor.fetchone()
    except Exception:
        return None


def _rating_min_age(rating: str) -> Optional[int]:
    if not rating:
        return None
    nm = normalize(str(rating)).upper()
    match = re.search(r"(\d{1,2})", nm)
    if match:
        return int(match.group(1))
    if nm.startswith(("P", "G")):
        return 0
    if nm.startswith("K"):
        return 13
    return None


def _build_age_verdict(rating: str) -> str:
    min_age = _rating_min_age(rating)
    label = rating or "chưa cập nhật"
    if min_age is None:
        return f"Phân loại hiện tại là {label}, nhưng mình chưa xác định được mốc tuổi cụ thể từ dữ liệu này."
    if min_age >= 18:
        return f"Phim phân loại {label}, chỉ dành cho khán giả từ {min_age} tuổi trở lên, nên không phù hợp cho người dưới 18 tuổi."
    if min_age > 0:
        return f"Phim phân loại {label}, khán giả từ {min_age} tuổi trở lên có thể xem; người dưới {min_age} tuổi thì không phù hợp."
    return f"Phim phân loại {label}, không có giới hạn tuổi theo dữ liệu rạp đang lưu."


def _next_showtimes_for_movie(movie_id: int, now: datetime, limit: int = 3) -> list:
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT TOP 3 s.Id AS ShowtimeId, s.StartTime, s.BasePrice, s.MovieId,
                       r.Name AS RoomName, m.Title AS MovieTitle
                FROM Showtimes s
                INNER JOIN Movies m ON m.Id = s.MovieId
                LEFT  JOIN Rooms  r ON r.Id = s.RoomId
                WHERE s.MovieId = ? AND s.StartTime >= ?
                ORDER BY s.StartTime
                """,
                int(movie_id),
                now,
            )
            return cursor.fetchall()[:limit]
    except Exception:
        return []


def _build_movie_age_reply(message: str, page_context: dict, now: datetime) -> Optional[dict]:
    nm = expand_synonyms(normalize(message))
    if not is_movie_age_question(nm):
        return None

    movie = None
    page_movie_id = page_context.get("movieId") if page_context else None
    if page_movie_id and not asks_global_movie_list(nm):
        movie = _get_movie(int(page_movie_id))
    if movie is None:
        movie = _resolve_requested_movie(nm)
    if movie is None:
        return None

    next_showtimes = _next_showtimes_for_movie(movie.Id, now)
    lines = [
        f"{movie.Title}",
        f"- Phân loại: {movie.Rating or 'Chưa cập nhật'}",
        f"- {_build_age_verdict(movie.Rating or '')}",
    ]
    if movie.Genre or movie.DurationMinutes:
        lines.append(f"- Thể loại: {movie.Genre or 'Chưa cập nhật'} | Thời lượng: {movie.DurationMinutes or 'Chưa cập nhật'} phút")
    if next_showtimes:
        lines.append("- Suất gần nhất: " + "; ".join(
            f"{s.StartTime.strftime('%d/%m %H:%M')} ({_format_room(s.RoomName)})"
            for s in next_showtimes
        ))

    actions = [{
        "type": "view_movie",
        "label": "Xem chi tiết phim",
        "url": f"/Movies/Details/{movie.Id}",
        "movieId": movie.Id,
    }]
    actions.extend(_build_showtime_actions(next_showtimes))
    return {
        "reply": "\n".join(lines),
        "actions": actions,
        "session_update": {"last_movie_id": movie.Id, "last_movie_title": movie.Title},
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Upcoming release — fallback khi phim chưa có suất chiếu nhưng có ReleaseDate
# ─────────────────────────────────────────────────────────────────────────────
def _build_upcoming_release_reply(movie_id: Optional[int], nm: str,
                                   now: datetime) -> Optional[dict]:
    """Khi không có Showtime nào, kiểm tra Movies.ReleaseDate.
    Dùng cho trường hợp: phim sắp chiếu, mục 'Sắp chiếu', câu hỏi 'khi nào chiếu?'
    """
    try:
        with get_conn() as conn:
            cursor = conn.cursor()

            if movie_id:
                # Đến từ trang chi tiết phim — biết chính xác ID
                cursor.execute(
                    "SELECT Id, Title, Genre, DurationMinutes, Rating, "
                    "       ReleaseDate, Director "
                    "FROM Movies WHERE Id = ?",
                    int(movie_id),
                )
                movie = cursor.fetchone()
            else:
                # Tìm theo tên trong câu hỏi
                movie = _resolve_requested_movie(nm)

        if not movie:
            return None

        release = getattr(movie, "ReleaseDate", None)
        if not release:
            return None

        from datetime import date as date_type
        release_date = release.date() if isinstance(release, datetime) else release
        today = now.date()

        lines = [f"Phim **{movie.Title}** chưa có suất chiếu cụ thể tại MetaCinema."]

        if release_date > today:
            delta = (release_date - today).days
            if delta == 1:
                countdown = "ngày mai"
            elif delta <= 7:
                countdown = f"còn {delta} ngày nữa"
            elif delta <= 30:
                countdown = f"còn khoảng {delta // 7} tuần nữa"
            else:
                countdown = f"còn khoảng {(delta + 15) // 30} tháng nữa"
            lines.append(
                f"- Dự kiến khởi chiếu: **{release_date.strftime('%d/%m/%Y')}** ({countdown})."
            )
        else:
            lines.append(
                f"- Ngày khởi chiếu: {release_date.strftime('%d/%m/%Y')} "
                "— lịch suất chưa được cập nhật trên hệ thống."
            )

        genre = getattr(movie, "Genre", None)
        duration = getattr(movie, "DurationMinutes", None)
        director = getattr(movie, "Director", None)
        if genre:
            lines.append(f"- Thể loại: {genre}.")
        if duration:
            lines.append(f"- Thời lượng: {duration} phút.")
        if director:
            lines.append(f"- Đạo diễn: {director}.")
        lines.append("Theo dõi trang phim để cập nhật lịch chiếu sớm nhất!")

        mid = getattr(movie, "Id", None) or movie_id
        return {
            "reply": "\n".join(lines),
            "actions": [{
                "type": "view_movie",
                "label": f"Xem chi tiết {movie.Title[:20]}",
                "url": f"/Movies/Details/{mid}",
                "movieId": mid,
            }],
        }
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
#  Showtime reply
# ─────────────────────────────────────────────────────────────────────────────
def _build_showtime_reply(message: str, page_context: dict, now: datetime) -> Optional[dict]:
    nm = expand_synonyms(normalize(message))
    requested_date = extract_requested_date(message, now)
    page_movie_id = page_context.get("movieId") if page_context else None

    if not is_showtime_question(nm) and not (requested_date and page_movie_id):
        return None

    global_list = asks_global_movie_list(nm)

    sql = """
        SELECT s.Id AS ShowtimeId, s.StartTime, s.BasePrice, s.MovieId,
               r.Name AS RoomName, m.Title AS MovieTitle
        FROM Showtimes s
        INNER JOIN Movies m ON m.Id = s.MovieId
        LEFT  JOIN Rooms  r ON r.Id = s.RoomId
        WHERE 1=1
    """
    params = []

    if page_movie_id and not global_list:
        sql += " AND s.MovieId = ?"
        params.append(int(page_movie_id))

    if requested_date:
        day_start = requested_date.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        sql += " AND s.StartTime >= ? AND s.StartTime < ?"
        params += [day_start, day_end]
    else:
        week_end = datetime.combine(now.date() + timedelta(days=8), datetime.min.time())
        sql += " AND s.StartTime >= ? AND s.StartTime < ?"
        params += [now, week_end]

    sql += " ORDER BY s.StartTime"

    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, *params)
            rows = cursor.fetchall()
    except Exception as e:
        return {"reply": f"Không thể truy vấn lịch chiếu: {e}", "actions": []}

    if not rows:
        # Trước khi báo không tìm thấy — kiểm tra phim có ReleaseDate không (sắp chiếu)
        upcoming = _build_upcoming_release_reply(page_movie_id, nm, now)
        if upcoming:
            return upcoming
        return {
            "reply": "Không tìm thấy suất chiếu phù hợp. Bạn thử hỏi ngày khác hoặc gọi 0799010072 nhé.",
            "actions": [],
        }

    # Detect movie name mentioned in message (when not already on a movie page)
    if not page_movie_id and not global_list:
        seen_titles: dict[int, str] = {}
        for r in rows:
            seen_titles.setdefault(r.MovieId, r.MovieTitle)
        best_score, best_mid = 0, None
        for mid, title in seen_titles.items():
            nm_title = normalize(title)
            score = 100 if nm_title in nm else 0
            tokens = [t for t in re.split(r"[^a-z0-9]+", nm_title) if len(t) >= 3]
            score += sum(10 if len(t) >= 5 else 3 for t in tokens if t in nm)
            if score > best_score:
                best_score, best_mid = score, mid
        # Threshold: full match (100), one long token (10), or two short tokens (6)
        if best_score >= 6:
            page_movie_id = best_mid
            rows = [r for r in rows if r.MovieId == best_mid]

    date_label = requested_date.strftime("ngày %d/%m/%Y") if requested_date else "hiện tại"

    if page_movie_id and not global_list and rows:
        title = rows[0].MovieTitle
        times = "; ".join(_format_showtime_item(r) for r in rows)
        reply = f"Có. {date_label}, phim {title} có suất: {times}. Bạn có thể bấm nhanh một suất bên dưới để chọn ghế."
        return {
            "reply": reply,
            "actions": _build_showtime_actions(rows),
            # Store resolved movie so follow-up questions ("phim đó…") work
            "session_update": {"last_movie_id": page_movie_id, "last_movie_title": title},
        }

    by_movie = defaultdict(list)
    for r in rows:
        by_movie[r.MovieId].append(r)

    lines = [f"Có. {date_label} hiện có các suất chiếu:"]
    for mid, showtimes in list(by_movie.items())[:8]:
        title = showtimes[0].MovieTitle
        times = "; ".join(_format_showtime_item(r) for r in showtimes)
        lines.append(f"- {title}: {times}")
    lines.append("Bạn chọn phim hoặc bấm nhanh một suất bên dưới để đặt ghế.")

    return {"reply": "\n".join(lines), "actions": _build_showtime_actions(rows[:8])}


# ─────────────────────────────────────────────────────────────────────────────
#  Seat status reply
# ─────────────────────────────────────────────────────────────────────────────
def _build_seat_status_reply(message: str, now: datetime) -> dict:
    nm = normalize(message)
    requested_date = extract_requested_date(message, now)

    sql = """
        SELECT s.Id AS ShowtimeId, s.StartTime, s.BasePrice, s.MovieId,
               r.Name AS RoomName, r.SeatRows, r.SeatCols,
               m.Title AS MovieTitle
        FROM Showtimes s
        INNER JOIN Movies m ON m.Id = s.MovieId
        LEFT  JOIN Rooms  r ON r.Id = s.RoomId
        WHERE s.StartTime >= ? AND s.StartTime < ?
        ORDER BY s.StartTime
    """
    if requested_date:
        start = requested_date.replace(hour=0, minute=0, second=0)
        end = start + timedelta(days=1)
    else:
        start = now
        end = datetime.combine(now.date() + timedelta(days=15), datetime.min.time())

    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, start, end)
            showtimes = cursor.fetchall()

            if not showtimes:
                return {
                    "reply": 'Mình chưa tìm thấy suất chiếu phù hợp để kiểm tra ghế trống. '
                             'Bạn gửi giúp tên phim + ngày + giờ, ví dụ: "Conan 30/12 10:25 còn ghế không".',
                    "actions": []
                }

            # Get sold seats for these showtimes
            showtime_ids = [s.ShowtimeId for s in showtimes]
            placeholders = ",".join("?" * len(showtime_ids))
            seat_sql = f"""
                SELECT bs.SeatCode, b.ShowtimeId
                FROM BookingSeats bs
                INNER JOIN Bookings b ON b.Id = bs.BookingId
                WHERE b.ShowtimeId IN ({placeholders})
                  AND b.PaymentStatus IN ('Paid', 'PendingPayment', 'CheckedIn')
            """
            cursor.execute(seat_sql, *showtime_ids)
            sold_map: dict[int, set] = {}
            for row in cursor.fetchall():
                sold_map.setdefault(row.ShowtimeId, set()).add(row.SeatCode.strip().upper())

            # Build inventory per showtime
            results = []
            for s in showtimes[:6]:
                rows = s.SeatRows or 8
                cols = s.SeatCols or 12
                total = rows * cols
                sold_count = len(sold_map.get(s.ShowtimeId, set()))
                available = total - sold_count
                results.append((s, total, sold_count, available))

            if len(results) == 1:
                s, total, sold_count, available = results[0]
                label = f"{s.MovieTitle} - {s.StartTime.strftime('%d/%m/%Y %H:%M')} ({_format_room(s.RoomName)})"
                if available > 0:
                    reply = (f"Suất {label}:\n"
                             f"- Còn {available}/{total} ghế trống.\n"
                             f"- Lưu ý: số ghế thực có thể thay đổi do đang có khách giữ tạm thời.")
                else:
                    reply = f"Suất {label}:\n- Hiện không còn ghế trống để đặt online."
                actions = _build_showtime_actions([s]) if available > 0 else []
                return {"reply": reply, "actions": actions}

            lines = ["Mình tìm thấy vài suất phù hợp. Tình trạng ghế hiện tại:"]
            bookable = []
            for s, total, sold_count, available in results:
                label = f"{s.MovieTitle} {s.StartTime.strftime('%d/%m %H:%M')}"
                lines.append(f"- {label}: còn {available}/{total} ghế trống")
                if available > 0:
                    bookable.append(s)
            return {"reply": "\n".join(lines), "actions": _build_showtime_actions(bookable)}

    except Exception as e:
        return {"reply": f"Không thể kiểm tra ghế lúc này: {e}", "actions": []}


# ─────────────────────────────────────────────────────────────────────────────
#  Specific movie info reply — trả lời câu hỏi về thông tin 1 phim cụ thể
# ─────────────────────────────────────────────────────────────────────────────
def _build_specific_movie_info_reply(movie, now: datetime) -> dict:
    """Trả thông tin chi tiết (thể loại, đạo diễn, diễn viên…) cho 1 phim cụ thể."""
    lines = [f"**{movie.Title}**"]
    if movie.Genre:
        lines.append(f"- Thể loại: {movie.Genre}")
    if movie.DurationMinutes:
        lines.append(f"- Thời lượng: {movie.DurationMinutes} phút")
    if movie.Rating:
        lines.append(f"- Phân loại: {movie.Rating}")
    director = getattr(movie, "Director", None)
    if director:
        lines.append(f"- Đạo diễn: {director}")
    main_actors = getattr(movie, "MainActors", None)
    if main_actors:
        lines.append(f"- Diễn viên chính: {main_actors}")

    next_showtimes = _next_showtimes_for_movie(movie.Id, now)
    if next_showtimes:
        lines.append("- Suất gần nhất: " + "; ".join(
            f"{s.StartTime.strftime('%d/%m %H:%M')} ({_format_room(s.RoomName)})"
            for s in next_showtimes
        ))

    actions = [{
        "type": "view_movie",
        "label": f"Xem chi tiết {movie.Title[:20]}",
        "url": f"/Movies/Details/{movie.Id}",
        "movieId": movie.Id,
    }]
    actions.extend(_build_showtime_actions(next_showtimes))
    return {
        "reply": "\n".join(lines),
        "actions": actions,
        "session_update": {"last_movie_id": movie.Id, "last_movie_title": movie.Title},
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Movie reply — danh sách phim đang chiếu (câu hỏi chung)
# ─────────────────────────────────────────────────────────────────────────────
def _build_movie_reply(now: datetime) -> dict:
    sql = """
        SELECT TOP 5 m.Id, m.Title, m.Genre, m.DurationMinutes, m.Rating, m.Description,
               MIN(s.StartTime) AS FirstShowtime
        FROM Movies m
        INNER JOIN Showtimes s ON s.MovieId = m.Id
        WHERE s.StartTime >= ?
        GROUP BY m.Id, m.Title, m.Genre, m.DurationMinutes, m.Rating, m.Description
        ORDER BY MIN(s.StartTime)
    """
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, now)
            movies = cursor.fetchall()

        if not movies:
            return {
                "reply": "Hiện rạp chưa có phim đang chiếu trong dữ liệu. Bạn quay lại sau hoặc gọi 0799010072.",
                "actions": []
            }

        lines = ["Một vài phim đang có lịch chiếu tại Meta Cinema:"]
        actions = []
        for m in movies:
            lines.append(f"- {m.Title} | {m.Genre or 'Chưa cập nhật'} | Suất gần nhất: {m.FirstShowtime.strftime('%d/%m %H:%M')}")
            actions.append({
                "type": "view_movie",
                "label": f"Xem {m.Title[:24]}",
                "url": f"/Movies/Details/{m.Id}",
                "movieId": m.Id,
            })
        return {"reply": "\n".join(lines), "actions": actions}
    except Exception as e:
        return {"reply": f"Không thể tải danh sách phim: {e}", "actions": []}


# ─────────────────────────────────────────────────────────────────────────────
#  Food reply
# ─────────────────────────────────────────────────────────────────────────────
def _build_food_reply(nm: str) -> dict:
    sql = "SELECT Name, Category, Price, Description FROM FoodAndDrinks ORDER BY Category, Name"
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(sql)
            items = cursor.fetchall()

        if not items:
            return {
                "reply": "Hiện rạp chưa cập nhật menu đồ ăn/thức uống. Bạn có thể hỏi nhân viên tại quầy.",
                "actions": []
            }

        matched = [f for f in items if any(
            token in nm for token in normalize(f.Name).split() if len(token) >= 3
        ) or (f.Category and normalize(f.Category) in nm)]

        target = matched[:8] if matched else items[:10]

        by_cat = defaultdict(list)
        for f in target:
            by_cat[f.Category or "Khác"].append(f)

        lines = ["Mình tìm thấy các món phù hợp:" if matched else "Menu đồ ăn/thức uống hiện có:"]
        for cat, foods in by_cat.items():
            lines.append(f"{cat}:")
            for f in foods:
                lines.append(f"- {f.Name}: {_format_money(f.Price)}")
        lines.append("Bạn có thể thêm đồ ăn sau khi chọn ghế trong luồng đặt vé.")

        return {
            "reply": "\n".join(lines),
            "actions": [{"type": "open_url", "label": "Xem phim để đặt vé", "url": "/?filter=now"}]
        }
    except Exception as e:
        return {"reply": f"Không thể tải menu: {e}", "actions": []}


# ─────────────────────────────────────────────────────────────────────────────
#  Policy reply
# ─────────────────────────────────────────────────────────────────────────────
_promo_cache: tuple[float, list] = (0.0, [])
_PROMO_TTL = 300  # 5 minutes


def _get_cached_promotions() -> list:
    global _promo_cache
    ts, items = _promo_cache
    if _time.monotonic() - ts < _PROMO_TTL:
        return items
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT Title, Content FROM ChatbotKnowledge WHERE IsActive=1 ORDER BY SortOrder, Id"
            )
            items = cursor.fetchall()
        _promo_cache = (_time.monotonic(), items)
    except Exception:
        pass  # return stale data on failure
    return items


def _build_policy_reply(nm: str) -> dict:
    if any(kw in nm for kw in ["hoan", "doi suat", "huy ve"]):
        return {"reply": "Vé đã thanh toán không hoàn tiền. Nếu cần đổi suất, liên hệ hotline 0799010072 trước giờ chiếu ít nhất 2 giờ.", "actions": []}
    if any(kw in nm for kw in ["tuoi", "c18", "c16"]):
        return {"reply": "Phim C18 chỉ dành cho khách từ 18 tuổi, phim C16 dành cho khách từ 16 tuổi. Rạp có thể yêu cầu CMND/CCCD khi kiểm tra vé.", "actions": []}
    if any(kw in nm for kw in ["do an", "mang", "nuoc"]):
        return {"reply": "Rạp không hỗ trợ mang đồ ăn/thức uống từ ngoài vào phòng chiếu. Bạn có thể mua bắp, nước hoặc combo tại quầy/luồng đặt vé.", "actions": []}
    if any(kw in nm for kw in ["dia chi", "o dau"]):
        return {"reply": "Meta Cinema ở 06 Trần Văn Ơn, phường Phú Lợi, TP. Hồ Chí Minh. Hotline: 0799010072.", "actions": []}
    if any(kw in nm for kw in ["khuyen mai", "uu dai", "giam gia"]):
        items = _get_cached_promotions()
        promotions = [k for k in items if any(
            kw in normalize(k.Title + " " + k.Content) for kw in ["khuyen mai", "uu dai", "giam"]
        )][:4]
        if promotions:
            lines = ["Khuyến mãi/ưu đãi hiện có:"] + [f"- {k.Title}: {k.Content}" for k in promotions]
            return {"reply": "\n".join(lines), "actions": []}
    return {"reply": "Với chính sách cụ thể, bạn có thể gọi hotline 0799010072 để được rạp xác nhận nhanh nhất.", "actions": []}


# ─────────────────────────────────────────────────────────────────────────────
#  My tickets reply (requires user_id)
# ─────────────────────────────────────────────────────────────────────────────
def _build_my_tickets_reply(user_id: Optional[int]) -> dict:
    if not user_id:
        return {
            "reply": "Bạn cần đăng nhập để xem vé đã đặt. Vào website hoặc app MetaCinema và đăng nhập để xem lịch sử vé nhé.",
            "actions": [{"type": "open_url", "label": "Đăng nhập", "url": "/User/Login"}]
        }
    sql = """
        SELECT TOP 5 b.Id, b.PaymentStatus, b.GrandTotal, b.CreatedAt,
               s.StartTime, m.Title AS MovieTitle
        FROM Bookings b
        INNER JOIN Showtimes s ON s.Id = b.ShowtimeId
        INNER JOIN Movies m ON m.Id = s.MovieId
        WHERE b.UserId = ? AND b.PaymentStatus IN ('Paid', 'CheckedIn')
        ORDER BY b.CreatedAt DESC
    """
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, user_id)
            bookings = cursor.fetchall()
        if not bookings:
            return {
                "reply": "Bạn chưa có vé nào đã đặt hoặc thanh toán thành công.",
                "actions": [{"type": "open_url", "label": "Xem phim đang chiếu", "url": "/?filter=now"}]
            }
        lines = ["Vé đã đặt gần đây của bạn:"]
        actions = []
        for b in bookings:
            status = "Đã check-in" if b.PaymentStatus == "CheckedIn" else "Đã thanh toán"
            lines.append(f"- {b.MovieTitle} | {b.StartTime.strftime('%d/%m/%Y %H:%M')} | {_format_money(b.GrandTotal)} | {status}")
            actions.append({"type": "open_url", "label": f"Xem vé #{b.Id}", "url": f"/Booking/MyTicket/{b.Id}"})
        return {"reply": "\n".join(lines), "actions": actions[:3]}
    except Exception as e:
        return {"reply": f"Không thể tải vé: {e}", "actions": []}


# ─────────────────────────────────────────────────────────────────────────────
#  Payment help reply
# ─────────────────────────────────────────────────────────────────────────────
_PAYMENT_GENERIC_REPLY = (
    "Một số lý do thanh toán có thể gặp vấn đề:\n"
    "1. QR code PayOS hết hạn (quá 15 phút) → đặt lại từ đầu.\n"
    "2. Ghế đã được người khác đặt trong lúc bạn thanh toán → chọn ghế khác.\n"
    "3. Đã bị trừ tiền nhưng chưa nhận vé → chờ 5-10 phút rồi kiểm tra mục 'Vé của tôi'."
    " Nếu vẫn chưa có, gọi hotline 0799010072 kèm thông tin giao dịch."
)

_PAYMENT_STATUS_LABELS = {
    "Paid":           "✅ Đã thanh toán",
    "CheckedIn":      "✅ Đã check-in",
    "PendingPayment": "⏳ Đang chờ thanh toán",
    "PendingSelect":  "⏳ Chưa hoàn tất",
    "Cancelled":      "❌ Đã huỷ",
}


def _build_payment_help_reply(user_id: Optional[int] = None) -> dict:
    """Hiển thị giao dịch 24h gần nhất của user (nếu đăng nhập) + hướng dẫn xử lý."""
    if user_id:
        sql = """
            SELECT TOP 5 b.Id, b.PaymentStatus, b.GrandTotal, b.CreatedAt,
                   m.Title AS MovieTitle, s.StartTime
            FROM Bookings b
            INNER JOIN Showtimes s ON s.Id = b.ShowtimeId
            INNER JOIN Movies    m ON m.Id = s.MovieId
            WHERE b.UserId = ?
              AND b.CreatedAt >= ?
            ORDER BY b.CreatedAt DESC
        """
        cutoff_utc = datetime.utcnow() - timedelta(days=7)
        try:
            with get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(sql, user_id, cutoff_utc)
                rows = cursor.fetchall()
            if rows:
                lines = ["Giao dịch gần đây của bạn (7 ngày qua):"]
                actions = []
                for r in rows:
                    label = _PAYMENT_STATUS_LABELS.get(r.PaymentStatus, r.PaymentStatus)
                    lines.append(
                        f"- {r.MovieTitle} | {r.StartTime.strftime('%d/%m %H:%M')}"
                        f" | {_format_money(r.GrandTotal)} | {label}"
                    )
                    if r.PaymentStatus in ("Paid", "CheckedIn"):
                        actions.append({"type": "open_url", "label": f"Xem vé #{r.Id}",
                                        "url": f"/Booking/MyTicket/{r.Id}"})
                lines += ["", _PAYMENT_GENERIC_REPLY]
                return {
                    "reply": "\n".join(lines),
                    "actions": actions or [{"type": "open_url", "label": "Xem vé của tôi",
                                            "url": "/Booking/MyTickets"}],
                }
        except Exception:
            pass  # fall through to generic reply

    return {
        "reply": _PAYMENT_GENERIC_REPLY,
        "actions": [{"type": "open_url", "label": "Xem vé của tôi", "url": "/Booking/MyTickets"}],
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Booking guide reply
# ─────────────────────────────────────────────────────────────────────────────
def _build_booking_guide_reply(page_context: dict, now: datetime) -> dict:
    movie_id = page_context.get("movieId") if page_context else None
    actions = []
    if movie_id:
        sql = """
            SELECT TOP 3 s.Id AS ShowtimeId, s.StartTime, s.BasePrice, s.MovieId,
                   r.Name AS RoomName, m.Title AS MovieTitle
            FROM Showtimes s
            INNER JOIN Movies m ON m.Id = s.MovieId
            LEFT  JOIN Rooms  r ON r.Id = s.RoomId
            WHERE s.MovieId = ? AND s.StartTime >= ?
            ORDER BY s.StartTime
        """
        try:
            with get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(sql, int(movie_id), now)
                rows = cursor.fetchall()
            actions = _build_showtime_actions(rows)
        except Exception:
            pass
    if not actions:
        actions = [{"type": "open_url", "label": "Xem phim đang chiếu", "url": "/?filter=now"}]

    return {
        "reply": (
            "Để đặt vé: chọn phim, chọn ngày và suất chiếu, chọn ghế, "
            "thêm đồ ăn nếu muốn, sau đó thanh toán PayOS bằng QR/chuyển khoản. "
            "Sau khi thanh toán, bạn dùng mã QR vé điện tử để vào rạp."
        ),
        "actions": actions
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Main entry point — called from main.py before Gemini fallback
# ─────────────────────────────────────────────────────────────────────────────
def try_build_routed_reply(message: str, page_context: dict, user_id: Optional[int], now: datetime) -> Optional[dict]:
    nm = expand_synonyms(normalize(message))

    # Resolve pronoun references ("phim đó", "phim này"…) using session context
    if any(kw in nm for kw in _MOVIE_REF_WORDS):
        last_id = page_context.get("last_movie_id")
        if last_id and not page_context.get("movieId"):
            page_context = {**page_context, "movieId": last_id}

    # Seat availability (must check before showtime to avoid overlap)
    if is_seat_status_question(nm):
        logger.info("intent=seat_status user=%s", user_id)
        return _build_seat_status_reply(message, now)

    movie_age_reply = _build_movie_age_reply(message, page_context, now)
    if movie_age_reply is not None:
        logger.info("intent=movie_age user=%s", user_id)
        return movie_age_reply

    # Booking guide takes priority over showtime when user asks "how to" buy tickets
    # (prevents "hướng dẫn đặt vé" from being captured by showtime detector first)
    if is_booking_guide_question(nm) and any(kw in nm for kw in _GUIDANCE_HINTS):
        logger.info("intent=booking_guide user=%s", user_id)
        return _build_booking_guide_reply(page_context, now)

    # Showtime schedule
    showtime_reply = _build_showtime_reply(message, page_context, now)
    if showtime_reply is not None:
        logger.info("intent=showtime user=%s", user_id)
        return showtime_reply

    # My tickets
    if is_my_tickets_question(nm):
        logger.info("intent=my_tickets user=%s", user_id)
        return _build_my_tickets_reply(user_id)

    # Payment issues
    if is_payment_help_question(nm):
        logger.info("intent=payment_help user=%s", user_id)
        return _build_payment_help_reply(user_id)

    # Policy / promotions / address
    if is_policy_question(nm):
        logger.info("intent=policy user=%s", user_id)
        return _build_policy_reply(nm)

    # Character / actor search — "X là nhân vật trong phim nào?"
    # Phải chạy TRƯỚC is_movie_question để tránh character query rơi vào danh sách phim chung
    if is_character_search_question(nm):
        char_reply = _build_character_search_reply(message, now)
        if char_reply is not None:
            logger.info("intent=character_search user=%s", user_id)
            return char_reply
        # Không có trong DB (ví dụ: nhân vật anime) → để Gemini trả lời
        logger.info("intent=gemini_fallback(character) user=%s msg=%.60r", user_id, message)
        return None

    # Genre filter — "đề xuất phim Hoạt Hình", "phim Action", …
    # Chạy TRƯỚC is_movie_question để câu hỏi thể loại không rơi vào danh sách chung
    genre = is_genre_question(nm)
    if genre is not None:
        logger.info("intent=genre_filter genre=%s user=%s", genre, user_id)
        return _build_genre_reply(genre, now)

    # Movie info / list
    if is_movie_question(nm):
        # Nếu câu hỏi không phải dạng "liệt kê toàn bộ" thì thử resolve phim cụ thể trước
        if not asks_global_movie_list(nm):
            page_movie_id = page_context.get("movieId") if page_context else None
            specific = (
                _get_movie(int(page_movie_id)) if page_movie_id else None
            ) or _resolve_requested_movie(nm)
            if specific:
                logger.info("intent=movie_info_specific user=%s movie=%s", user_id, specific.Title)
                return _build_specific_movie_info_reply(specific, now)
        logger.info("intent=movie_info_list user=%s", user_id)
        return _build_movie_reply(now)

    # Food / menu
    if is_food_question(nm):
        logger.info("intent=food user=%s", user_id)
        return _build_food_reply(nm)

    # Catch-all for booking-guide questions that lack _GUIDANCE_HINTS keywords (e.g. bare "đặt vé")
    if is_booking_guide_question(nm):
        logger.info("intent=booking_guide user=%s", user_id)
        return _build_booking_guide_reply(page_context, now)

    logger.info("intent=gemini_fallback user=%s msg=%.60r", user_id, message)
    return None  # falls through to Gemini
