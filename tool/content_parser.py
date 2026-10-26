"""Shared, ordered DOCX reader for preview, voice-over alignment and export."""
from dataclasses import dataclass
from pathlib import Path
import re
import unicodedata

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from generate_xml import GeneratorError


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
    # Separate an explicit source label from its timestamp (Vid2:2:06).
    # Keep timestamp boundaries strict so malformed timestamps still fail.
    return SOURCE_TIME_SEPARATOR.sub(r'\1: ', fold(text))


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
                raise GeneratorError(f'Số video {scenario} tại {location} không hợp lệ. Chỉ dùng số từ 1 đến 6.')
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
                raise GeneratorError(f'Không đọc được thời gian cắt tại {location}. Hãy ghi theo mẫu: “0:10 - 0:20”.')
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
                        raise GeneratorError(f'Có nhiều số video tại {location}. Hãy ghi rõ video cần dùng cho từng đoạn.')
                    active_source = numbers.pop()
                if active_source is None:
                    raise GeneratorError(f'Chưa biết dùng video nào tại {location}. Hãy ghi thêm, ví dụ: “Vid1: 0:10 - 0:20”.')
                if not 1 <= active_source <= 6:
                    raise GeneratorError(f'Số video {active_source} tại {location} không hợp lệ. Chỉ dùng số từ 1 đến 6.')
                start, end = (seconds(stamp, location) for stamp in time_range.groups())
                if end <= start:
                    raise GeneratorError(invalid_time_message(location))
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
