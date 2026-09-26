#!/usr/bin/env python3
"""Generate a minimal Premiere-compatible FCP XML with spare tracks.

The DOCX is interpreted as an ordered stream of narration rows and video
timecodes. Video cuts are placed on V1 with linked source audio on A1, while
narration is placed on A2. The remaining tracks are left empty for editing.
No template, effects, transitions or fixed assets are used.
"""

from __future__ import annotations

import argparse
import difflib
import os
import re
import sys
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from docx import Document

from generate_xml import (
    GeneratorError,
    MediaInfo,
    ScriptToken,
    extract_timecodes,
    discover_media,
    existing_directory,
    existing_file,
    natural_sort_key,
    path_to_url,
    premiere_ticks,
    run_ffprobe,
)


SCENARIO_RE = re.compile(r"vào\s+tình\s+huống\s*(\d+)", re.IGNORECASE)
VIDEO_NUMBER_RE = re.compile(r"vid(?:eo)?\s*(\d+)", re.IGNORECASE)
TRANSCRIPT_TIMESTAMP_PATTERN = r"\d{1,2}:\d{2}:\d{2}[,.]\d{3}"
TRANSCRIPT_BLOCK_RE = re.compile(
    rf"(?ms)^[ \t]*(?P<index>\d+)[ \t]*\n"
    rf"[ \t]*(?P<start>{TRANSCRIPT_TIMESTAMP_PATTERN})[ \t]*-->[ \t]*"
    rf"(?P<end>{TRANSCRIPT_TIMESTAMP_PATTERN})[ \t]*\n"
    rf"(?P<text>.*?)"
    rf"(?=^[ \t]*\d+[ \t]*\n[ \t]*{TRANSCRIPT_TIMESTAMP_PATTERN}"
    rf"[ \t]*-->|\Z)"
)
VIDEO_TRACK_COUNT = 10
AUDIO_TRACK_COUNT = 5


@dataclass(frozen=True)
class StreamDetails:
    width: int = 1920
    height: int = 1080
    sample_rate: int = 48000
    audio_channels: int = 1
    has_audio: bool = True


@dataclass(frozen=True)
class TranscriptBlock:
    index: int
    text: str
    start_seconds: float
    end_seconds: float


@dataclass(frozen=True)
class VideoAnchor:
    word_position: int
    document_order: int
    location: str
    token: ScriptToken


@dataclass(frozen=True)
class TranscriptPlacement:
    block: TranscriptBlock
    audio_index: int
    word_start: int
    word_end: int
    confidence: float


@dataclass(frozen=True)
class SimpleGenerationSummary:
    output: Path
    video_cut_count: int
    narration_count: int
    duration_seconds: float
    fps: int
    source_file_count: int
    video_clip_count: int
    audio_clip_count: int
    narration_block_count: int
    removed_blur_effect_count: int = 0


class IdFactory:
    def __init__(self) -> None:
        self.values: dict[str, int] = {}

    def next(self, prefix: str) -> str:
        value = self.values.get(prefix, 0) + 1
        self.values[prefix] = value
        return f"{prefix}-{value}"


def add_text(parent: ET.Element, tag: str, value: object, **attributes: str) -> ET.Element:
    element = ET.SubElement(parent, tag, attributes)
    element.text = str(value)
    return element


def add_rate(parent: ET.Element, fps: int) -> ET.Element:
    rate = ET.SubElement(parent, "rate")
    add_text(rate, "timebase", fps)
    add_text(rate, "ntsc", "FALSE")
    return rate


def probe_stream_details(path: Path) -> StreamDetails:
    data = run_ffprobe(path)
    video = next((stream for stream in data.get("streams", []) if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in data.get("streams", []) if stream.get("codec_type") == "audio"), None)
    return StreamDetails(
        width=int(video.get("width", 1920)) if video else 1920,
        height=int(video.get("height", 1080)) if video else 1080,
        sample_rate=int(audio.get("sample_rate", 48000)) if audio else 48000,
        audio_channels=max(int(audio.get("channels", 1)), 1) if audio else 1,
        has_audio=audio is not None,
    )


def add_timecode(parent: ET.Element, fps: int) -> None:
    timecode = ET.SubElement(parent, "timecode")
    add_rate(timecode, fps)
    add_text(timecode, "string", "00:00:00:00")
    add_text(timecode, "frame", 0)
    add_text(timecode, "displayformat", "NDF")


def add_audio_characteristics(parent: ET.Element, details: StreamDetails) -> None:
    characteristics = ET.SubElement(parent, "samplecharacteristics")
    add_text(characteristics, "depth", 16)
    add_text(characteristics, "samplerate", details.sample_rate)


