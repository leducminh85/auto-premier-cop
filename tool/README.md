# Auto Premiere Pro XML Generator

Tool tạo timeline Premiere Pro từ một kịch bản Word, 6 video tình huống và các file voice-over. Các track hiệu ứng, BGM, SFX, overlay và nested sequence được giữ từ `Copied_28/28.8.xml`.

## Yêu cầu

- Python 3.10 trở lên.
- `ffmpeg`/`ffprobe` có trong `PATH`.
- Package `python-docx`.

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
