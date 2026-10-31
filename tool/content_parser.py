"""Shared, ordered DOCX reader for preview, voice-over alignment and export."""
from dataclasses import dataclass
from pathlib import Path
import re
import unicodedata

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from generate_xml import GeneratorError


class ContentParseError(GeneratorError):
    """Actionable document diagnostic, preserved across the UI worker boundary."""
    def __init__(self, path, location, text, reason, hint, fragment=''):
        self.path = Path(path)
        self.location = location
        self.text = text
        self.reason = reason
        self.hint = hint
        start = text.find(fragment) if fragment else -1
        self.span = (start, start + len(fragment)) if start >= 0 else (0, len(text))
        if start < 0 and fragment and ':' in fragment:
            # Keep highlighting the original Word text after whitespace normalization.
            pattern = r'\s*:\s*'.join(re.escape(part) for part in fragment.split(':'))
            match = re.search(pattern, text)
            if match:
                self.span = match.span()
        super().__init__(f'{reason}\nVị trí: {location}\nFile: {self.path}\n'
                         f'Nội dung: {text}\nCách sửa: {hint}')


def fold(text: str) -> str:
    text = unicodedata.normalize('NFKD', text.casefold()).replace('đ', 'd')
    return ' '.join(''.join(c for c in text if not unicodedata.combining(c)).split())


SOURCE = re.compile(r'\b(?:vid(?:eo)?|th|(?:vao\s+)?tinh\s+huong|scenario|case)\s*[#:_-]?\s*(\d+)\b')
SOURCE_TIME_SEPARATOR = re.compile(rf'({SOURCE.pattern})\s*:(?=\d+:)')
STAMP = r'\d{1,3}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?'
RANGE = re.compile(rf'(?<![\d:])({STAMP})\s*(?:-->|->|[-–—−→]|\bto\b|\bden\b)\s*({STAMP})(?![\d:])')
INTRO = {'intro', 'introduction', 'mo dau', 'mo bai'}
OUTRO = {'outro', 'conclusion', 'ket bai', 'ket thuc'}
LABEL_HEADERS = {'type', 'section', 'phan', 'hang muc', 'loai'}
TEXT_HEADERS = {'content', 'script', 'noi dung', 'loi doc', 'narration'}


def normalize_timecodes(text: str) -> str:
    # Word often contains spaces (including NBSP/tabs) around timestamp colons.
    # Only join numeric components; do not remove whitespace inside digit groups.
    text = re.sub(r'(?<=\d)\s*:\s*(?=\d)', ':', fold(text))
    # Separate an explicit source label from its timestamp (Vid2:2:06).
    # Keep timestamp boundaries strict so malformed timestamps still fail.
    return SOURCE_TIME_SEPARATOR.sub(r'\1: ', text)


@dataclass(frozen=True)
class ContentEvent:
    kind: str
    section: str
    location: str
    text: str = ''
    source: int | None = None
    start: float = 0
    end: float = 0


def seconds(stamp: str, location: str = '') -> float:
    parts = stamp.replace(',', '.').split(':')
    if any(float(p) >= 60 for p in parts[1:]):
        raise GeneratorError(invalid_time_message(location))
    value = 0.0
    for part in parts:
        value = value * 60 + float(part)
    return value


def invalid_time_message(location: str) -> str:
    where = f' tại {location}' if location else ''
    return (f'Thời gian cắt{where} không hợp lệ. Hãy kiểm tra lại giờ, phút, giây '
            'và bảo đảm thời gian kết thúc lớn hơn thời gian bắt đầu.')


def rows(document):
    """Preserve paragraph/table order; ignore extra note columns explicitly."""
    table_index = 0
    paragraph_index = 0
    for block in document.element.body:
        if block.tag.endswith('}p'):
            paragraph_index += 1
            text = Paragraph(block, document).text.strip()
            if text:
                yield '', [text], f'đoạn văn {paragraph_index}', True
        elif block.tag.endswith('}tbl'):
            table_index += 1
            table = Table(block, document)
            label_col, text_col = 0, 1
            for index, row in enumerate(table.rows, 1):
                cells = list(row.cells)
                location = f'bảng {table_index}, dòng {index}'
                labels = [fold(c.text).strip(':') for c in cells]
                # A header can move the content column, e.g. STT / Section / Content.
                types = [i for i, v in enumerate(labels) if v in LABEL_HEADERS]
                texts = [i for i, v in enumerate(labels) if v in TEXT_HEADERS]
                if types and texts:
                    label_col, text_col = types[0], texts[0]
                    continue
                unique = {id(c._tc) for c in cells}
                if len(cells) == 1 or len(unique) == 1:
                    yield '', [p.text for p in cells[0].paragraphs], location, True
                elif max(label_col, text_col) < len(cells):
                    yield cells[label_col].text, [p.text for p in cells[text_col].paragraphs], location, False


