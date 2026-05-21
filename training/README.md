# Bộ câu hỏi train/test chatbot MetaCinema

File chính: `role_training_questions_vi.csv`

## Cách dùng

- Dùng cột `question` để nhập các câu mẫu khi test chatbot.
- Dùng `answer_hint` để viết nội dung FAQ/knowledge nếu muốn thêm vào trang Admin > Chatbot.
- Dùng `intent`, `expected_action`, `permission_note` để kiểm tra router có đi đúng luồng và đúng quyền không.

## Quy tắc phân quyền cần giữ

- `User/Customer`: chỉ xem dữ liệu công khai hoặc dữ liệu vé của chính tài khoản đó.
- `Staff/TicketStaff`: được hỗ trợ xác thực vé, phòng chiếu, bán vé tại quầy, menu, thanh toán vận hành. Không xem doanh thu và không dùng chấm công.
- `Staff/ShiftManager`: được xem chấm công, thống kê nhân viên trong ca và doanh thu ca.
- `Admin`: được xem dashboard quản trị, doanh thu, nhân viên, khách hàng, chấm công, chatbot knowledge và được phép thao tác ghi dữ liệu như tạo/xóa suất chiếu hoặc đổi vai trò nhân viên.

## Gợi ý train vào ChatbotKnowledge

Không nên nhập nguyên bộ câu hỏi động vào `ChatbotKnowledge`, vì các dữ liệu như suất chiếu, doanh thu, ghế trống, booking và chấm công phải lấy trực tiếp từ database/router.

Nên nhập các nhóm kiến thức tĩnh:

- Chính sách vé, đổi suất, hoàn tiền.
- Quy định độ tuổi C16/C18.
- Hướng dẫn thanh toán PayOS.
- Địa chỉ, hotline, giờ hỗ trợ.
- Quy trình thao tác cho nhân viên tại quầy.
- Nguyên tắc phân quyền: nhân viên thường không xem doanh thu/chấm công; Quản lý ca/Admin mới được xem.

Các câu hỏi động trong CSV nên dùng làm bộ test sau mỗi lần sửa chatbot.
