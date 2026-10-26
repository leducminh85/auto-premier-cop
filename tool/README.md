# Auto Premiere Pro XML Generator

## Cập nhật tự động cho bản Windows

Ứng dụng có nút **Kiểm tra cập nhật** và kiểm tra bản mới khi mở app.
Xem [hướng dẫn máy chủ cập nhật](UPDATES.md) để tạo tunnel, đóng gói bản có địa chỉ
cập nhật và phát hành. Khách dùng EXE cũ cần thay bản có bộ cập nhật một lần.

## Format DOCX linh hoạt trong ứng dụng desktop

Ứng dụng dùng chung một bộ đọc cho preview, xuất XML, transcript và audio gộp.
Format bảng cũ vẫn được hỗ trợ; không cần thêm “Đoạn kịch tính” trước `Vid1:`.

- Bảng hai cột: nhãn phần và nội dung. Bảng có thêm cột STT/ghi chú cần header
  `Section`/`Phần` và `Content`/`Nội dung` để xác định đúng cột; cột ghi chú không được đọc.
- Có thể dùng các đoạn văn thay bảng: đặt tiêu đề phần trên một đoạn riêng,
  sau đó mỗi đoạn lời đọc hoặc dòng timecode riêng. Có thể trộn bảng và đoạn văn.
- Nhãn phần: `Intro`, `Introduction`, `Mở đầu`, `Mở bài`; `Vào tình huống 1`,
  `Tình huống 1`, `TH1`, `Scenario 1`, `Case 1`; `Kết bài`, `Outro`, `Conclusion`.
  Không phân biệt hoa/thường, dấu tiếng Việt hoặc khoảng trắng.
- Timecode: `6:23 - 6:27`, `00:06:23.500 --> 00:06:27.000`, dấu `–`, `—`, `→`,
  `to`, `đến`. Các khoảng cắt có thể nối bằng `+`.
- Intro cần chỉ rõ nguồn, ví dụ `Vid1: 6:23 - 6:27 + Vid2: 1:53 - 1:58`.
  Trong tình huống, dòng không ghi nguồn sẽ dùng số tình huống hiện tại.
- Các dòng teaser lặp trong nhãn tình huống được loại trùng như trước;
  đoạn cắt lặp trong phần nội dung vẫn được giữ.

Đây là parser theo cấu trúc, không tự hiểu mọi bố cục Word. Giữ lời đọc và
timecode ở các đoạn riêng. Thiếu số nguồn hoặc timecode sai sẽ báo vị trí để sửa,
không đoán video. Quy trình media hiện vẫn dùng TH1–TH6. Audio và video thực tế
vẫn cần đúng thứ tự, nội dung và đủ thời lượng; nhận đúng DOCX không thay thế
việc kiểm tra media.

Tool tạo timeline Premiere Pro từ một kịch bản Word, 6 video tình huống và các file voice-over. Các track hiệu ứng, BGM, SFX, overlay và nested sequence được giữ từ `Copied_28/28.8.xml`.

Repo có hai phiên bản:

- `generate_xml.py`: bản đầy đủ, giữ cấu trúc effect/template `28.8.xml`.
- `generate_simple_xml.py`: bản tối giản, chỉ dựng video cut và audio trên V1/A1, không cần template hoặc fixed asset.

## Yêu cầu

- Python 3.10 trở lên.
- `ffmpeg`/`ffprobe` có trong `PATH`.
- Package `python-docx` và `PySide6` (được liệt kê trong `requirements.txt`).

```bash
python3 -m pip install -r tool/requirements.txt
```

## Cấu trúc project đầu vào

```text
project_moi/
├── content.docx
├── video/
│   ├── 1.mp4
│   ├── 2.mp4
│   ├── 3.mp4
│   ├── 4.mp4
│   ├── 5.mp4
│   └── 6.mp4
└── audio/
    ├── 01.mp3
    ├── 02.mp3
    ├── ...
    └── NN.mp3
```

Quy tắc:

- Phải có đúng 6 video. Có thể dùng tên `1.mp4`–`6.mp4` hoặc tên chứa `TH1`–`TH6`.
- Audio được ánh xạ theo natural sort: `1, 2, ..., 9, 10...`.
- Số audio phải bằng số câu đọc trong DOCX. Số này có thể thay đổi theo từng bài; kịch bản mẫu hiện có 68 câu.
- Số đoạn timecode/video cut có thể tăng hoặc giảm theo từng bài.
- Timecode trong DOCX không được vượt quá thời lượng video tương ứng.
- Cấu trúc DOCX phải theo mẫu: bảng intro, 6 bảng tình huống và bảng outro.