def read_content(path: Path) -> list[ContentEvent]:
    try:
        document = Document(path)
    except Exception as exc:
        raise GeneratorError(f'Không đọc được DOCX: {path}') from exc
    events = []
    section = ''
    scenario = None
    seen_teasers = set()
    for label, paragraphs, location, standalone in rows(document):
        original_label = label
        text = ' '.join(paragraphs).strip()
        def fail(reason, hint, fragment='', *, heading=False):
            raise ContentParseError(path, location, original_label if heading and not standalone else text,
                                    reason, hint, fragment)
        label = fold(label).strip(' :')
        body = ' '.join(paragraphs).strip()
        heading = fold(body).strip(' :') if standalone else label
        match = SOURCE.fullmatch(heading) if standalone else SOURCE.match(heading)
        is_scenario = bool(match and not heading.startswith(('vid', 'video')))
        if heading in INTRO | OUTRO:
            section = 'intro' if heading in INTRO else 'outro'
            scenario = None
            events.append(ContentEvent('section', section, location))
            if standalone:
                continue
        elif match:
            scenario = int(match.group(1))
            if not 1 <= scenario <= 6:
                fail(f'Số video {scenario} nằm ngoài phạm vi hỗ trợ.', 'Dùng số từ 1 đến 6, đúng với video nguồn.', str(scenario), heading=True)
            section = f'th{scenario}'
            events.append(ContentEvent('section', section, location))
            if standalone:
                continue
        if label == 'video' and not RANGE.search(normalize_timecodes(body)):
            continue  # Source-title list in legacy documents.
        for paragraph in paragraphs:
            text = paragraph.strip()
            if not text:
                continue
            normalized = normalize_timecodes(text)
            ranges = list(RANGE.finditer(normalized))
            remainder = RANGE.sub('', normalized)
            if re.search(r'\d+:\d+\s*(?:[-–—−→]|to\b|den\b)', remainder):
                fragment = next((part.strip() for part in text.split('+')
                                 if re.search(r'\d+:\d+\s*(?:[-–—−→]|to\b|den\b)',
                                              RANGE.sub('', normalize_timecodes(part)))), text)
                fail('Không đọc được khoảng thời gian được đánh dấu.',
                     'Ghi đủ mốc bắt đầu và kết thúc theo phút:giây hoặc giờ:phút:giây; '
                     'phần giây phải có 2 chữ số. Ví dụ: 0:10 - 0:20. '
                     'Dùng dấu + để nối nhiều đoạn. Đối chiếu mốc thực tế trong video.', fragment)
            if not ranges:
                if section:
                    events.append(ContentEvent('audio', section, location, text))
                continue
            sources = list(SOURCE.finditer(normalized))
            if not section:
                section = 'intro'
            previous_end = 0
            active_source = scenario
            parsed = []
            for time_range in ranges:
                explicit = [s for s in sources if previous_end <= s.start() < time_range.start()]
                if explicit:
                    numbers = {int(s.group(1)) for s in explicit}
                    if len(numbers) != 1:
                        fail('Một đoạn cắt có nhiều số video nguồn.', 'Chỉ giữ một nhãn Vid cho mỗi đoạn. Ví dụ: Vid1: 0:10 - 0:20 + Vid2: 0:30 - 0:40.')
                    active_source = numbers.pop()
                if active_source is None:
                    fail('Đoạn cắt chưa có video nguồn.', 'Thêm nhãn Vid trước mốc thời gian, đúng số video nguồn. Ví dụ: Vid1: 0:10 - 0:20.')
                if not 1 <= active_source <= 6:
                    fail(f'Số video {active_source} nằm ngoài phạm vi hỗ trợ.', 'Đổi nhãn Vid thành số từ 1 đến 6, đúng với video nguồn.')
                for stamp in time_range.groups():
                    if any(float(p) >= 60 for p in stamp.replace(',', '.').split(':')[1:]):
                        fail(f'Mốc {stamp} có phần phút hoặc giây từ 60 trở lên.',
                             'Phần giây và phần phút trong giờ:phút:giây phải nhỏ hơn 60. '
                             'Ví dụ: 0:65 viết thành 1:05. Kiểm tra mốc đúng trong video.', stamp)
                start, end = (seconds(stamp, location) for stamp in time_range.groups())
                if end <= start:
                    fail('Mốc kết thúc không lớn hơn mốc bắt đầu.', 'Sửa mốc kết thúc để nằm sau mốc bắt đầu. Ví dụ: 0:10 - 0:20; đối chiếu video trước khi đổi.', next((part.strip() for part in text.split('+') if time_range.group() in normalize_timecodes(part)), text))
                parsed.append(ContentEvent('video', section or 'intro', location, text, active_source, start, end))
                previous_end = time_range.end()
            key = tuple((e.source, e.start, e.end) for e in parsed)
            if is_scenario and key in seen_teasers:
                continue
            if is_scenario:
                seen_teasers.add(key)
            events.extend(parsed)
    if not any(e.kind == 'video' for e in events):
        raise GeneratorError('File Word chưa có thời gian cắt video. Hãy thêm, ví dụ: “Vid1: 0:10 - 0:20”.')
    return events