def add_full_file(
    parent: ET.Element,
    file_id: str,
    media: MediaInfo,
    details: StreamDetails,
    fps: int,
    include_video: bool,
) -> ET.Element:
    file_element = ET.SubElement(parent, "file", {"id": file_id})
    add_text(file_element, "name", media.path.name)
    add_text(file_element, "pathurl", path_to_url(media.path))
    add_rate(file_element, fps)
    add_text(file_element, "duration", media.frames)
    add_timecode(file_element, fps)
    file_media = ET.SubElement(file_element, "media")
    if include_video:
        video = ET.SubElement(file_media, "video")
        characteristics = ET.SubElement(video, "samplecharacteristics")
        add_rate(characteristics, fps)
        add_text(characteristics, "width", details.width)
        add_text(characteristics, "height", details.height)
        add_text(characteristics, "anamorphic", "FALSE")
        add_text(characteristics, "pixelaspectratio", "square")
        add_text(characteristics, "fielddominance", "none")
    if details.has_audio or not include_video:
        audio = ET.SubElement(file_media, "audio")
        add_audio_characteristics(audio, details)
        add_text(audio, "channelcount", details.audio_channels)
        if details.audio_channels >= 2:
            add_text(audio, "layout", "stereo")
        for channel_index in range(1, details.audio_channels + 1):
            audio_channel = ET.SubElement(audio, "audiochannel")
            add_text(audio_channel, "sourcechannel", channel_index)
            if details.audio_channels >= 2 and channel_index <= 2:
                add_text(audio_channel, "channellabel", "left" if channel_index == 1 else "right")
    return file_element


def add_file_reference(
    clip: ET.Element,
    media: MediaInfo,
    details: StreamDetails,
    fps: int,
    include_video: bool,
    file_ids: dict[Path, str],
    ids: IdFactory,
) -> None:
    key = media.path.resolve()
    if key in file_ids:
        ET.SubElement(clip, "file", {"id": file_ids[key]})
        return
    file_id = ids.next("file")
    file_ids[key] = file_id
    add_full_file(clip, file_id, media, details, fps, include_video)


def add_clip_common(
    track: ET.Element,
    clip_id: str,
    masterclip_id: str,
    media: MediaInfo,
    start: int,
    source_in: int,
    source_out: int,
    fps: int,
    channel_type: str | None = None,
) -> ET.Element:
    attributes = {"id": clip_id}
    if channel_type:
        attributes["premiereChannelType"] = channel_type
    clip = ET.SubElement(track, "clipitem", attributes)
    add_text(clip, "masterclipid", masterclip_id)
    add_text(clip, "name", media.path.name)
    add_text(clip, "enabled", "TRUE")
    add_text(clip, "duration", media.frames)
    add_rate(clip, fps)
    add_text(clip, "start", start)
    add_text(clip, "end", start + source_out - source_in)
    add_text(clip, "in", source_in)
    add_text(clip, "out", source_out)
    add_text(clip, "pproTicksIn", premiere_ticks(source_in, fps))
    add_text(clip, "pproTicksOut", premiere_ticks(source_out, fps))
    return clip


def add_source_track(clip: ET.Element, media_type: str) -> None:
    source_track = ET.SubElement(clip, "sourcetrack")
    add_text(source_track, "mediatype", media_type)
    add_text(source_track, "trackindex", 1)


def add_link(
    owner: ET.Element,
    reference: str,
    media_type: str,
    clip_index: int,
    track_index: int = 1,
) -> None:
    link = ET.SubElement(owner, "link")
    add_text(link, "linkclipref", reference)
    add_text(link, "mediatype", media_type)
    add_text(link, "trackindex", track_index)
    add_text(link, "clipindex", clip_index)


def add_av_links(
    video_clip: ET.Element,
    audio_clip: ET.Element,
    video_index: int,
    audio_index: int,
) -> None:
    for owner in (video_clip, audio_clip):
        add_link(owner, video_clip.get("id", ""), "video", video_index)
        add_link(
            owner,
            audio_clip.get("id", ""),
            "audio",
            audio_index,
            track_index=1,
        )


def add_track_footer(track: ET.Element, output_channel: int | None = None) -> None:
    add_text(track, "enabled", "TRUE")
    add_text(track, "locked", "FALSE")
    if output_channel is not None:
        add_text(track, "outputchannelindex", output_channel)


def add_audio_outputs(audio_media: ET.Element) -> None:
    outputs = ET.SubElement(audio_media, "outputs")
    group = ET.SubElement(outputs, "group")
    add_text(group, "index", 1)
    add_text(group, "numchannels", 2)
    add_text(group, "downmix", 0)
    for channel_index in (1, 2):
        channel = ET.SubElement(group, "channel")
        add_text(channel, "index", channel_index)


def add_sequence_format(video_media: ET.Element, details: StreamDetails, fps: int) -> None:
    format_element = ET.SubElement(video_media, "format")
    characteristics = ET.SubElement(format_element, "samplecharacteristics")
    add_rate(characteristics, fps)
    add_text(characteristics, "codec", "none")
    add_text(characteristics, "width", details.width)
    add_text(characteristics, "height", details.height)
    add_text(characteristics, "anamorphic", "FALSE")
    add_text(characteristics, "pixelaspectratio", "square")
    add_text(characteristics, "fielddominance", "none")