## Kiểm tra trước khi tạo XML

Chạy từ thư mục gốc của repo:

```bash
python3 tool/generate_xml.py \
  --project-dir path/to/project_moi \
  --fixed-dir Copied_28 \
  --template Copied_28/28.8.xml \
  --dry-run
```

Dry run kiểm tra:

- DOCX và số câu voice-over.
- Đủ TH1–TH6.
- Chuỗi voice-over/video cut có thể ánh xạ, thêm hoặc bỏ slot trong template.
- Timecode có nằm trong video.
- Toàn bộ media path trong XML có tồn tại.
- Các trường timeline `start/end` và `in/out` sau khi ripple.

Dry run không ghi file.

## Tạo XML

```bash
python3 tool/generate_xml.py \
  --project-dir path/to/project_moi \
  --fixed-dir Copied_28 \
  --template Copied_28/28.8.xml
```

Kết quả mặc định là `project_moi/output.xml`.

## Bản tối giản: chỉ video và audio

Bản này không đọc `28.8.xml` và không cần thư mục `fixed/`. Timeline được tạo mới từ đầu:

- Mỗi câu voice-over được đặt nguyên file trên A1.
- Mỗi timecode được cắt từ video tương ứng và đặt trên V1.
- Audio gốc của đoạn video được đặt trên A1 và link với clip V1.
- Tất cả token được nối tiếp đúng thứ tự xuất hiện trong DOCX.
- Khoảng có voice-over nhưng không có video sẽ để hình đen mặc định.
- Không có effect, transition, adjustment layer, graphic, overlay, BGM, SFX hoặc nested sequence.

Kiểm tra trước:

```bash
python3 tool/generate_simple_xml.py \
  --project-dir path/to/project_moi \
  --dry-run
```

Tạo `project_moi/output_simple.xml`:

```bash
python3 tool/generate_simple_xml.py \
  --project-dir path/to/project_moi
```

Có thể đặt tên sequence và output riêng:

```bash
python3 tool/generate_simple_xml.py \
  --project-dir path/to/project_moi \
  --sequence-name "Episode Simple Cut" \
  --output path/to/simple_cut.xml
```

Cũng có thể truyền từng đường dẫn:

```bash
python3 tool/generate_xml.py \
  --content path/to/content.docx \
  --video-dir path/to/video \
  --audio-dir path/to/audio \
  --fixed-dir Copied_28 \
  --template Copied_28/28.8.xml \
  --output path/to/output.xml
```

Các đường dẫn tương đối được tính từ thư mục đang chạy lệnh. Nếu không truyền `--fixed-dir` và `--template`, tool tự dùng thư mục `Copied_28` nằm cạnh thư mục `tool`.

## Tool xử lý timeline như thế nào

1. Đọc intro, teaser và các đoạn bodycam từ timecode trong DOCX.
2. Ánh xạ 6 video và các voice-over theo thứ tự.
3. Thay source media nhưng giữ nguyên XML id của template.
4. Cập nhật các linked clip video/audio và các nested sequence hiện có của TH4.
5. Nếu bài có ít slot hơn template, bỏ clip thừa và ripple phần timeline còn lại.
6. Nếu bài có nhiều slot hơn template, nhân bản clip video/audio gần nhất, tạo XML id mới và chèn đúng thứ tự nội dung.
7. Gộp các mảnh voice-over cũ thành một clip hoàn chỉnh cho mỗi file audio mới.
8. Ripple-remap các track hiệu ứng, transition, BGM và SFX theo các vùng nội dung thay đổi.
9. Kiểm tra kết quả rồi ghi XML theo kiểu atomic, có `DOCTYPE xmeml`.

## Import vào Premiere Pro

1. Mở Adobe Premiere Pro.
2. Chọn **File → Import** (`Cmd + I` trên macOS hoặc `Ctrl + I` trên Windows).
3. Chọn file `output.xml`.
4. Nếu Premiere hỏi relink media, kiểm tra lại rằng project chưa bị di chuyển sau khi tạo XML.

