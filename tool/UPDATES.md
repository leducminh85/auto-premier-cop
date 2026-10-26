# Cập nhật app từ máy chủ của bạn

Khách đang dùng bản cũ cần nhận bản có bộ cập nhật **một lần**. Sau đó app tự
kiểm tra khi mở; khách chọn **Tải bản mới** rồi **Cập nhật ngay**. Có thể
chọn **Để sau** hoặc dùng nút **Kiểm tra cập nhật** bất cứ lúc nào.

## Kết nối máy chủ

Tạo Cloudflare Tunnel tên `auto-premier-updates`:

- Public hostname: `auto-premier-updates.wevic.vn`
- Service type: `HTTP`
- Service URL: `127.0.0.1:8765`
- Để trống Path; không đặt trang đăng nhập Cloudflare Access trước địa chỉ tải.

Chạy connector trên chính máy chủ theo hướng dẫn Cloudflare. Không đưa token
connector vào mã nguồn hoặc bản app gửi khách.

Khởi động máy chủ từ thư mục repository:

```powershell
python tool/serve_updates.py
```

Hoặc chạy ẩn cửa sổ lệnh bằng `powershell -File tool/start_update_server.ps1`.
Lệnh này kiểm tra máy chủ đã chạy để tránh mở trùng. Nhật ký nằm trong
`build/update-server.log` và `build/update-server-error.log`.

Máy chủ chỉ phục vụ `latest.json` và file `.exe` trong thư mục `update-server/releases`.
Không phục vụ mã nguồn, danh sách thư mục hay file dự án. Chỉ lắng nghe trên máy
này (`127.0.0.1`); Cloudflare Tunnel cung cấp địa chỉ HTTPS bên ngoài.
Mở tên miền trong trình duyệt sẽ thấy `Dumi Cut Room update server is running.`
`latest.json` chỉ có sau khi phát hành bản đầu tiên.

Máy chủ và connector cần chạy khi khách kiểm tra/tải bản mới. Nếu máy tắt hoặc
mất mạng, app trên máy khách vẫn dùng được bản hiện tại. Có thể đặt hai tiến trình
chạy lúc Windows khởi động; cần cấu hình dưới tài khoản phù hợp trên máy chủ.

## Đóng gói và phát hành

Địa chỉ cập nhật đã được cấu hình cho máy chủ này. Cần Python, PyInstaller, các gói trong
`requirements.txt` và ffprobe như quy trình build hiện tại.

Chạy các lệnh dưới đây trong PowerShell tại `D:\Tool\auto-premier-cop`.
Bản `3.1.0` đã phát hành. Ví dụ dưới đây tạo bản `3.1.1` có giao diện cập nhật mới.

1. Tạo file app mới:

   ```powershell
   python tool/build_v3_app.py --version 3.1.1
   ```

   Địa chỉ máy chủ đã có sẵn trong cấu hình. Chờ dòng `Build complete`, rồi mở
   `dist/Dumi Cut Room.exe` để kiểm tra trước khi gửi khách.

2. Bật máy chủ nếu chưa chạy:

   ```powershell
   powershell -File tool/start_update_server.ps1
   ```

   Tunnel `auto-premier-updates` cũng phải đang chạy.

3. Đưa bản mới lên máy chủ:

   ```powershell
   python tool/publish_update.py --exe "dist/Dumi Cut Room.exe" --notes "Giao diện cập nhật mới, thông báo ngắn gọn hơn."
   ```

   Lệnh này sao chép file vào thư mục phát hành và cập nhật thông tin bản mới.
   Khách sẽ thấy bản mới khi mở app hoặc bấm **Kiểm tra cập nhật**.

Gửi `dist/Dumi Cut Room.exe` cho khách thay bản cũ lần đầu. Những lần sau, tăng
số phiên bản (ví dụ `3.1.1`), build, kiểm tra app, rồi chạy lệnh publish.
Không thay nội dung một phiên bản đã phát hành. File `.release.json` cạnh EXE
ghi lại phiên bản và địa chỉ đã đóng gói; giữ nó để lệnh publish đọc đúng thông tin.