def one_line(value: str) -> str:
    return " ".join(value.split())


def timestamp_seconds(value: str) -> float:
    hours, minutes, remainder = value.replace(",", ".").split(":")
    seconds = float(remainder)
    result = int(hours) * 3600 + int(minutes) * 60 + seconds
    if int(minutes) >= 60 or seconds >= 60:
        raise GeneratorError(f"Mốc thời gian transcript không hợp lệ: {value}")
    return result


def read_transcript_text(path: Path) -> str:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise GeneratorError(f"Không đọc được transcript TXT: {path}") from exc
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return payload.decode("utf-16")
        except UnicodeDecodeError as exc:
            raise GeneratorError(f"Transcript TXT không phải UTF-16 hợp lệ: {path}") from exc
    try:
        return payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            return payload.decode("cp1252")
        except UnicodeDecodeError as exc:
            raise GeneratorError(
                f"Transcript TXT phải dùng UTF-8, UTF-16 hoặc Windows-1252: {path}"
            ) from exc


def parse_transcript_file(path: Path) -> list[TranscriptBlock]:
    """Parse numbered SRT-style blocks, where one block represents one audio file."""
    raw = read_transcript_text(path).replace("\r\n", "\n").replace("\r", "\n")
    matches = list(TRANSCRIPT_BLOCK_RE.finditer(raw))
    if not matches:
        raise GeneratorError(
            "Transcript không có block hợp lệ. Mỗi block cần gồm số thứ tự, "
            "timecode và nội dung."
        )

    cursor = 0
    blocks: list[TranscriptBlock] = []
    for match in matches:
        if raw[cursor : match.start()].strip():
            raise GeneratorError(
                f"Transcript có nội dung không thuộc block gần ký tự {cursor + 1}"
            )
        cursor = match.end()
        index = int(match.group("index"))
        text = one_line(match.group("text"))
        if not text:
            raise GeneratorError(f"Transcript block {index} không có nội dung")
        start = timestamp_seconds(match.group("start"))
        end = timestamp_seconds(match.group("end"))
        if end <= start:
            raise GeneratorError(f"Timecode transcript block {index} không hợp lệ")
        blocks.append(TranscriptBlock(index, text, start, end))
    if raw[cursor:].strip():
        raise GeneratorError("Transcript có nội dung thừa sau block cuối")

    indices = [block.index for block in blocks]
    expected = list(range(1, len(blocks) + 1))
    if indices != expected:
        raise GeneratorError(
            "Số thứ tự transcript phải liên tục từ 1; "
            f"hiện nhận được: {', '.join(map(str, indices[:12]))}"
        )
    return blocks


