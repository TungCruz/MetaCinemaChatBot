"""
Gemini API call — port of ChatController.cs Send() Gemini section
"""
from datetime import datetime
from typing import List
import google.generativeai as genai

_VIETNAM_OFFSET = 7  # UTC+7

_DAY_NAMES = {
    0: "Thứ Hai", 1: "Thứ Ba", 2: "Thứ Tư",
    3: "Thứ Năm", 4: "Thứ Sáu", 5: "Thứ Bảy", 6: "Chủ Nhật",
}


def _day_vn(weekday: int) -> str:
    return _DAY_NAMES.get(weekday, "")


def _now_vn() -> datetime:
    from datetime import timezone, timedelta
    tz = timezone(timedelta(hours=_VIETNAM_OFFSET))
    return datetime.now(tz)


_PROJECT_LOGIC_CONTEXT = """## PROJECT LOGIC KNOWLEDGE (luồng nghiệp vụ nội bộ)
### Seat lock / chống bán trùng ghế
- Ghế hợp lệ để đặt là Seat.IsActive=true và không nằm trong booking Paid/PendingPayment/CheckedIn, cũng không có active lock.
- Seat lock lưu theo showtimeId + seatCode + userToken trong BookingController; lock hết hạn được dọn khi đọc/ghi lock.
- Stage Select giữ ghế khoảng 10 phút; Stage Payment giữ/gia hạn khoảng 15 phút.
- Hàm logic chính: TryLockSeatsForClient, TryExtendLocksForClient, ReleaseSeatLocksForClient, AdoptSeatLocksForClient, GetSeatLocksForShowtime.
### Booking / checkout
- Luồng đặt vé: chọn suất → lock ghế → chọn bắp nước → Checkout → lưu PendingPayment booking → tạo PayOS link → PayOSReturn chuyển Paid.
- BookingSeats lưu ghế, BookingConcessions lưu đồ ăn/nước, Booking.GrandTotal = vé + concession.
- PendingSelect/PendingPayment quá hạn thì không xem là vé hợp lệ; Paid/CheckedIn là trạng thái vé đã thanh toán.
### PayOS
- PayOS tạo checkoutUrl theo orderCode; project lưu mapping PayOSBooking_{orderCode}, PayOSOrder_{bookingId}, PayOSLink_{bookingId} trong session.
- PayOSReturn xác minh trạng thái và cập nhật booking; PayOSCancel xử lý hủy/thất bại.
- Chatbot đọc dữ liệu booking/transaction đã lưu; không tự gọi PayOS live nếu chưa có yêu cầu/module riêng.
### QR / check-in vé
- Mã vé dạng BKG-xxxxxx; QR chứa ticket code, showtime, seats, total.
- Staff ValidateTicket chỉ chấp nhận Paid/CheckedIn; Paid lần đầu sẽ đổi sang CheckedIn, CheckedIn thì báo vé đã sử dụng.
### Staff sale / bán vé tại quầy
- Staff bán trực tiếp tạo Booking Paid, thường UserId=null, kèm BookingSeats/BookingConcessions.
- Trước khi bán, hệ thống kiểm tra ghế đã Paid/PendingPayment/CheckedIn hoặc đang lock để tránh conflict.
### Admin tạo suất chiếu bằng chatbot
- Chỉ Admin được tạo suất. Bot cần đủ phim, ngày, giờ HH:mm, phòng, giá vé.
- Bot chặn thời gian quá khứ và trùng phòng theo duration phim + khoảng cách 30 phút.
- Bulk Showtime Scheduler hỗ trợ tạo nhiều suất: tất cả phim đang chiếu, X ngày tới, N suất/ngày, tự phân bổ phòng/giờ còn trống.
- Với bulk create, bot chỉ lưu kế hoạch vào bộ nhớ trước; Admin phải nhắn "xác nhận tạo suất" thì mới ghi DB."""


