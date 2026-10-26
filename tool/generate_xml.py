#!/usr/bin/env python3
"""Generate a Premiere Pro FCP XML timeline from a DOCX script and media.

The generator keeps the effects, graphics and nested sequences from the supplied
Premiere template. It replaces six source videos, maps voice-over files in script
order, updates source cuts, then ripple-remaps the rest of the timeline.
"""

from __future__ import annotations

import argparse
import copy
import difflib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from docx import Document


PREMIERE_TICKS_PER_SECOND = 254_016_000_000
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".m4v", ".avi"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac"}
TIMECODE_RE = re.compile(r"(\d+):(\d+)\s*[-\u2013]\s*(\d+):(\d+)")
TH_RE = re.compile(r"TH(\d+)", re.IGNORECASE)


class GeneratorError(RuntimeError):
    """A user-facing input or template error."""


@dataclass(frozen=True)
class MediaInfo:
    path: Path
    duration_seconds: float
    frames: int


@dataclass(frozen=True)
class EditEvent:
    old_start: int
    old_end: int
    new_duration: int
    label: str

    @property
    def old_duration(self) -> int:
        return self.old_end - self.old_start

    @property
    def delta(self) -> int:
        return self.new_duration - self.old_duration


@dataclass(frozen=True)
class ScriptToken:
    kind: str
    th_number: int | None = None
    source_in: int = 0
    source_out: int = 0
    audio_index: int | None = None

    @property
    def key(self) -> tuple[str, int | None]:
        return self.kind, self.th_number

    def duration(self, audios: list[MediaInfo]) -> int:
        if self.kind == "video":
            return self.source_out - self.source_in
        if self.audio_index is None:
            raise GeneratorError("Voice-over token thiếu audio index")
        return audios[self.audio_index].frames


@dataclass
class TemplateToken:
    kind: str
    th_number: int | None
    old_start: int
    old_end: int
    clips: list[ET.Element]
    old_name: str
    nest_name: str | None = None

    @property
    def key(self) -> tuple[str, int | None]:
        return self.kind, self.th_number


def natural_sort_key(value: object) -> list[object]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", str(value))]


def path_to_url(path: Path) -> str:
    return "file://localhost" + urllib.parse.quote(str(path.resolve()), safe="/:@")


def premiere_ticks(frame: int, fps: int) -> int:
    return round(frame * (PREMIERE_TICKS_PER_SECOND / fps))


def extract_timecodes(text: str) -> list[tuple[float, float]]:
    segments: list[tuple[float, float]] = []
    for match in TIMECODE_RE.finditer(text):
        start_min, start_sec, end_min, end_sec = map(int, match.groups())
        start = start_min * 60 + start_sec
        end = end_min * 60 + end_sec
        if end <= start:
            raise GeneratorError(f"Timecode không hợp lệ: {match.group(0)!r}")
        segments.append((float(start), float(end)))
    return segments


def hidden_subprocess_options() -> dict[str, object]:
    """Keep command-line helpers hidden when called by the Windows GUI."""
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "creationflags": subprocess.CREATE_NO_WINDOW,
        "startupinfo": startupinfo,
    }