Kiểm tra `https://auto-premier-updates.wevic.vn/latest.json` trả về JSON và địa chỉ
`url` trong đó tải được EXE. Không bật cache cho `latest.json` tại Cloudflare.
Chỉ phát hành sau khi đã thử bản mới trên một máy Windows khác.

## Gửi app cho khách

- Khách mới hoặc khách chưa có tính năng cập nhật: gửi riêng file
  `dist/Dumi Cut Room.exe`, hoặc gửi đường dẫn tải sau khi publish thành công.
  Với bản `3.1.1`, đường dẫn là:
  `https://auto-premier-updates.wevic.vn/releases/Dumi-Cut-Room-3.1.1.exe`.
  Đường dẫn này chỉ có sau khi phát hành bản `3.1.1`.
- Không gửi thư mục mã nguồn, Python, file `.release.json` hoặc token tunnel.
  Khách không cần cài Python để chạy bản `.exe`.
- Khách đã dùng bản có cập nhật: chỉ cần nhắn họ mở app và bấm
  **Kiểm tra cập nhật**. Không cần gửi lại file mỗi lần.

Tin nhắn mẫu gửi khách lần đầu:

> Bạn tải file app theo link mình gửi, lưu vào một thư mục riêng trong Documents
> rồi mở file để dùng. Nếu đang dùng bản cũ, hãy đóng app cũ trước khi thay file.
> Những lần sau, app sẽ báo khi có bản mới; bạn chọn “Tải bản mới” rồi làm theo
> thông báo để cập nhật.

Giữ app ở thư mục khách có quyền ghi để tự cập nhật được. Với bản cũ chưa có
nút cập nhật, cần thay file thủ công lần đầu. Với bản `3.1.0`, nút xác nhận cuối
hiển thị **Cập nhật và mở lại**; giao diện mới đổi thành **Cập nhật ngay**.

## Phạm vi và khôi phục

- Hiện hỗ trợ bản Windows 64-bit, một file EXE, trong thư mục người dùng có quyền ghi.
  Bản nằm trong `Program Files` hoặc thư mục bị khóa cần người cài app hỗ trợ;
  bộ cập nhật không tự xin quyền quản trị. macOS chưa có bộ cập nhật.
- App tải qua HTTPS, kiểm tra dung lượng và SHA-256 trước khi thay file. Chỉ
  người quản lý máy chủ được sửa thư mục phát hành. HTTPS và máy chủ là nguồn
  tin cậy hiện tại; chưa bổ sung chữ ký số độc lập cho bản phát hành.
- Cập nhật không diễn ra trong lúc app đang xử lý dự án. Sau khi mở lại, khách
  cần chọn lại dự án. File Word, âm thanh, video và XML không bị thay đổi.
- Bộ trợ giúp chờ app đóng, giữ EXE cũ tại `.dumi-update-*/previous.exe` cạnh app,
  rồi thay file mới và mở lại. Nếu thay hoặc gọi mở file mới thất bại, nó phục hồi
  EXE cũ. Chưa tự phát hiện lỗi xảy ra bên trong app mới sau khi khởi chạy.
- Nếu app mới mở lỗi, đóng app và chép `previous.exe` về đúng tên EXE ban đầu.
  Các thư mục `.dumi-update-*` giữ bản tải và dự phòng; chỉ xóa sau khi app mới
  chạy ổn và không còn tiến trình cập nhật. Không đưa chúng vào thư mục phát hành.

Tài liệu tham khảo:
[Cloudflare Tunnel](https://developers.cloudflare.com/tunnel/get-started/),
[khởi động lại app PyInstaller](https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html#using-sys-executable-to-spawn-subprocesses-that-outlive-the-application-process-implementing-application-restart).