> Tool vẫn dùng 6 tình huống TH1–TH6 vì đó là cấu trúc lớn của template `28.8.xml`. Số voice-over và số cut bên trong mỗi bài được phép thay đổi. Slot được thêm sẽ kế thừa filter/thiết lập clip cơ bản gần nhất; các graphic đặc thù ở upper track có thể cần tinh chỉnh thủ công trong Premiere.

## Một file audio gộp, không có transcript

`generate_simple_xml.py` tự bật speech-to-text khi thư mục audio chỉ có một
file và không truyền `--transcript`. Giao diện v3 dùng chung backend nên cũng
nhận chế độ này khi chạy từ source có cài dependency ASR.

```bash
python3 -m pip install -r tool/requirements-asr.txt
python3 tool/generate_simple_xml.py \
  --project-dir path/to/project_moi \
  --asr-language vi \
  --asr-model small
```

- ASR dùng [faster-whisper](https://github.com/SYSTRAN/faster-whisper), chạy
  trên CPU tại máy. Lần đầu cần mạng để tải model; không gửi audio đến API.
  Bỏ `--asr-language` để tự nhận diện ngôn ngữ; dùng `en` cho tiếng Anh.
- Lấy timestamp từng từ, đối chiếu chuỗi từ với lời dẫn trong DOCX theo thứ tự.
  Cho phép sai, thiếu hoặc thừa từ; từng vùng lời dẫn giữa các video cut phải
  đạt ít nhất 40% từ khớp và tối thiểu 3 từ (hoặc toàn bộ nếu vùng ngắn hơn).
- Điểm chèn video cần từ khớp ở hai phía gần ranh giới. Nếu thiếu một số từ,
  tool ước lượng ranh giới từ timestamp lân cận; không đủ căn cứ sẽ báo lỗi.
  Có thể thử `--asr-model medium` hoặc `large-v3` nếu nhận diện chưa tốt.
- A2 gồm các đoạn tham chiếu **cùng file nguồn** với `in/out` khác nhau.
  Mỗi vùng lời dẫn giữa các video cut là một clip; không khôi phục chính xác
  ranh giới các MP3 trước khi merge. Giữ toàn bộ audio, kiểm tra waveform để
  tìm khoảng lặng thực tế (ít nhất 120 ms, dưới -40 dBFS) gần ranh giới câu,
  rồi cắt ở giữa khoảng lặng. Nếu không tìm được khoảng lặng phù hợp, dùng
  ranh giới timestamp lời đọc và ghi log. Không chèn thêm khoảng nghỉ.
  Video được chèn vào timeline theo content. Timestamp nguồn
  khác thời gian timeline vì timeline có thêm thời lượng video.
- Log `[ASR]`, `[MATCH]`, `[CUT]` cho biết tiến độ, tỷ lệ khớp và khoảng cắt.
  Timestamp ASR là ước lượng; nên nghe lại các điểm chuyển đoạn trong Premiere.
  `--dry-run` vẫn chạy ASR và kiểm tra alignment nhưng không ghi XML.
- Nhiều file audio hoặc có `--transcript` tiếp tục dùng chế độ cũ.
  Dependency ASR cài riêng; các bản ứng dụng đã đóng gói trước đó chưa có thay đổi này.

Kiểm tra tự động phần alignment, range XML và các chế độ cũ (ASR được giả lập):

```bash
python3 -m unittest discover -s tool -p 'test_merged_audio.py' -v
```

## Build ứng dụng v3

Chạy trực tiếp giao diện PySide6 trên macOS:

```bash
python3 -m pip install -r tool/requirements.txt
python3 tool/generate_simple_xml_v3_app.py
```

Giao diện chỉ dùng light theme, tự quét project ở background và giữ nguyên backend tạo XML hiện có.

Cài dependency build rồi chạy trên đúng hệ điều hành đích:

```bash
python3 -m pip install -r tool/requirements.txt "pyinstaller>=6.10,<7"
python3 tool/build_v3_app.py
```

- Windows tạo `dist/xmlcutroom-win.exe`.
- macOS tạo `dist/xmlcutroom-mac.app`.
- `ffprobe` được đóng gói cùng ứng dụng; máy chạy app không cần cài FFmpeg riêng.

Muốn tạo `.exe` mà không có máy Windows, chạy workflow **Build v3 Windows executable** trong GitHub Actions rồi tải artifact `xmlcutroom-win`.