def run_ffprobe(path: Path) -> dict:
    executable = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    bundle_root = getattr(sys, "_MEIPASS", None)
    bundled_executable = Path(bundle_root, executable) if bundle_root else None
    ffprobe_command = (
        str(bundled_executable)
        if bundled_executable is not None and bundled_executable.is_file()
        else executable
    )
    try:
        result = subprocess.run(
            [
                ffprobe_command,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=True,
            **hidden_subprocess_options(),
        )
        output = result.stdout
        if not isinstance(output, str) or not output.strip():
            details = (result.stderr or "").strip()
            suffix = f"\nChi tiết ffprobe: {details}" if details else ""
            raise GeneratorError(
                f"ffprobe không trả về dữ liệu JSON cho media: {path}{suffix}"
            )
        try:
            return json.loads(output)
        except json.JSONDecodeError as exc:
            details = (result.stderr or "").strip()
            suffix = f"\nChi tiết ffprobe: {details}" if details else ""
            raise GeneratorError(
                f"ffprobe trả về JSON không hợp lệ cho media: {path}{suffix}"
            ) from exc
    except FileNotFoundError as exc:
        raise GeneratorError("Không tìm thấy ffprobe. Hãy cài ffmpeg trước.") from exc
    except subprocess.SubprocessError as exc:
        raise GeneratorError(f"Không đọc được media bằng ffprobe: {path}") from exc


def probe_media(path: Path, fps: int, require_video: bool = False) -> MediaInfo:
    data = run_ffprobe(path)
    streams = data.get("streams", [])
    if require_video and not any(stream.get("codec_type") == "video" for stream in streams):
        raise GeneratorError(f"File không có video stream: {path}")
    duration_value = data.get("format", {}).get("duration")
    if duration_value is None:
        durations = [stream.get("duration") for stream in streams if stream.get("duration")]
        duration_value = durations[0] if durations else None
    try:
        seconds = float(duration_value)
    except (TypeError, ValueError) as exc:
        raise GeneratorError(f"Không xác định được thời lượng media: {path}") from exc
    if seconds <= 0:
        raise GeneratorError(f"Media có thời lượng không hợp lệ: {path}")
    return MediaInfo(path=path.resolve(), duration_seconds=seconds, frames=round(seconds * fps))


def sort_audios(file_list: list[Path]) -> list[Path]:
    """Natural sort, while supporting the template's two legacy '-2 (N)' intros."""
    intro = [path for path in file_list if re.search(r"[-_]2\s*\(|\bintro\b", path.name, re.IGNORECASE)]
    regular = [path for path in file_list if path not in intro]
    if intro:
        return sorted(intro, key=lambda p: natural_sort_key(p.name)) + sorted(
            regular, key=lambda p: natural_sort_key(p.name)
        )
    return sorted(file_list, key=lambda p: natural_sort_key(p.name))


def discover_media(video_dir: Path, audio_dir: Path, fps: int) -> tuple[dict[int, MediaInfo], list[MediaInfo]]:
    video_paths = sorted(
        (path for path in video_dir.iterdir() if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS and not path.name.startswith(".")),
        key=lambda p: natural_sort_key(p.name),
    )
    audio_paths = sort_audios([
        path for path in audio_dir.iterdir()
        if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS and not path.name.startswith(".")
    ])

    videos: dict[int, MediaInfo] = {}
    print(f"[MEDIA] {len(video_paths)} video trong {video_dir}")
    for fallback_number, path in enumerate(video_paths, 1):
        match = TH_RE.search(path.name)
        number = int(match.group(1)) if match else fallback_number
        if number in videos:
            raise GeneratorError(f"Có nhiều file cùng mang số video {number}. Hãy kiểm tra lại tên file.")
        info = probe_media(path, fps, require_video=True)
        videos[number] = info
        print(f"  TH{number}: {path.name} ({info.duration_seconds:.2f}s)")

    print(f"[MEDIA] {len(audio_paths)} voice-over trong {audio_dir}")
    audios = [probe_media(path, fps) for path in audio_paths]
    for index, info in enumerate(audios, 1):
        if index <= 4 or index > len(audios) - 2:
            print(f"  VO {index:02d}: {info.path.name} ({info.duration_seconds:.2f}s)")
        elif index == 5:
            print("  ...")
    return videos, audios


def parse_script(
    docx_path: Path, fps: int
) -> tuple[dict[int, list[tuple[int, int]]], int, list[ScriptToken]]:
    try:
        document = Document(docx_path)
    except Exception as exc:
        raise GeneratorError(f"Không đọc được DOCX: {docx_path}") from exc
    if len(document.tables) < 8:
        raise GeneratorError(
            f"DOCX phải có ít nhất 8 bảng (intro, TH1-TH6, outro); hiện có {len(document.tables)}"
        )

    intro_cuts: dict[int, list[tuple[float, float]]] = {}
    for row in document.tables[0].rows:
        cells = [cell.text.strip() for cell in row.cells]
        text = cells[1] if len(cells) > 1 else ""
        if "đoạn kịch tính" not in text.lower():
            continue
        match = re.search(r"vid(?:eo)?\s*(\d+)", text, re.IGNORECASE)
        if match:
            intro_cuts[int(match.group(1))] = extract_timecodes(text)

    scenario_cuts: dict[int, dict[str, list[tuple[float, float]]]] = {}
    for table in document.tables[1:-1]:
        if not table.rows:
            continue
        number_match = re.search(r"(\d+)", table.rows[0].cells[0].text.strip())
        if not number_match:
            continue
        number = int(number_match.group(1))
        teaser: list[tuple[float, float]] = []
        body: list[tuple[float, float]] = []
        seen_teaser_text: set[str] = set()
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            row_type = cells[0] if cells else ""
            text = cells[1] if len(cells) > 1 else ""
            segments = extract_timecodes(text)
            if not segments:
                continue
            if "tình huống" in row_type.lower():
                if text not in seen_teaser_text:
                    teaser.extend(segments)
                    seen_teaser_text.add(text)
            elif row_type in ("Nội dung", ""):
                body.extend(segments)
        scenario_cuts[number] = {"teaser": teaser, "body": body}

    cut_lists: dict[int, list[tuple[int, int]]] = {}
    for number in range(1, 7):
        if number not in scenario_cuts:
            raise GeneratorError(f"Không tìm thấy bảng tình huống {number} trong DOCX")
        raw = intro_cuts.get(number, []) + scenario_cuts[number]["teaser"] + scenario_cuts[number]["body"]
        cut_lists[number] = [(round(start * fps), round(end * fps)) for start, end in raw]

    narration_count = 0
    tokens: list[ScriptToken] = []
    for table_index, table in enumerate(document.tables):
        seen_timecode_rows: set[str] = set()
        scenario_number = table_index if 1 <= table_index <= 6 else None
        for row in table.rows:
            cells = [" ".join(cell.text.split()) for cell in row.cells]
            row_type = cells[0] if cells else ""
            text = cells[1] if len(cells) > 1 else ""
            if not text or row_type == "Video":
                continue
            segments = extract_timecodes(text)
            if segments:
                if "tình huống" in row_type.lower():
                    if text in seen_timecode_rows:
                        continue
                    seen_timecode_rows.add(text)
                if table_index == 0:
                    match = re.search(r"vid(?:eo)?\s*(\d+)", text, re.IGNORECASE)
                    token_th = int(match.group(1)) if match else None
                else:
                    token_th = scenario_number
                if token_th is not None:
                    tokens.extend(
                        ScriptToken(
                            kind="video",
                            th_number=token_th,
                            source_in=round(start * fps),
                            source_out=round(end * fps),
                        )
                        for start, end in segments
                    )
                continue
            tokens.append(ScriptToken(kind="audio", audio_index=narration_count))
            narration_count += 1
    return cut_lists, narration_count, tokens


def int_text(element: ET.Element, child: str, default: int = 0) -> int:
    value = element.findtext(child)
    return int(value) if value not in (None, "") else default


def set_text(element: ET.Element, child: str, value: int | str) -> None:
    target = element.find(child)
    if target is not None:
        target.text = str(value)


def source_th_number(name: str) -> int | None:
    match = TH_RE.search(name)
    return int(match.group(1)) if match else None


def is_voiceover_name(name: str) -> bool:
    return Path(name).suffix.lower() in AUDIO_EXTENSIONS


def unique_in_order(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def validate_edit_events(events: list[EditEvent]) -> None:
    previous: EditEvent | None = None
    for event in events:
        if event.old_end < event.old_start or event.new_duration < 0:
            raise GeneratorError(f"Edit event không hợp lệ: {event.label}")
        if event.old_duration == 0 and event.new_duration == 0:
            raise GeneratorError(f"Edit event rỗng: {event.label}")
        if event.old_duration > 0 and previous and event.old_start < previous.old_end:
            raise GeneratorError(f"Hai vùng nội dung bị chồng nhau: {previous.label} và {event.label}")
        if event.old_duration > 0:
            previous = event


def sort_edit_events(events: list[EditEvent]) -> list[EditEvent]:
    # Insertions at a boundary must happen before the original token at that boundary.
    return sorted(events, key=lambda item: (item.old_start, 0 if item.old_duration == 0 else 1, item.old_end))


def event_new_starts(events: list[EditEvent]) -> dict[str, int]:
    starts: dict[str, int] = {}
    delta = 0
    for event in events:
        starts[event.label] = event.old_start + delta
        delta += event.delta
    return starts


def map_position(position: int, events: list[EditEvent]) -> int:
    if position < 0:
        return position
    delta = 0
    for event in events:
        if event.old_duration == 0:
            if position >= event.old_start:
                delta += event.new_duration
            continue
        if position >= event.old_end:
            delta += event.delta
            continue
        if position <= event.old_start:
            break
        if event.new_duration == 0:
            return event.old_start + delta
        new_start = event.old_start + delta
        fraction = (position - event.old_start) / event.old_duration
        return new_start + round(fraction * event.new_duration)
    return position + delta


def remap_main_sequence(main_sequence: ET.Element, events: list[EditEvent]) -> None:
    tracks = main_sequence.findall("./media/video/track") + main_sequence.findall("./media/audio/track")
    for track in tracks:
        for tag in ("clipitem", "transitionitem"):
            for item in track.findall(tag):
                for field in ("start", "end"):
                    value = int_text(item, field, -1)
                    if value >= 0:
                        set_text(item, field, map_position(value, events))


def set_source_range(clip: ET.Element, source_in: int, source_out: int, media_duration: int, fps: int) -> None:
    set_text(clip, "in", source_in)
    set_text(clip, "out", source_out)
    set_text(clip, "duration", media_duration)
    set_text(clip, "pproTicksIn", premiere_ticks(source_in, fps))
    set_text(clip, "pproTicksOut", premiere_ticks(source_out, fps))


def set_timeline_range(clip: ET.Element, start: int, duration: int) -> None:
    set_text(clip, "start", start)
    set_text(clip, "end", start + duration)


def full_sequence_by_name(root: ET.Element, name: str) -> ET.Element | None:
    for sequence in root.findall(".//sequence"):
        if sequence.findtext("name") == name and sequence.find("duration") is not None:
            return sequence
    return None


def build_fixed_index(fixed_dir: Path) -> dict[str, Path]:
    index: dict[str, Path] = {}
    for path in fixed_dir.rglob("*"):
        if path.is_file() and not path.name.startswith("."):
            index.setdefault(path.name, path.resolve())
    return index


def update_file_definition(file_element: ET.Element, media: MediaInfo) -> None:
    pathurl = file_element.find("pathurl")
    if pathurl is not None:
        pathurl.text = path_to_url(media.path)
    duration = file_element.find("duration")
    if duration is not None:
        file_rate = int_text(file_element, "./rate/timebase", 0)
        if file_rate > 0:
            duration.text = str(round(media.duration_seconds * file_rate))


def replace_names(root: ET.Element, name_mapping: dict[str, str]) -> None:
    for name in root.findall(".//name"):
        if name.text in name_mapping:
            name.text = name_mapping[name.text]


def local_path_from_url(url: str) -> Path | None:
    if not url.startswith("file:"):
        return None
    return Path(urllib.parse.unquote(urllib.parse.urlparse(url).path))


def validate_result(root: ET.Element, changed_clip_ids: set[int]) -> list[str]:
    errors: list[str] = []
    main_sequence = root.find("./sequence")
    if main_sequence is None:
        return ["XML đầu ra không có main sequence"]
    tracks = main_sequence.findall("./media/video/track") + main_sequence.findall("./media/audio/track")
    for track in tracks:
        for clip in track.findall("clipitem"):
            start, end = int_text(clip, "start", -1), int_text(clip, "end", -1)
            if start >= 0 and end >= 0 and end <= start:
                errors.append(f"Clip {clip.findtext('name')!r} có start/end không hợp lệ")
            if id(clip) in changed_clip_ids:
                source_in, source_out = int_text(clip, "in", -1), int_text(clip, "out", -1)
                if source_in < 0 or source_out <= source_in:
                    errors.append(f"Clip {clip.findtext('name')!r} có in/out không hợp lệ")
    missing: set[Path] = set()
    for pathurl in root.findall(".//pathurl"):
        if pathurl.text:
            path = local_path_from_url(pathurl.text)
            if path is not None and not path.exists():
                missing.add(path)
    if missing:
        preview = ", ".join(str(path) for path in sorted(missing)[:5])
        errors.append(f"Còn {len(missing)} đường dẫn media không tồn tại: {preview}")
    return errors


def write_xml_atomic(root: ET.Element, output_path: Path) -> None:
    ET.indent(root, space="\t")
    body = ET.tostring(root, encoding="utf-8")
    payload = b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n' + body + b"\n"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=output_path.name + ".", suffix=".tmp", dir=output_path.parent)
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


def build_template_tokens(
    root: ET.Element,
    main_clips: list[ET.Element],
    original: dict[int, dict[str, int | str]],
    v1: ET.Element,
    a1: ET.Element,
) -> list[TemplateToken]:
    tokens: list[TemplateToken] = []
    for clip in v1.findall("clipitem"):
        state = original[id(clip)]
        name = str(state["name"])
        number = source_th_number(name)
        nest_name: str | None = None
        if number is None:
            match = re.fullmatch(r"Nested Sequence (\d{2})", name)
            if match and 1 <= int(match.group(1)) <= 11:
                number = 4
                nest_name = name
        if number is None:
            continue
        related = [
            candidate
            for candidate in main_clips
            if original[id(candidate)]["name"] == name
            and original[id(candidate)]["start"] == state["start"]
            and original[id(candidate)]["end"] == state["end"]
            and original[id(candidate)]["in"] == state["in"]
            and original[id(candidate)]["out"] == state["out"]
        ]
        tokens.append(
            TemplateToken(
                kind="video",
                th_number=number,
                old_start=int(state["start"]),
                old_end=int(state["end"]),
                clips=related,
                old_name=name,
                nest_name=nest_name,
            )
        )

    voice_names = unique_in_order(
        clip.findtext("name") or ""
        for clip in a1.findall("clipitem")
        if is_voiceover_name(clip.findtext("name") or "")
    )
    for name in voice_names:
        clips = [clip for clip in a1.findall("clipitem") if (clip.findtext("name") or "") == name]
        clips.sort(key=lambda clip: int(original[id(clip)]["start"]))
        tokens.append(
            TemplateToken(
                kind="audio",
                th_number=None,
                old_start=int(original[id(clips[0])]["start"]),
                old_end=max(int(original[id(clip)]["end"]) for clip in clips),
                clips=clips,
                old_name=name,
            )
        )
    return sorted(tokens, key=lambda token: (token.old_start, token.old_end))


def align_tokens(
    template_tokens: list[TemplateToken], script_tokens: list[ScriptToken]
) -> tuple[dict[int, int], dict[int, int]]:
    matcher = difflib.SequenceMatcher(
        a=[token.key for token in template_tokens],
        b=[token.key for token in script_tokens],
        autojunk=False,
    )
    template_to_script: dict[int, int] = {}
    script_to_template: dict[int, int] = {}
    for a_start, b_start, size in matcher.get_matching_blocks():
        for offset in range(size):
            template_index = a_start + offset
            script_index = b_start + offset
            template_to_script[template_index] = script_index
            script_to_template[script_index] = template_index
    return template_to_script, script_to_template


def remove_clip_from_tracks(tracks: list[ET.Element], clip: ET.Element) -> None:
    for track in tracks:
        if clip in list(track):
            track.remove(clip)
            return


class XmlIdAllocator:
    def __init__(self, root: ET.Element) -> None:
        self.used = {element.get("id") for element in root.iter() if element.get("id")}
        self.counters: dict[str, int] = {}

    def next(self, prefix: str) -> str:
        number = self.counters.get(prefix, 1)
        while f"{prefix}-{number}" in self.used:
            number += 1
        value = f"{prefix}-{number}"
        self.counters[prefix] = number + 1
        self.used.add(value)
        return value


def clear_links(clip: ET.Element) -> None:
    for link in clip.findall("link"):
        clip.remove(link)


def add_av_links(video_clip: ET.Element, audio_clip: ET.Element) -> None:
    video_id, audio_id = video_clip.get("id"), audio_clip.get("id")
    for owner in (video_clip, audio_clip):
        clear_links(owner)
        for reference, media_type in ((video_id, "video"), (audio_id, "audio")):
            link = ET.SubElement(owner, "link")
            ET.SubElement(link, "linkclipref").text = reference
            ET.SubElement(link, "mediatype").text = media_type
            ET.SubElement(link, "trackindex").text = "1"
            ET.SubElement(link, "clipindex").text = "1"


def refresh_main_links(video_tracks: list[ET.Element], audio_tracks: list[ET.Element]) -> None:
    metadata: dict[str, tuple[str, int, int]] = {}
    for media_type, tracks in (("video", video_tracks), ("audio", audio_tracks)):
        for track_index, track in enumerate(tracks, 1):
            for clip_index, clip in enumerate(track.findall("clipitem"), 1):
                if clip.get("id"):
                    metadata[clip.get("id")] = (media_type, track_index, clip_index)
    for tracks in (video_tracks, audio_tracks):
        for track in tracks:
            for clip in track.findall("clipitem"):
                for link in list(clip.findall("link")):
                    reference = link.findtext("linkclipref") or ""
                    if reference not in metadata:
                        clip.remove(link)
                        continue
                    media_type, track_index, clip_index = metadata[reference]
                    set_text(link, "mediatype", media_type)
                    set_text(link, "trackindex", track_index)
                    set_text(link, "clipindex", clip_index)


def remove_collapsed_items(tracks: list[ET.Element]) -> None:
    for track in tracks:
        for tag in ("clipitem", "transitionitem"):
            for item in list(track.findall(tag)):
                start, end = int_text(item, "start", -1), int_text(item, "end", -1)
                if start >= 0 and end >= 0 and end <= start:
                    track.remove(item)


def update_nested_cut(
    root: ET.Element,
    nest_name: str,
    source_in: int,
    source_out: int,
    video: MediaInfo,
    fps: int,
    changed_clip_ids: set[int],
) -> None:
    nested = full_sequence_by_name(root, nest_name)
    if nested is None:
        raise GeneratorError(f"Không tìm thấy nội dung của {nest_name}")
    duration = source_out - source_in
    set_text(nested, "duration", duration)
    tracks = nested.findall("./media/video/track") + nested.findall("./media/audio/track")
    for track in tracks:
        for inner in track.findall("clipitem"):
            if source_th_number(inner.findtext("name") or "") == 4:
                set_timeline_range(inner, 0, duration)
                set_source_range(inner, source_in, source_out, video.frames, fps)
                changed_clip_ids.add(id(inner))


def find_video_prototypes(
    root: ET.Element,
    template_tokens: list[TemplateToken],
    th_number: int,
) -> tuple[ET.Element, ET.Element]:
    main_sequence = root.find("./sequence")
    main_v1 = main_sequence.find("./media/video/track") if main_sequence is not None else None
    main_a1 = main_sequence.find("./media/audio/track") if main_sequence is not None else None
    for token in template_tokens:
        if token.kind == "video" and token.th_number == th_number and token.nest_name is None:
            video = next(
                (clip for clip in token.clips if main_v1 is not None and clip in list(main_v1)),
                None,
            )
            audio = next(
                (clip for clip in token.clips if main_a1 is not None and clip in list(main_a1)),
                None,
            )
            if video is not None and audio is not None:
                return video, audio
    if th_number == 4:
        nested = full_sequence_by_name(root, "Nested Sequence 04")
        if nested is not None:
            video = nested.find("./media/video/track/clipitem")
            audio = nested.find("./media/audio/track/clipitem")
            if video is not None and audio is not None:
                return video, audio
    raise GeneratorError(f"Template không có prototype video/audio cho TH{th_number}")


def process_flexible_timeline(
    root: ET.Element,
    main_sequence: ET.Element,
    video_tracks: list[ET.Element],
    audio_tracks: list[ET.Element],
    main_clips: list[ET.Element],
    original: dict[int, dict[str, int | str]],
    script_tokens: list[ScriptToken],
    videos: dict[int, MediaInfo],
    audios: list[MediaInfo],
    fixed_dir: Path,
    output_path: Path,
    fps: int,
    dry_run: bool,
) -> None:
    v1, a1 = video_tracks[0], audio_tracks[0]
    all_tracks = video_tracks + audio_tracks
    template_tokens = build_template_tokens(root, main_clips, original, v1, a1)
    template_to_script, script_to_template = align_tokens(template_tokens, script_tokens)

    events: list[EditEvent] = []
    template_labels: dict[int, str] = {}
    script_labels: dict[int, str] = {}
    for template_index, template_token in enumerate(template_tokens):
        script_index = template_to_script.get(template_index)
        label = f"template-{template_index}"
        template_labels[template_index] = label
        if script_index is None:
            new_duration = 0
        else:
            script_labels[script_index] = label
            new_duration = script_tokens[script_index].duration(audios)
        events.append(
            EditEvent(
                template_token.old_start,
                template_token.old_end,
                new_duration,
                label,
            )
        )

    matched_script_indices = sorted(script_to_template)
    for script_index, script_token in enumerate(script_tokens):
        if script_index in script_to_template:
            continue
        next_match = next((index for index in matched_script_indices if index > script_index), None)
        previous_match = next((index for index in reversed(matched_script_indices) if index < script_index), None)
        if next_match is not None:
            anchor = template_tokens[script_to_template[next_match]].old_start
        elif previous_match is not None:
            anchor = template_tokens[script_to_template[previous_match]].old_end
        elif template_tokens:
            anchor = template_tokens[0].old_start
        else:
            anchor = 0
        label = f"insert-{script_index}"
        script_labels[script_index] = label
        events.append(EditEvent(anchor, anchor, script_token.duration(audios), label))

    events = sort_edit_events(events)
    validate_edit_events(events)
    new_starts = event_new_starts(events)
    remap_main_sequence(main_sequence, events)

    changed_clip_ids: set[int] = set()
    voiceover_media_by_old_name: dict[str, MediaInfo] = {}

    for template_index, template_token in enumerate(template_tokens):
        script_index = template_to_script.get(template_index)
        if script_index is None:
            for clip in template_token.clips:
                remove_clip_from_tracks(all_tracks, clip)
            continue

        script_token = script_tokens[script_index]
        duration = script_token.duration(audios)
        start = new_starts[template_labels[template_index]]
        if script_token.kind == "video":
            if script_token.th_number is None:
                raise GeneratorError("Video token thiếu số tình huống")
            media = videos[script_token.th_number]
            if template_token.nest_name:
                update_nested_cut(
                    root,
                    template_token.nest_name,
                    script_token.source_in,
                    script_token.source_out,
                    media,
                    fps,
                    changed_clip_ids,
                )
            for clip in template_token.clips:
                set_timeline_range(clip, start, duration)
                if template_token.nest_name:
                    set_source_range(clip, 0, duration, duration, fps)
                else:
                    set_source_range(
                        clip,
                        script_token.source_in,
                        script_token.source_out,
                        media.frames,
                        fps,
                    )
                changed_clip_ids.add(id(clip))
        else:
            if script_token.audio_index is None:
                raise GeneratorError("Voice-over token thiếu audio index")
            media = audios[script_token.audio_index]
            voiceover_media_by_old_name[template_token.old_name] = media
            first = template_token.clips[0]
            set_timeline_range(first, start, duration)
            set_source_range(first, 0, duration, duration, fps)
            changed_clip_ids.add(id(first))
            for extra in template_token.clips[1:]:
                remove_clip_from_tracks(audio_tracks, extra)

    allocator = XmlIdAllocator(root)
    audio_template_tokens = [token for token in template_tokens if token.kind == "audio"]
    for script_index, script_token in enumerate(script_tokens):
        if script_index in script_to_template:
            continue
        start = new_starts[script_labels[script_index]]
        duration = script_token.duration(audios)
        if script_token.kind == "video":
            if script_token.th_number is None:
                raise GeneratorError("Video token chèn thêm thiếu số tình huống")
            video_prototype, audio_prototype = find_video_prototypes(
                root, template_tokens, script_token.th_number
            )
            video_clip, audio_clip = copy.deepcopy(video_prototype), copy.deepcopy(audio_prototype)
            video_clip.set("id", allocator.next("clipitem"))
            audio_clip.set("id", allocator.next("clipitem"))
            media = videos[script_token.th_number]
            for clip in (video_clip, audio_clip):
                replace_names(clip, {video_prototype.findtext("name") or "": media.path.name})
                file_element = clip.find("file")
                if file_element is not None and file_element.find("pathurl") is not None:
                    file_name = file_element.find("name")
                    if file_name is not None:
                        file_name.text = media.path.name
                    update_file_definition(file_element, media)
                set_timeline_range(clip, start, duration)
                set_source_range(
                    clip,
                    script_token.source_in,
                    script_token.source_out,
                    media.frames,
                    fps,
                )
                changed_clip_ids.add(id(clip))
            add_av_links(video_clip, audio_clip)
            v1.append(video_clip)
            a1.append(audio_clip)
        else:
            if script_token.audio_index is None or not audio_template_tokens:
                raise GeneratorError("Không có prototype để chèn voice-over")
            media = audios[script_token.audio_index]
            nearest = min(
                audio_template_tokens,
                key=lambda token: abs(token.old_start - map_position(start, events)),
            )
            clip = copy.deepcopy(nearest.clips[0])
            clip.set("id", allocator.next("clipitem"))
            masterclip = clip.find("masterclipid")
            if masterclip is not None:
                masterclip.text = allocator.next("masterclip")
            file_element = clip.find("file")
            if file_element is None or file_element.find("pathurl") is None:
                # First clips of template VO groups normally contain the full file definition.
                source_file = next(
                    (candidate.find("file") for candidate in nearest.clips if candidate.find("file/pathurl") is not None),
                    None,
                )
                if source_file is None:
                    raise GeneratorError("Prototype voice-over thiếu file definition")
                file_index = list(clip).index(file_element) if file_element is not None else len(list(clip))
                if file_element is not None:
                    clip.remove(file_element)
                file_element = copy.deepcopy(source_file)
                clip.insert(file_index, file_element)
            file_element.set("id", allocator.next("file"))
            file_name = file_element.find("name")
            if file_name is not None:
                file_name.text = media.path.name
            update_file_definition(file_element, media)
            clip_name = clip.find("name")
            if clip_name is not None:
                clip_name.text = media.path.name
            clear_links(clip)
            set_timeline_range(clip, start, duration)
            set_source_range(clip, 0, duration, duration, fps)
            changed_clip_ids.add(id(clip))
            a1.append(clip)

    old_total = int_text(main_sequence, "duration", 0)
    new_total = map_position(old_total, events)
    set_text(main_sequence, "duration", new_total)
    remove_collapsed_items(all_tracks)
    refresh_main_links(video_tracks, audio_tracks)

    fixed_index = build_fixed_index(fixed_dir)
    source_name_mapping: dict[str, str] = {}
    for file_element in root.findall(".//file"):
        old_name = file_element.findtext("name") or ""
        media: MediaInfo | None = None
        number = source_th_number(old_name)
        if number in videos:
            media = videos[number]
        elif old_name in voiceover_media_by_old_name:
            media = voiceover_media_by_old_name[old_name]
        elif old_name in fixed_index:
            pathurl = file_element.find("pathurl")
            if pathurl is not None:
                pathurl.text = path_to_url(fixed_index[old_name])
        if media is not None:
            source_name_mapping[old_name] = media.path.name
            update_file_definition(file_element, media)
    replace_names(root, source_name_mapping)

    validation_errors = validate_result(root, changed_clip_ids)
    if validation_errors:
        raise GeneratorError("XML sau xử lý không hợp lệ:\n  - " + "\n  - ".join(validation_errors))
    matched = len(script_to_template)
    inserted = len(script_tokens) - matched
    removed = len(template_tokens) - matched
    print(
        f"[FLEX] {matched} slot dùng lại, {inserted} slot thêm, {removed} slot bỏ; "
        f"{len(script_tokens)} token nội dung"
    )
    print(
        f"[TIMELINE] {old_total / fps:.2f}s -> {new_total / fps:.2f}s "
        f"({(new_total - old_total) / fps:+.2f}s), {len(events)} vùng ripple"
    )
    print(f"[VALIDATE] OK: {len(audios)} voice-over, media đầy đủ")
    if dry_run:
        print("[DRY RUN] Không ghi file XML")
        return
    write_xml_atomic(root, output_path)
    print(f"[OUTPUT] {output_path} ({output_path.stat().st_size / 1024 / 1024:.2f} MB)")


def process_xml(
    template_path: Path,
    docx_path: Path,
    video_dir: Path,
    audio_dir: Path,
    fixed_dir: Path,
    output_path: Path,
    fps: int,
    dry_run: bool,
) -> None:
    cut_lists, narration_count, script_tokens = parse_script(docx_path, fps)
    videos, audios = discover_media(video_dir, audio_dir, fps)
    if set(videos) != set(range(1, 7)):
        found = ", ".join(f"TH{number}" for number in sorted(videos)) or "không có"
        raise GeneratorError(f"Cần đúng TH1-TH6; hiện tìm thấy: {found}")
    if len(audios) != narration_count:
        raise GeneratorError(f"DOCX có {narration_count} câu đọc nhưng thư mục audio có {len(audios)} file")
    for number, cuts in cut_lists.items():
        for _, source_out in cuts:
            if source_out > videos[number].frames:
                raise GeneratorError(
                    f"Timecode TH{number} kết thúc ở {source_out / fps:.2f}s, "
                    f"vượt thời lượng video {videos[number].duration_seconds:.2f}s"
                )

    try:
        root = ET.parse(template_path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise GeneratorError(f"Không đọc được template XML: {template_path}") from exc
    main_sequence = root.find("./sequence")
    if main_sequence is None:
        raise GeneratorError("Template không có main sequence ở ./sequence")
    video_tracks = main_sequence.findall("./media/video/track")
    audio_tracks = main_sequence.findall("./media/audio/track")
    if not video_tracks or not audio_tracks:
        raise GeneratorError("Template thiếu V1 hoặc A1")
    v1, a1 = video_tracks[0], audio_tracks[0]

    main_clips = [clip for track in video_tracks + audio_tracks for clip in track.findall("clipitem")]
    original = {
        id(clip): {
            "name": clip.findtext("name") or "",
            "start": int_text(clip, "start", -1),
            "end": int_text(clip, "end", -1),
            "in": int_text(clip, "in", 0),
            "out": int_text(clip, "out", 0),
        }
        for clip in main_clips
    }

    template_tokens = build_template_tokens(root, main_clips, original, v1, a1)
    if [token.key for token in template_tokens] != [token.key for token in script_tokens]:
        process_flexible_timeline(
            root=root,
            main_sequence=main_sequence,
            video_tracks=video_tracks,
            audio_tracks=audio_tracks,
            main_clips=main_clips,
            original=original,
            script_tokens=script_tokens,
            videos=videos,
            audios=audios,
            fixed_dir=fixed_dir,
            output_path=output_path,
            fps=fps,
            dry_run=dry_run,
        )
        return
    events: list[EditEvent] = []
    changed_clip_ids: set[int] = set()

    direct_by_th: dict[int, list[ET.Element]] = {number: [] for number in range(1, 7)}
    for clip in v1.findall("clipitem"):
        number = source_th_number(original[id(clip)]["name"])
        if number is not None:
            direct_by_th[number].append(clip)

    cut_mapping: dict[tuple[int, int, int], tuple[int, int]] = {}
    for number in (1, 2, 3, 5, 6):
        clips = direct_by_th[number]
        desired = cut_lists[number]
        if len(clips) != len(desired):
            raise GeneratorError(f"Template có {len(clips)} đoạn TH{number}, DOCX yêu cầu {len(desired)} đoạn")
        for index, (clip, (source_in, source_out)) in enumerate(zip(clips, desired), 1):
            state = original[id(clip)]
            cut_mapping[(number, state["in"], state["out"])] = (source_in, source_out)
            events.append(EditEvent(state["start"], state["end"], source_out - source_in, f"TH{number} cut {index}"))

    th4_outer: list[tuple[int, ET.Element]] = []
    for clip in v1.findall("clipitem"):
        match = re.fullmatch(r"Nested Sequence (\d{2})", original[id(clip)]["name"])
        if match and 1 <= int(match.group(1)) <= 11:
            th4_outer.append((int(match.group(1)), clip))
    th4_outer.sort(key=lambda item: item[0])
    if len(th4_outer) != len(cut_lists[4]):
        raise GeneratorError(f"Template có {len(th4_outer)} nested cut TH4, DOCX yêu cầu {len(cut_lists[4])} đoạn")
    nested_ranges: dict[str, tuple[int, int]] = {}
    for (nest_number, clip), (source_in, source_out) in zip(th4_outer, cut_lists[4]):
        state = original[id(clip)]
        nest_name = f"Nested Sequence {nest_number:02d}"
        duration = source_out - source_in
        nested_ranges[nest_name] = (source_in, source_out)
        events.append(EditEvent(state["start"], state["end"], duration, f"TH4 {nest_name}"))
        nested = full_sequence_by_name(root, nest_name)
        if nested is None:
            raise GeneratorError(f"Không tìm thấy nội dung của {nest_name}")
        set_text(nested, "duration", duration)
        for track in nested.findall("./media/video/track") + nested.findall("./media/audio/track"):
            for inner in track.findall("clipitem"):
                if source_th_number(inner.findtext("name") or "") == 4:
                    set_timeline_range(inner, 0, duration)
                    set_source_range(inner, source_in, source_out, videos[4].frames, fps)
                    changed_clip_ids.add(id(inner))

    voiceover_names = unique_in_order(
        clip.findtext("name") or "" for clip in a1.findall("clipitem")
        if is_voiceover_name(clip.findtext("name") or "")
    )
    if len(voiceover_names) != len(audios):
        raise GeneratorError(f"Template có {len(voiceover_names)} vị trí voice-over nhưng đầu vào có {len(audios)} file")

    voiceover_groups: list[tuple[str, list[ET.Element], MediaInfo]] = []
    for old_name, media in zip(voiceover_names, audios):
        clips = [clip for clip in a1.findall("clipitem") if clip.findtext("name") == old_name]
        clips.sort(key=lambda clip: original[id(clip)]["start"])
        old_start = original[id(clips[0])]["start"]
        old_end = max(original[id(clip)]["end"] for clip in clips)
        events.append(EditEvent(old_start, old_end, media.frames, f"VO {media.path.name}"))
        voiceover_groups.append((old_name, clips, media))

    events = sort_edit_events(events)
    validate_edit_events(events)
    remap_main_sequence(main_sequence, events)

    for clip in main_clips:
        state = original[id(clip)]
        number = source_th_number(state["name"])
        if number not in (1, 2, 3, 5, 6):
            continue
        desired = cut_mapping.get((number, state["in"], state["out"]))
        if desired is None:
            continue
        source_in, source_out = desired
        set_source_range(clip, source_in, source_out, videos[number].frames, fps)
        changed_clip_ids.add(id(clip))

    for clip in main_clips:
        state = original[id(clip)]
        if state["name"] not in nested_ranges:
            continue
        source_in, source_out = nested_ranges[state["name"]]
        duration = source_out - source_in
        set_timeline_range(clip, map_position(state["start"], events), duration)
        set_source_range(clip, 0, duration, duration, fps)
        changed_clip_ids.add(id(clip))

    voiceover_media_by_old_name: dict[str, MediaInfo] = {}
    for old_name, clips, media in voiceover_groups:
        voiceover_media_by_old_name[old_name] = media
        first = clips[0]
        new_start = map_position(original[id(first)]["start"], events)
        set_timeline_range(first, new_start, media.frames)
        set_source_range(first, 0, media.frames, media.frames, fps)
        changed_clip_ids.add(id(first))
        for extra in clips[1:]:
            a1.remove(extra)

    old_total = int_text(main_sequence, "duration", 0)
    new_total = map_position(old_total, events)
    set_text(main_sequence, "duration", new_total)

    fixed_index = build_fixed_index(fixed_dir)
    source_name_mapping: dict[str, str] = {}
    for file_element in root.findall(".//file"):
        old_name = file_element.findtext("name") or ""
        media: MediaInfo | None = None
        number = source_th_number(old_name)
        if number in videos:
            media = videos[number]
        elif old_name in voiceover_media_by_old_name:
            media = voiceover_media_by_old_name[old_name]
        elif old_name in fixed_index:
            pathurl = file_element.find("pathurl")
            if pathurl is not None:
                pathurl.text = path_to_url(fixed_index[old_name])
        if media is not None:
            source_name_mapping[old_name] = media.path.name
            update_file_definition(file_element, media)
    replace_names(root, source_name_mapping)

    validation_errors = validate_result(root, changed_clip_ids)
    if validation_errors:
        raise GeneratorError("XML sau xử lý không hợp lệ:\n  - " + "\n  - ".join(validation_errors))

    delta_seconds = (new_total - old_total) / fps
    print(f"[TIMELINE] {old_total / fps:.2f}s -> {new_total / fps:.2f}s ({delta_seconds:+.2f}s), {len(events)} vùng ripple")
    print(f"[VALIDATE] OK: {len(cut_lists)} tình huống, {len(audios)} voice-over, media đầy đủ")
    if dry_run:
        print("[DRY RUN] Không ghi file XML")
        return
    write_xml_atomic(root, output_path)
    print(f"[OUTPUT] {output_path} ({output_path.stat().st_size / 1024 / 1024:.2f} MB)")


def existing_directory(path: Path, label: str) -> Path:
    if not path.is_dir():
        raise GeneratorError(f"{label} không tồn tại hoặc không phải thư mục: {path}")
    return path


def existing_file(path: Path, label: str) -> Path:
    if not path.is_file():
        raise GeneratorError(f"{label} không tồn tại: {path}")
    return path


def resolve_arguments(args: argparse.Namespace) -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent.parent
    project_dir = Path(args.project_dir).expanduser().resolve() if args.project_dir else None
    if project_dir:
        existing_directory(project_dir, "Project dir")
    if args.content:
        content = Path(args.content).expanduser().resolve()
    elif project_dir:
        candidates = sorted(project_dir.glob("*.docx"), key=lambda path: natural_sort_key(path.name))
        if not candidates:
            raise GeneratorError(f"Không tìm thấy file DOCX trong {project_dir}")
        content = candidates[0]
    else:
        raise GeneratorError("Thiếu --content hoặc --project-dir")

    video_dir = Path(args.video_dir).expanduser().resolve() if args.video_dir else (
        project_dir / "video" if project_dir else repo_root / "Copied_28" / "video"
    )
    audio_dir = Path(args.audio_dir).expanduser().resolve() if args.audio_dir else (
        project_dir / "audio" if project_dir else repo_root / "Copied_28" / "audio"
    )
    fixed_dir = Path(args.fixed_dir).expanduser().resolve() if args.fixed_dir else (
        project_dir / "fixed" if project_dir and (project_dir / "fixed").is_dir() else repo_root / "Copied_28"
    )
    template = Path(args.template).expanduser().resolve() if args.template else (
        fixed_dir / "28.8.xml" if (fixed_dir / "28.8.xml").is_file() else repo_root / "Copied_28" / "28.8.xml"
    )
    output = Path(args.output).expanduser().resolve() if args.output else (
        project_dir / "output.xml" if project_dir else None
    )
    if output is None and not args.dry_run:
        raise GeneratorError("Thiếu --output khi không dùng --project-dir")
    if output is None:
        output = Path.cwd() / "output.xml"

    args.content = existing_file(content, "Content DOCX")
    args.video_dir = existing_directory(video_dir, "Video dir")
    args.audio_dir = existing_directory(audio_dir, "Audio dir")
    args.fixed_dir = existing_directory(fixed_dir, "Fixed dir")
    args.template = existing_file(template, "Template XML")
    args.output = output
    return args


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Tạo Premiere Pro XML từ DOCX + video + voice-over")
    parser.add_argument("--project-dir", help="Thư mục có content.docx, video/, audio/ và tùy chọn fixed/")
    parser.add_argument("--content", help="Đường dẫn content.docx")
    parser.add_argument("--video-dir", help="Thư mục chứa 6 video TH1-TH6")
    parser.add_argument("--audio-dir", help="Thư mục chứa voice-over theo thứ tự kịch bản")
    parser.add_argument("--fixed-dir", help="Thư mục fixed assets; mặc định là Copied_28")
    parser.add_argument("--template", help="Premiere XML template; mặc định là fixed/28.8.xml")
    parser.add_argument("--output", help="XML đầu ra; mặc định project-dir/output.xml")
    parser.add_argument("--fps", type=int, default=60, help="Frame rate timeline (mặc định: 60)")
    parser.add_argument("--dry-run", action="store_true", help="Kiểm tra toàn bộ nhưng không ghi XML")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.fps <= 0:
        parser.error("--fps phải lớn hơn 0")
    try:
        args = resolve_arguments(args)
        print("=" * 68)
        print("Auto Premiere Pro XML Generator")
        print("=" * 68)
        print(f"[SCRIPT] {args.content}")
        print(f"[TEMPLATE] {args.template}")
        process_xml(
            template_path=args.template,
            docx_path=args.content,
            video_dir=args.video_dir,
            audio_dir=args.audio_dir,
            fixed_dir=args.fixed_dir,
            output_path=args.output,
            fps=args.fps,
            dry_run=args.dry_run,
        )
        return 0
    except GeneratorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