def normalized_words(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = normalized.replace("’", "'").replace("‘", "'")
    return re.findall(r"[^\W_]+", normalized, flags=re.UNICODE)


def parse_content_anchors(
    docx_path: Path, fps: int
) -> tuple[list[str], list[VideoAnchor]]:
    """Flatten narration to words while retaining every video's document position."""
    try:
        document = Document(docx_path)
    except Exception as exc:
        raise GeneratorError(f"Không đọc được DOCX: {docx_path}") from exc
    if not document.tables:
        raise GeneratorError("DOCX không có bảng nội dung")

    words: list[str] = []
    video_anchors: list[VideoAnchor] = []
    current_scenario: int | None = None
    content_started = False
    seen_teasers: set[tuple[int, str]] = set()
    document_order = 0

    for table_index, table in enumerate(document.tables, 1):
        for row_index, row in enumerate(table.rows, 1):
            cells = [one_line(cell.text) for cell in row.cells]
            row_type = cells[0] if cells else ""
            text = cells[1] if len(cells) > 1 else ""
            folded_type = row_type.casefold()
            location = f"bảng {table_index}, dòng {row_index}"

            scenario_match = SCENARIO_RE.search(row_type)
            if scenario_match:
                current_scenario = int(scenario_match.group(1))
                content_started = True
            elif folded_type in {"intro", "kết bài", "outro"}:
                content_started = True

            segments = extract_timecodes(text)
            if segments:
                source_number = current_scenario
                if "đoạn kịch tính" in text.casefold():
                    video_match = VIDEO_NUMBER_RE.search(text)
                    source_number = int(video_match.group(1)) if video_match else None
                if source_number is None or not 1 <= source_number <= 6:
                    raise GeneratorError(f"Không xác định được video tại {location}")
                if "tình huống" in folded_type:
                    teaser_key = (source_number, text.casefold())
                    if teaser_key in seen_teasers:
                        continue
                    seen_teasers.add(teaser_key)
                for start, end in segments:
                    video_anchors.append(
                        VideoAnchor(
                            word_position=len(words),
                            document_order=document_order,
                            location=location,
                            token=ScriptToken(
                                kind="video",
                                th_number=source_number,
                                source_in=round(start * fps),
                                source_out=round(end * fps),
                            ),
                        )
                    )
                    document_order += 1
                continue

            if not content_started or len(row.cells) < 2 or folded_type == "video":
                continue
            for paragraph in row.cells[1].paragraphs:
                narration = one_line(paragraph.text)
                if narration and not extract_timecodes(narration):
                    words.extend(normalized_words(narration))

    if not words:
        raise GeneratorError("Không tìm thấy nội dung để so khớp transcript trong DOCX")
    if not video_anchors:
        raise GeneratorError("Không tìm thấy timecode video trong DOCX")
    return words, video_anchors


def weighted_median(values: list[tuple[int, int]]) -> int:
    ordered = sorted(values)
    threshold = (sum(weight for _value, weight in ordered) + 1) // 2
    accumulated = 0
    for value, weight in ordered:
        accumulated += weight
        if accumulated >= threshold:
            return value
    return ordered[-1][0]


def align_transcript_blocks(
    content_words: list[str], blocks: list[TranscriptBlock]
) -> list[TranscriptPlacement]:
    block_words = [normalized_words(block.text) for block in blocks]
    empty = [block.index for block, words in zip(blocks, block_words) if not words]
    if empty:
        raise GeneratorError(f"Transcript block {empty[0]} không có từ để so khớp")

    transcript_words = [word for words in block_words for word in words]
    ranges: list[tuple[int, int]] = []
    cursor = 0
    for words in block_words:
        ranges.append((cursor, cursor + len(words)))
        cursor += len(words)

    matches = [
        match
        for match in difflib.SequenceMatcher(
            None, transcript_words, content_words, autojunk=False
        ).get_matching_blocks()
        if match.size
    ]
    placements: list[TranscriptPlacement] = []
    previous_start = -1
    for audio_index, (block, words, (block_start, block_end)) in enumerate(
        zip(blocks, block_words, ranges, strict=True)
    ):
        start_estimates: list[tuple[int, int]] = []
        matched_words = 0
        matched_content_positions: list[tuple[int, int]] = []
        for match in matches:
            overlap_start = max(block_start, match.a)
            overlap_end = min(block_end, match.a + match.size)
            if overlap_end <= overlap_start:
                continue
            size = overlap_end - overlap_start
            content_start = match.b + overlap_start - match.a
            start_estimates.append(
                (content_start - (overlap_start - block_start), size)
            )
            matched_content_positions.append((content_start, content_start + size))
            matched_words += size

        confidence = matched_words / len(words)
        minimum_matches = min(
            len(words), max(2, min(8, (len(words) + 3) // 4))
        )
        if matched_words < minimum_matches or confidence < 0.40:
            preview = " ".join(words[:12])
            raise GeneratorError(
                f"Không xác định chắc chắn vị trí transcript block {block.index} "
                f"({confidence:.0%} khớp): {preview!r}"
            )

        word_start = max(0, weighted_median(start_estimates))
        matched_start = min(start for start, _end in matched_content_positions)
        matched_end = max(end for _start, end in matched_content_positions)
        word_end = max(word_start + 1, matched_end)
        word_start = min(word_start, matched_start)
        if word_start < previous_start:
            raise GeneratorError(
                f"Transcript block {block.index} bị đảo thứ tự so với block trước"
            )
        previous_start = word_start
        placements.append(
            TranscriptPlacement(
                block=block,
                audio_index=audio_index,
                word_start=word_start,
                word_end=word_end,
                confidence=confidence,
            )
        )
    return placements


def tokens_from_transcript(
    docx_path: Path,
    transcript_path: Path,
    audio_count: int,
    fps: int,
) -> tuple[list[ScriptToken], list[TranscriptPlacement]]:
    blocks = parse_transcript_file(transcript_path)
    if len(blocks) != audio_count:
        raise GeneratorError(
            f"Transcript có {len(blocks)} block nhưng thư mục voice-over có "
            f"{audio_count} file. Mỗi block phải ứng với đúng một file audio."
        )
    content_words, video_anchors = parse_content_anchors(docx_path, fps)
    placements = align_transcript_blocks(content_words, blocks)

    for placement in placements:
        crossed = next(
            (
                anchor
                for anchor in video_anchors
                if placement.word_start < anchor.word_position < placement.word_end
            ),
            None,
        )
        if crossed is not None:
            raise GeneratorError(
                f"Transcript block {placement.block.index} đi qua video cut tại "
                f"{crossed.location}. Cần tách audio tại vị trí này."
            )

    ordered: list[tuple[int, int, int, ScriptToken]] = []
    for anchor in video_anchors:
        ordered.append(
            (anchor.word_position, 0, anchor.document_order, anchor.token)
        )
    for placement in placements:
        ordered.append(
            (
                placement.word_start,
                1,
                placement.audio_index,
                ScriptToken(kind="audio", audio_index=placement.audio_index),
            )
        )
    ordered.sort(key=lambda item: item[:3])
    return [item[3] for item in ordered], placements


def parse_ordered_script(
    docx_path: Path, fps: int
) -> tuple[list[ScriptToken], list[int]]:
    """Read narration blocks and video cuts without relying on table indexes."""
    try:
        document = Document(docx_path)
    except Exception as exc:
        raise GeneratorError(f"Không đọc được DOCX: {docx_path}") from exc
    if not document.tables:
        raise GeneratorError("DOCX không có bảng nội dung")

    tokens: list[ScriptToken] = []
    narration_lengths: list[int] = []
    current_scenario: int | None = None
    intro_started = False
    seen_teasers: set[tuple[int, str]] = set()

    for table_index, table in enumerate(document.tables):
        for row_index, row in enumerate(table.rows):
            cells = [one_line(cell.text) for cell in row.cells]
            row_type = cells[0] if cells else ""
            text = cells[1] if len(cells) > 1 else ""
            row_type_folded = row_type.casefold()

            scenario_match = SCENARIO_RE.search(row_type)
            if scenario_match:
                current_scenario = int(scenario_match.group(1))
            if table_index == 0 and row_type_folded == "intro":
                intro_started = True

            segments = extract_timecodes(text)
            if segments:
                source_number = current_scenario
                if "đoạn kịch tính" in text.casefold():
                    video_match = VIDEO_NUMBER_RE.search(text)
                    source_number = int(video_match.group(1)) if video_match else None
                if source_number is None or not 1 <= source_number <= 6:
                    raise GeneratorError(
                        f"Không xác định được video tại bảng {table_index + 1}, "
                        f"dòng {row_index + 1}"
                    )
                if "tình huống" in row_type_folded:
                    teaser_key = (source_number, text.casefold())
                    if teaser_key in seen_teasers:
                        continue
                    seen_teasers.add(teaser_key)
                tokens.extend(
                    ScriptToken(
                        kind="video",
                        th_number=source_number,
                        source_in=round(start * fps),
                        source_out=round(end * fps),
                    )
                    for start, end in segments
                )
                continue

            if table_index == 0 and not intro_started:
                continue
            if row_type_folded == "video" or len(row.cells) < 2:
                continue
            for paragraph in row.cells[1].paragraphs:
                narration = one_line(paragraph.text)
                if not narration or extract_timecodes(narration):
                    continue
                narration_lengths.append(len(narration))
                tokens.append(
                    ScriptToken(kind="audio", audio_index=len(narration_lengths) - 1)
                )

    if not any(token.kind == "video" for token in tokens):
        raise GeneratorError("Không tìm thấy timecode video trong DOCX")
    if not narration_lengths:
        raise GeneratorError("Không tìm thấy nội dung lời đọc trong DOCX")
    return tokens, narration_lengths


def rebalance_narration_tokens(
    tokens: list[ScriptToken], narration_lengths: list[int], audio_count: int
) -> list[ScriptToken]:
    """Map every supplied audio file to ordered narration positions in the DOCX."""
    block_count = len(narration_lengths)
    if audio_count <= 0:
        raise GeneratorError("Thư mục audio không có file voice-over")
    if block_count <= 0:
        raise GeneratorError("DOCX không có vị trí để đặt voice-over")

    allocations = [0] * block_count
    if audio_count >= block_count:
        allocations = [1] * block_count
        order = sorted(range(block_count), key=lambda index: (-narration_lengths[index], index))
        for extra_index in range(audio_count - block_count):
            allocations[order[extra_index % block_count]] += 1
    else:
        for audio_index in range(audio_count):
            block_index = min(block_count - 1, audio_index * block_count // audio_count)
            allocations[block_index] += 1

    result: list[ScriptToken] = []
    narration_index = 0
    audio_index = 0
    for token in tokens:
        if token.kind != "audio":
            result.append(token)
            continue
        for _ in range(allocations[narration_index]):
            result.append(ScriptToken(kind="audio", audio_index=audio_index))
            audio_index += 1
        narration_index += 1
    if audio_index != audio_count:
        raise GeneratorError(
            f"Không thể ánh xạ đủ audio: đã dùng {audio_index}/{audio_count} file"
        )
    return result


def build_simple_xml(
    sequence_name: str,
    tokens: list[ScriptToken],
    videos: dict[int, MediaInfo],
    audios: list[MediaInfo],
    fps: int,
) -> tuple[ET.Element, dict[str, int]]:
    ids = IdFactory()
    video_details = {number: probe_stream_details(media.path) for number, media in videos.items()}
    audio_details = [probe_stream_details(media.path) for media in audios]
    sequence_details = video_details[min(video_details)]

    root = ET.Element("xmeml", {"version": "4"})
    sequence = ET.SubElement(root, "sequence", {"id": "sequence-1"})
    add_text(sequence, "name", sequence_name)
    duration_element = add_text(sequence, "duration", 0)
    add_rate(sequence, fps)
    add_timecode(sequence, fps)
    media_element = ET.SubElement(sequence, "media")

    video_media = ET.SubElement(media_element, "video")
    add_sequence_format(video_media, sequence_details, fps)
    video_tracks = [
        ET.SubElement(video_media, "track") for _ in range(VIDEO_TRACK_COUNT)
    ]
    video_track = video_tracks[0]

    audio_media = ET.SubElement(media_element, "audio")
    add_text(audio_media, "numOutputChannels", 2)
    audio_format = ET.SubElement(audio_media, "format")
    add_audio_characteristics(audio_format, StreamDetails(sample_rate=48000))
    add_audio_outputs(audio_media)
    audio_track_attributes = {
        "premiereTrackType": "Stereo",
        "currentExplodedTrackIndex": "0",
        "totalExplodedTrackCount": "1",
    }
    audio_tracks = [
        ET.SubElement(audio_media, "track", audio_track_attributes)
        for _ in range(AUDIO_TRACK_COUNT)
    ]
    source_audio_track = audio_tracks[0]
    narration_track = audio_tracks[1]

    file_ids: dict[Path, str] = {}
    masterclip_ids: dict[Path, str] = {}
    cursor = 0
    video_clip_count = 0
    audio_clip_count = 0
    source_audio_clip_count = 0
    narration_count = 0
    video_cut_count = 0

    for token in tokens:
        if token.kind == "audio":
            if token.audio_index is None:
                raise GeneratorError("Voice-over token thiếu audio index")
            media = audios[token.audio_index]
            details = audio_details[token.audio_index]
            duration = media.frames
            clip_id = ids.next("clipitem")
            master_id = masterclip_ids.setdefault(media.path, ids.next("masterclip"))
            clip = add_clip_common(
                narration_track,
                clip_id,
                master_id,
                media,
                cursor,
                0,
                duration,
                fps,
                "mono" if details.audio_channels == 1 else "stereo",
            )
            add_file_reference(clip, media, details, fps, False, file_ids, ids)
            add_source_track(clip, "audio")
            cursor += duration
            audio_clip_count += 1
            narration_count += 1
            continue

        if token.th_number is None or token.th_number not in videos:
            raise GeneratorError("Video token thiếu source TH hợp lệ")
        media = videos[token.th_number]
        details = video_details[token.th_number]
        duration = token.source_out - token.source_in
        video_id = ids.next("clipitem")
        audio_id = ids.next("clipitem")
        master_id = masterclip_ids.setdefault(media.path, ids.next("masterclip"))

        video_clip_count += 1
        video_clip = add_clip_common(
            video_track,
            video_id,
            master_id,
            media,
            cursor,
            token.source_in,
            token.source_out,
            fps,
        )
        add_file_reference(video_clip, media, details, fps, True, file_ids, ids)
        add_source_track(video_clip, "video")
        if details.has_audio:
            audio_clip_count += 1
            source_audio_clip_count += 1
            audio_clip = add_clip_common(
                source_audio_track,
                audio_id,
                master_id,
                media,
                cursor,
                token.source_in,
                token.source_out,
                fps,
                "mono" if details.audio_channels == 1 else "stereo",
            )
            add_file_reference(audio_clip, media, details, fps, True, file_ids, ids)
            add_source_track(audio_clip, "audio")
            add_av_links(
                video_clip,
                audio_clip,
                video_clip_count,
                source_audio_clip_count,
            )
        cursor += duration
        video_cut_count += 1

    for track in video_tracks:
        add_track_footer(track)
    for track in audio_tracks:
        add_track_footer(track, 1)
    duration_element.text = str(cursor)
    return root, {
        "duration": cursor,
        "video_cuts": video_cut_count,
        "narrations": narration_count,
        "video_clips": video_clip_count,
        "audio_clips": audio_clip_count,
        "narration_audio_clips": narration_count,
        "source_audio_clips": source_audio_clip_count,
        "source_files": len(file_ids),
    }


def local_paths(root: ET.Element) -> list[Path]:
    from generate_xml import local_path_from_url

    paths: list[Path] = []
    for pathurl in root.findall(".//pathurl"):
        if pathurl.text:
            path = local_path_from_url(pathurl.text)
            if path is not None:
                paths.append(path)
    return paths


def validate_simple_xml(root: ET.Element, expected: dict[str, int]) -> None:
    sequence = root.find("./sequence")
    if sequence is None:
        raise GeneratorError("XML không có sequence")
    video_tracks = sequence.findall("./media/video/track")
    audio_tracks = sequence.findall("./media/audio/track")
    if len(video_tracks) != VIDEO_TRACK_COUNT:
        raise GeneratorError(
            f"Simple XML phải có đúng {VIDEO_TRACK_COUNT} video track"
        )
    if len(audio_tracks) != AUDIO_TRACK_COUNT:
        raise GeneratorError(
            f"Simple XML phải có đúng {AUDIO_TRACK_COUNT} audio track"
        )
    video_clips = video_tracks[0].findall("clipitem")
    source_audio_clips = audio_tracks[0].findall("clipitem")
    narration_clips = audio_tracks[1].findall("clipitem")
    audio_clips = narration_clips + source_audio_clips
    if len(video_clips) != expected["video_clips"] or len(audio_clips) != expected["audio_clips"]:
        raise GeneratorError("Số clip trong XML không khớp token kịch bản")
    if len(narration_clips) != expected["narration_audio_clips"]:
        raise GeneratorError("Voice-over trong XML không nằm đầy đủ trên A2")
    if len(source_audio_clips) != expected["source_audio_clips"]:
        raise GeneratorError("Audio video trong XML không nằm đầy đủ trên A1")
    if any(track.findall("clipitem") for track in video_tracks[1:]):
        raise GeneratorError("Các video track V2-V10 phải để trống")
    if any(track.findall("clipitem") for track in audio_tracks[2:]):
        raise GeneratorError("Các audio track A3-A5 phải để trống")
    ids = [clip.get("id") for clip in video_clips + audio_clips]
    if len(ids) != len(set(ids)):
        raise GeneratorError("XML có clipitem id bị trùng")
    for clip in video_clips + audio_clips:
        start = int(clip.findtext("start") or -1)
        end = int(clip.findtext("end") or -1)
        source_in = int(clip.findtext("in") or -1)
        source_out = int(clip.findtext("out") or -1)
        if start < 0 or end <= start or source_in < 0 or source_out <= source_in:
            raise GeneratorError(f"Clip {clip.findtext('name')!r} có range không hợp lệ")
    missing = sorted({path for path in local_paths(root) if not path.exists()})
    if missing:
        raise GeneratorError(f"Có {len(missing)} media path không tồn tại: {missing[0]}")
    if int(sequence.findtext("duration") or -1) != expected["duration"]:
        raise GeneratorError("Sequence duration không khớp timeline")


def write_xml(root: ET.Element, output_path: Path) -> None:
    ET.indent(root, space="\t")
    body = ET.tostring(root, encoding="utf-8")
    payload = b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n' + body + b"\n"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=output_path.name + ".", suffix=".tmp", dir=output_path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
        os.replace(temporary_name, output_path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def validate_written_simple_xml(output: Path, expected: dict[str, int]) -> None:
    try:
        root = ET.parse(output).getroot()
    except (ET.ParseError, OSError) as exc:
        raise GeneratorError(f"Không đọc lại được XML vừa tạo: {output}") from exc
    validate_simple_xml(root, expected)
    if root.findall(".//filter"):
        raise GeneratorError("XML vừa tạo còn chứa filter hoặc hiệu ứng hình ảnh")
    if root.findall(".//generatoritem") or root.findall(".//transitionitem"):
        raise GeneratorError("XML vừa tạo còn chứa overlay hoặc transition")


def generate_simple_project_xml(
    *,
    project_dir: Path,
    content: Path,
    video_dir: Path,
    audio_dir: Path,
    transcript: Path | None = None,
    output: Path | None = None,
    sequence_name: str | None = None,
    fps: int = 60,
    write_output: bool = True,
) -> SimpleGenerationSummary:
    """Generate a clean XML from DOCX, videos and audio without a blueprint."""
    if fps <= 0:
        raise GeneratorError("Frame rate phải lớn hơn 0")
    project_dir = existing_directory(project_dir.expanduser().resolve(), "Project dir")
    content = existing_file(content.expanduser().resolve(), "Content DOCX")
    video_dir = existing_directory(video_dir.expanduser().resolve(), "Video dir")
    audio_dir = existing_directory(audio_dir.expanduser().resolve(), "Audio dir")
    transcript = (
        existing_file(transcript.expanduser().resolve(), "Transcript TXT")
        if transcript is not None
        else None
    )
    output = (
        output.expanduser().resolve()
        if output is not None
        else project_dir / "output_simple.xml"
    )
    sequence_name = sequence_name or f"{project_dir.name} - Simple Cut"

    videos, audios = discover_media(video_dir, audio_dir, fps)
    if set(videos) != set(range(1, 7)):
        found = ", ".join(f"TH{number}" for number in sorted(videos)) or "không có"
        raise GeneratorError(f"Cần đúng video TH1-TH6; hiện tìm thấy: {found}")
    if transcript is not None:
        audios = sorted(audios, key=lambda media: natural_sort_key(media.path.name))
        tokens, placements = tokens_from_transcript(
            content, transcript, len(audios), fps
        )
        narration_block_count = len(placements)
    else:
        tokens, narration_lengths = parse_ordered_script(content, fps)
        tokens = rebalance_narration_tokens(tokens, narration_lengths, len(audios))
        narration_block_count = len(narration_lengths)

    for token in tokens:
        if token.kind != "video" or token.th_number is None:
            continue
        if token.source_out > videos[token.th_number].frames:
            raise GeneratorError(
                f"Timecode TH{token.th_number} kết thúc ở "
                f"{token.source_out / fps:.2f}s, vượt video "
                f"{videos[token.th_number].duration_seconds:.2f}s"
            )

    root, expected = build_simple_xml(sequence_name, tokens, videos, audios, fps)
    validate_simple_xml(root, expected)
    if root.findall(".//filter"):
        raise GeneratorError("XML đang tạo có filter hình ảnh ngoài dự kiến")
    if write_output:
        write_xml(root, output)
        validate_written_simple_xml(output, expected)

    return SimpleGenerationSummary(
        output=output,
        video_cut_count=expected["video_cuts"],
        narration_count=expected["narrations"],
        duration_seconds=expected["duration"] / fps,
        fps=fps,
        source_file_count=expected["source_files"],
        video_clip_count=expected["video_clips"],
        audio_clip_count=expected["audio_clips"],
        narration_block_count=narration_block_count,
    )


def resolve_arguments(args: argparse.Namespace) -> argparse.Namespace:
    project_dir = Path(args.project_dir).expanduser().resolve() if args.project_dir else None
    if project_dir:
        existing_directory(project_dir, "Project dir")
    if args.content:
        content = Path(args.content).expanduser().resolve()
    elif project_dir:
        candidates = sorted(project_dir.glob("*.docx"), key=lambda path: natural_sort_key(path.name))
        if not candidates:
            raise GeneratorError(f"Không tìm thấy DOCX trong {project_dir}")
        content = candidates[0]
    else:
        raise GeneratorError("Thiếu --project-dir hoặc --content")
    video_dir = Path(args.video_dir).expanduser().resolve() if args.video_dir else (
        project_dir / "video" if project_dir else None
    )
    audio_dir = Path(args.audio_dir).expanduser().resolve() if args.audio_dir else (
        project_dir / "audio" if project_dir else None
    )
    if video_dir is None or audio_dir is None:
        raise GeneratorError("Thiếu --video-dir hoặc --audio-dir")
    output = Path(args.output).expanduser().resolve() if args.output else (
        project_dir / "output_simple.xml" if project_dir else Path.cwd() / "output_simple.xml"
    )
    args.content = existing_file(content, "Content DOCX")
    args.project_dir = project_dir
    args.video_dir = existing_directory(video_dir, "Video dir")
    args.audio_dir = existing_directory(audio_dir, "Audio dir")
    args.transcript = (
        existing_file(Path(args.transcript).expanduser().resolve(), "Transcript TXT")
        if args.transcript
        else None
    )
    args.output = output
    if not args.sequence_name:
        base_name = project_dir.name if project_dir else content.stem
        args.sequence_name = f"{base_name} - Simple Cut"
    return args


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Tạo Premiere XML tối giản chỉ gồm video cut và audio")
    parser.add_argument("--project-dir", help="Thư mục có content.docx, video/ và audio/")
    parser.add_argument("--content", help="Đường dẫn content.docx")
    parser.add_argument("--video-dir", help="Thư mục video TH1-TH6")
    parser.add_argument("--audio-dir", help="Thư mục voice-over theo thứ tự kịch bản")
    parser.add_argument(
        "--transcript",
        help="TXT dạng SRT, mỗi block tương ứng một file voice-over (tùy chọn)",
    )
    parser.add_argument("--output", help="XML đầu ra; mặc định project-dir/output_simple.xml")
    parser.add_argument("--sequence-name", help="Tên sequence trong Premiere")
    parser.add_argument("--fps", type=int, default=60, help="Frame rate timeline (mặc định: 60)")
    parser.add_argument("--dry-run", action="store_true", help="Kiểm tra nhưng không ghi XML")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.fps <= 0:
        parser.error("--fps phải lớn hơn 0")
    try:
        args = resolve_arguments(args)
        summary = generate_simple_project_xml(
            project_dir=args.project_dir or args.content.parent,
            content=args.content,
            video_dir=args.video_dir,
            audio_dir=args.audio_dir,
            transcript=args.transcript,
            output=args.output,
            sequence_name=args.sequence_name,
            fps=args.fps,
            write_output=not args.dry_run,
        )
        print("=" * 68)
        print("Simple Premiere XML Generator - 10V + 5A")
        print("=" * 68)
        print(f"[SEQUENCE] {args.sequence_name}")
        print(
            f"[CONTENT] {summary.narration_block_count} khối nội dung: "
            f"{summary.narration_count} voice-over + "
            f"{summary.video_cut_count} video cut"
        )
        print(
            f"[TRACKS] V1={summary.video_clip_count} clip, "
            f"A1={summary.audio_clip_count - summary.narration_count} audio video, "
            f"A2={summary.narration_count} voice-over"
        )
        print(f"[DURATION] {summary.duration_seconds:.2f}s")
        print(
            f"[MEDIA] {summary.source_file_count} source, "
            "không có blueprint/effect/transition/overlay"
        )
        print("[VALIDATE] OK")
        if args.dry_run:
            print("[DRY RUN] Không ghi XML")
            return 0
        print(
            f"[OUTPUT] {summary.output} "
            f"({summary.output.stat().st_size / 1024:.1f} KB)"
        )
        return 0
    except GeneratorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