def build_system_prompt(movie_context: str, food_context: str, knowledge_context: str,
                        page_note: str = "") -> str:
    now = _now_vn()
    day_name = _day_vn(now.weekday())
    date_str = now.strftime("%d/%m/%Y")
    time_str = now.strftime("%H:%M")

    knowledge_section = knowledge_context if knowledge_context else ""
    page_note_section = f"\n## NGỮ CẢNH NGƯỜI DÙNG\n{page_note}\n" if page_note else ""

    return f"""Bạn là **Meta Bot** — trợ lý AI chính thức của rạp chiếu phim **Meta Cinema**. Bạn thân thiện, nhiệt tình và am hiểu điện ảnh.

## THÔNG TIN RẠP META CINEMA
- Địa chỉ: 06 Trần Văn Ơn, phường Phú Lợi, TP. Hồ Chí Minh
- Hotline: **0799010072** (hỗ trợ 8:00–22:00 hàng ngày)
- Website đặt vé: http://metacinema.somee.com
- App: MetaCinema (Android)

## HƯỚNG DẪN ĐẶT VÉ
1. Vào website hoặc mở app MetaCinema
2. Chọn phim → chọn ngày và suất chiếu → chọn ghế
3. Thêm đồ ăn/thức uống nếu muốn (combo rẻ hơn mua lẻ)
4. Thanh toán qua **PayOS** (QR code / chuyển khoản ngân hàng)
5. Nhận mã vé điện tử — xuất trình QR tại cửa rạp

## QUY ĐỊNH QUAN TRỌNG
- Ghế giữ tối đa **15 phút** sau khi chọn; quá hạn tự hủy
- Vé đã thanh toán **không hoàn tiền**; đổi suất liên hệ hotline trước 2 giờ chiếu
- Phim **C18**: từ 18 tuổi (cần CMND/CCCD) | Phim **C16**: từ 16 tuổi
- Trẻ dưới 3 tuổi: vào miễn phí, không chiếm ghế
- Không mang đồ ăn/thức uống từ ngoài vào phòng chiếu

## THỜI ĐIỂM HIỆN TẠI
**{day_name}, {date_str} — {time_str} giờ Việt Nam**

---
{movie_context}
---
{food_context}
---
{_PROJECT_LOGIC_CONTEXT}
---
{knowledge_section}
---
{page_note_section}
## HƯỚNG DẪN TRẢ LỜI
- Luôn dùng **tiếng Việt**, giọng thân thiện, súc tích
- **"Hôm nay/tối nay có phim gì?" hay "còn suất nào không?"** → xem mục SUẤT CHIẾU HÔM NAY, liệt kê các suất còn lại kèm thời gian đếm ngược đã tính sẵn trong ngoặc
- **"Còn bao lâu/bao nhiêu phút nữa tới suất [giờ]?"** → đọc thẳng giá trị thời gian đếm ngược trong dữ liệu
- **"[Tên phim] chiếu lúc mấy giờ?"** → liệt kê suất hôm nay trước, sau đó ngày mai, kèm giá vé
- **"Phim nào hay/đang chiếu?"** → gợi ý 2–3 phim kèm mô tả hấp dẫn ngắn và suất chiếu gần nhất
- **Hỏi đặt vé** → hướng dẫn từng bước rõ ràng hoặc gửi link http://metacinema.somee.com
- **Hỏi đồ ăn/combo** → dùng đúng menu được cung cấp, đề xuất combo tiết kiệm
- **Hỏi khuyến mãi/chính sách/thông tin rạp** → ưu tiên dùng mục KIẾN THỨC BỔ SUNG phía trên
- Câu hỏi ngoài phạm vi rạp phim → lịch sự hướng về chủ đề phim/rạp
- Không có thông tin cụ thể → mời khách gọi **0799010072**
- **Không bịa thông tin**; chỉ dùng dữ liệu trong prompt này
- Suất đã qua hoàn toàn (kết thúc) → không giới thiệu; suất đang chiếu → vẫn thông báo nhưng nói rõ đang chiếu"""


def call_gemini(api_key: str, system_prompt: str, history: list, message: str) -> str:
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(
        model_name="gemini-2.5-flash-lite",
        system_instruction=system_prompt,
        generation_config=genai.types.GenerationConfig(
            temperature=0.35,
            max_output_tokens=1000,
            top_p=0.9,
        ),
        safety_settings=[
            {"category": "HARM_CATEGORY_HARASSMENT",        "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH",       "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
        ],
    )

    gemini_history = []
    for item in history:
        role = "model" if item.role == "assistant" else "user"
        gemini_history.append({"role": role, "parts": [item.content]})

    try:
        chat = model.start_chat(history=gemini_history)
        response = chat.send_message(message)

        if not response.candidates:
            return "Xin lỗi, tôi không thể trả lời câu đó. Bạn có thể hỏi về phim, lịch chiếu hoặc cách đặt vé không?"

        candidate = response.candidates[0]
        finish_reason = str(candidate.finish_reason)
        if "SAFETY" in finish_reason:
            return "Xin lỗi, tôi không thể trả lời câu đó. Bạn có thể hỏi về phim, lịch chiếu hoặc cách đặt vé không?"

        reply = candidate.content.parts[0].text.strip() if candidate.content.parts else ""
        return reply or "Không có dữ liệu phù hợp."

    except Exception as e:
        err = str(e).lower()
        if "429" in err or "quota" in err:
            return "Chatbot đang quá tải, vui lòng chờ 1 phút rồi thử lại."
        return "Không thể kết nối chatbot lúc này. Vui lòng thử lại sau."
