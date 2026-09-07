#!/usr/bin/env python3
"""Create a CapCut Desktop draft from a DOCX script and local media.

The DOCX is the timeline authority. Narrative paragraphs become sequential
voice-over clips; rows containing timecodes become trimmed source-video clips.
The generated draft can reference original media or bundle local copies inside
the draft. Direct installation bundles media by default so CapCut is not
blocked by macOS privacy permissions for folders such as Documents.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from docx import Document


VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".m4v", ".avi", ".webm"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
TIMECODE_RE = re.compile(r"(\d+):(\d+)\s*[-\u2013]\s*(\d+):(\d+)")
SCENARIO_RE = re.compile(r"vào\s+tình\s+huống\s*(\d+)", re.IGNORECASE)
VIDEO_NUMBER_RE = re.compile(r"vid(?:eo)?\s*(\d+)", re.IGNORECASE)
TH_NUMBER_RE = re.compile(r"(?:^|\b)TH\s*(\d+)(?:\b|$)", re.IGNORECASE)
CORRECTION_RE = re.compile(r"^sửa\s*:\s*", re.IGNORECASE)
VERDICT_LEAD_RE = re.compile(
    r"\b(?:a\s+charge|charges|multiple\s+counts)\s+of\b", re.IGNORECASE
)
VERDICT_CLAUSE_RE = re.compile(
    r",\s*(?=(?:Aggravated|Criminal|Menacing|along\s+with|plus|"
    r"and\s+(?:Resisting|Two|an\s+instant))\b)",
    re.IGNORECASE,
)

DEFAULT_PROJECT_NAME = "Bài Karen 0509"
DEFAULT_CONTENT_NAME = "content.docx"
DEFAULT_FPS = 60
MICROSECONDS = 1_000_000


class GeneratorError(RuntimeError):
    """An input or draft error that can be shown directly to the user."""


@dataclass(frozen=True)
class TimelineToken:
    kind: str
    location: str
    section: str
    text: str = ""
    audio_index: int | None = None
    source_number: int | None = None
    source_in_seconds: float = 0.0
    source_out_seconds: float = 0.0


@dataclass(frozen=True)
class MediaInfo:
    path: Path
    duration_seconds: float
    width: int = 0
    height: int = 0
    has_audio: bool = False


@dataclass(frozen=True)
class MediaIdentity:
    """CapCut identifiers shared by every clip made from one source file."""

    unique_id: str
    local_material_id: str


@dataclass(frozen=True)
class PlacedToken:
    token: TimelineToken
    timeline_start_us: int
    duration_us: int
    media: MediaInfo


@dataclass(frozen=True)
class GenerationSummary:
    output: Path
    canonical_file: str
    video_dir: Path
    audio_dir: Path
    video_count: int
    audio_count: int
    video_cut_count: int
    duration_us: int
    fps: int
    installed: bool
    bundled: bool
    blank_count: int


@dataclass(frozen=True)
class MediaCopy:
    source: Path
    destination: Path


@dataclass(frozen=True)
class GeneratedVideo:
    destination: Path
    duration_us: int
    width: int = 1920
    height: int = 1080


def uid() -> str:
    return str(uuid.uuid4())


def nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def one_line(value: str) -> str:
    return " ".join(value.split())


def natural_sort_key(value: object) -> list[object]:
    return [
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"(\d+)", str(value))
    ]


def find_named_child(directory: Path, expected_name: str) -> Path | None:
    expected = nfc(expected_name).casefold()
    for path in directory.iterdir():
        if nfc(path.name).casefold() == expected:
            return path
    return None


def existing_file(path: Path, label: str) -> Path:
    if not path.is_file():
        raise GeneratorError(f"{label} không tồn tại: {path}")
    return path.resolve()


def existing_directory(path: Path, label: str) -> Path:
    if not path.is_dir():
        raise GeneratorError(f"{label} không tồn tại: {path}")
    return path.resolve()


def resolve_default_project() -> Path:
    repository = Path(__file__).resolve().parent.parent
    for base in (Path.cwd(), repository):
        if base.is_dir() and nfc(base.name) == nfc(DEFAULT_PROJECT_NAME):
            return base.resolve()
        if base.is_dir():
            match = find_named_child(base, DEFAULT_PROJECT_NAME)
            if match is not None and match.is_dir():
                return match.resolve()
    raise GeneratorError(
        f"Không tự tìm thấy project {DEFAULT_PROJECT_NAME!r}; hãy truyền --project-dir"
    )


def parse_timecodes(text: str) -> list[tuple[float, float]]:
    result: list[tuple[float, float]] = []
    for match in TIMECODE_RE.finditer(text):
        start_minute, start_second, end_minute, end_second = map(int, match.groups())
        start = start_minute * 60 + start_second
        end = end_minute * 60 + end_second
        if start_second >= 60 or end_second >= 60 or end <= start:
            raise GeneratorError(f"Timecode không hợp lệ: {match.group(0)!r}")
        result.append((float(start), float(end)))
    return result


def split_verdict(text: str) -> list[str]:
    """Split a verdict into the narration chunks used by this content format."""
    if ":" in text:
        lead, remainder = text.split(":", 1)
        chunks = [lead.strip() + ":"]
    else:
        match = VERDICT_LEAD_RE.search(text)
        if match is None:
            return [text]
        chunks = [text[: match.end()].strip()]
        remainder = text[match.end() :]
    chunks.extend(part.strip() for part in VERDICT_CLAUSE_RE.split(remainder) if part.strip())
    return chunks


def narrative_paragraphs(cell: Any, row_type: str) -> list[str]:
    paragraphs = [one_line(paragraph.text) for paragraph in cell.paragraphs if paragraph.text.strip()]
    corrections = [
        CORRECTION_RE.sub("", paragraph).strip()
        for paragraph in paragraphs
        if CORRECTION_RE.match(paragraph)
    ]
    if corrections:
        paragraphs = corrections
    paragraphs = [re.sub(r"^BỊA\s*:\s*", "", text, flags=re.IGNORECASE) for text in paragraphs]
    if row_type.casefold() == "kết tội":
        return [chunk for paragraph in paragraphs for chunk in split_verdict(paragraph)]
    return paragraphs


def parse_docx_timeline(docx_path: Path) -> list[TimelineToken]:
    try:
        document = Document(docx_path)
    except Exception as exc:
        raise GeneratorError(f"Không đọc được DOCX: {docx_path}") from exc
    if not document.tables:
        raise GeneratorError("DOCX không có bảng nội dung")

    tokens: list[TimelineToken] = []
    current_scenario: int | None = None
    current_section: str | None = None
    audio_index = 0
    seen_teasers: set[tuple[int, str]] = set()

    for table_index, table in enumerate(document.tables, 1):
        for row_index, row in enumerate(table.rows, 1):
            if not row.cells:
                continue
            row_type = one_line(row.cells[0].text)
            folded_type = row_type.casefold()
            text = one_line(row.cells[1].text) if len(row.cells) > 1 else ""
            location = f"bảng {table_index}, dòng {row_index}"

            scenario_match = SCENARIO_RE.search(row_type)
            if scenario_match:
                current_scenario = int(scenario_match.group(1))
                current_section = f"TH{current_scenario}"
            elif folded_type == "intro":
                current_section = "Intro"
            elif folded_type in {"kết bài", "outro"}:
                current_section = "Kết bài"

            cuts = parse_timecodes(text)
            if cuts:
                if "đoạn kịch tính" in text.casefold():
                    video_match = VIDEO_NUMBER_RE.search(text)
                    if video_match is None:
                        raise GeneratorError(f"Thiếu số video tại {location}")
                    source_number = int(video_match.group(1))
                elif scenario_match or folded_type in {"nội dung", ""}:
                    if current_scenario is None:
                        raise GeneratorError(f"Không xác định được video nguồn tại {location}")
                    source_number = current_scenario
                else:
                    continue

                if scenario_match:
                    teaser_key = (source_number, text.casefold())
                    if teaser_key in seen_teasers:
                        continue
                    seen_teasers.add(teaser_key)

                for source_in, source_out in cuts:
                    tokens.append(
                        TimelineToken(
                            kind="video",
                            source_number=source_number,
                            source_in_seconds=source_in,
                            source_out_seconds=source_out,
                            section=current_section or f"TH{source_number}",
                            location=location,
                        )
                    )
                continue

            if current_section is None or len(row.cells) < 2 or folded_type == "video":
                continue
            for paragraph in narrative_paragraphs(row.cells[1], row_type):
                audio_index += 1
                tokens.append(
                    TimelineToken(
                        kind="audio",
                        audio_index=audio_index,
                        text=paragraph,
                        section=current_section,
                        location=location,
                    )
                )

    if not tokens:
        raise GeneratorError("Không tìm thấy fragment audio/video trong DOCX")
    if not any(token.kind == "video" for token in tokens):
        raise GeneratorError("Không tìm thấy timecode video trong DOCX")
    if not any(token.kind == "audio" for token in tokens):
        raise GeneratorError("Không tìm thấy nội dung voice-over trong DOCX")
    return tokens


def source_number(path: Path) -> int | None:
    if path.stem.strip().isdigit():
        return int(path.stem.strip())
    match = TH_NUMBER_RE.search(path.stem)
    return int(match.group(1)) if match else None


def media_files(directory: Path, extensions: set[str], recursive: bool) -> list[Path]:
    iterator: Iterable[Path] = directory.rglob("*") if recursive else directory.iterdir()
    return sorted(
        (
            path.resolve()
            for path in iterator
            if path.is_file()
            and not path.name.startswith(".")
            and path.suffix.casefold() in extensions
            and "Adobe Premiere Pro Auto-Save" not in path.parts
        ),
        key=lambda path: natural_sort_key(path.name),
    )


def map_video_files(paths: list[Path], expected_numbers: set[int]) -> dict[int, Path]:
    if len(paths) != len(expected_numbers):
        return {}
    result: dict[int, Path] = {}
    for fallback, path in zip(sorted(expected_numbers), paths, strict=True):
        number = source_number(path) or fallback
        if number not in expected_numbers or number in result:
            return {}
        result[number] = path
    return result if set(result) == expected_numbers else {}


def discover_video_media(
    project_dir: Path, supplied: Path | None, expected_numbers: set[int]
) -> tuple[Path, dict[int, Path]]:
    if supplied is not None:
        root = existing_directory(supplied.expanduser().resolve(), "Video dir")
        for recursive in (False, True):
            paths = media_files(root, VIDEO_EXTENSIONS, recursive)
            mapping = map_video_files(paths, expected_numbers)
            if mapping:
                return root, mapping
        raise GeneratorError(
            f"{root} phải chứa đúng các video nguồn {sorted(expected_numbers)}"
        )

    preferred_names = {"videos", "video", "vides", "vid"}
    candidates = {
        path.parent.resolve()
        for path in project_dir.rglob("*")
        if path.is_file() and path.suffix.casefold() in VIDEO_EXTENSIONS
    }
    candidates.add(project_dir)
    ordered = sorted(
        candidates,
        key=lambda path: (
            0 if path.name.casefold() in preferred_names else 1,
            len(path.relative_to(project_dir).parts),
            natural_sort_key(str(path)),
        ),
    )
    for directory in ordered:
        mapping = map_video_files(media_files(directory, VIDEO_EXTENSIONS, False), expected_numbers)
        if mapping:
            return directory, mapping
    raise GeneratorError(
        f"Không tự tìm thấy thư mục chứa đúng các video nguồn {sorted(expected_numbers)}; "
        "hãy truyền --video-dir"
    )


def discover_audio_media(
    project_dir: Path, supplied: Path | None, expected_count: int
) -> tuple[Path, list[Path]]:
    if supplied is not None:
        root = existing_directory(supplied.expanduser().resolve(), "Audio dir")
        for recursive in (False, True):
            paths = media_files(root, AUDIO_EXTENSIONS, recursive)
            if len(paths) == expected_count:
                return root, paths
        raise GeneratorError(
            f"{root} có số audio khác DOCX ({expected_count} fragment voice-over)"
        )

    groups: dict[Path, list[Path]] = {}
    for path in media_files(project_dir, AUDIO_EXTENSIONS, True):
        groups.setdefault(path.parent, []).append(path)
    exact = [(directory, sorted(paths, key=lambda p: natural_sort_key(p.name))) for directory, paths in groups.items() if len(paths) == expected_count]
    if not exact:
        counts = ", ".join(f"{path}: {len(paths)}" for path, paths in groups.items())
        raise GeneratorError(
            f"Không tìm thấy thư mục có đúng {expected_count} audio"
            + (f" ({counts})" if counts else "")
            + "; hãy truyền --audio-dir"
        )
    audio_names = {"audio", "audios", "au"}
    exact.sort(
        key=lambda item: (
            0 if any(part.casefold() in audio_names or part.casefold().startswith("au ") for part in item[0].parts) else 1,
            len(item[0].relative_to(project_dir).parts),
            natural_sort_key(str(item[0])),
        )
    )
    return exact[0]


def run_ffprobe(path: Path) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        return json.loads(completed.stdout)
    except FileNotFoundError as exc:
        raise GeneratorError("Không tìm thấy ffprobe; hãy cài ffmpeg trước") from exc
    except (subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise GeneratorError(f"Không đọc được media bằng ffprobe: {path}") from exc


def probe_one(path: Path, require_video: bool) -> MediaInfo:
    data = run_ffprobe(path)
    streams = data.get("streams", [])
    video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    if require_video and video_stream is None:
        raise GeneratorError(f"File không có video stream: {path}")
    duration_value = data.get("format", {}).get("duration")
    if duration_value is None:
        duration_value = next((stream.get("duration") for stream in streams if stream.get("duration")), None)
    try:
        duration = float(duration_value)
    except (TypeError, ValueError) as exc:
        raise GeneratorError(f"Không xác định được thời lượng media: {path}") from exc
    if duration <= 0:
        raise GeneratorError(f"Media có thời lượng không hợp lệ: {path}")
    return MediaInfo(
        path=path.resolve(),
        duration_seconds=duration,
        width=int(video_stream.get("width", 0)) if video_stream else 0,
        height=int(video_stream.get("height", 0)) if video_stream else 0,
        has_audio=any(stream.get("codec_type") == "audio" for stream in streams),
    )


def probe_all(videos: dict[int, Path], audios: list[Path]) -> tuple[dict[int, MediaInfo], list[MediaInfo]]:
    jobs = [("video", number, path, True) for number, path in videos.items()]
    jobs.extend(("audio", index, path, False) for index, path in enumerate(audios))
    results: dict[tuple[str, int], MediaInfo] = {}
    with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as executor:
        future_jobs = {
            executor.submit(probe_one, path, require_video): (kind, index)
            for kind, index, path, require_video in jobs
        }
        for future in as_completed(future_jobs):
            key = future_jobs[future]
            try:
                results[key] = future.result()
            except GeneratorError:
                raise
            except Exception as exc:
                raise GeneratorError(f"Lỗi khi đọc media {key[0]} {key[1]}") from exc
    return (
        {number: results[("video", number)] for number in videos},
        [results[("audio", index)] for index in range(len(audios))],
    )


def with_media_path(media: MediaInfo, path: Path) -> MediaInfo:
    return MediaInfo(
        path=path,
        duration_seconds=media.duration_seconds,
        width=media.width,
        height=media.height,
        has_audio=media.has_audio,
    )


def prepare_bundled_media(
    output: Path,
    videos: dict[int, MediaInfo],
    audios: list[MediaInfo],
) -> tuple[dict[int, MediaInfo], list[MediaInfo], list[MediaCopy]]:
    """Point draft materials at self-contained media files inside the draft."""
    bundled_videos: dict[int, MediaInfo] = {}
    bundled_audios: list[MediaInfo] = []
    copies: list[MediaCopy] = []

    for number, media in sorted(videos.items()):
        destination = output / "media" / "video" / f"{number:02d}{media.path.suffix.lower()}"
        bundled_videos[number] = with_media_path(media, destination)
        copies.append(MediaCopy(media.path, destination))

    for index, media in enumerate(audios, 1):
        destination = output / "media" / "audio" / f"{index:03d}{media.path.suffix.lower()}"
        bundled_audios.append(with_media_path(media, destination))
        copies.append(MediaCopy(media.path, destination))

    return bundled_videos, bundled_audios, copies


def frame_us(frame: int, fps: int) -> int:
    return round(frame * MICROSECONDS / fps)


def seconds_to_frame(seconds: float, fps: int) -> int:
    return round(seconds * fps)


def place_tokens(
    tokens: list[TimelineToken],
    videos: dict[int, MediaInfo],
    audios: list[MediaInfo],
    fps: int,
) -> list[PlacedToken]:
    placed: list[PlacedToken] = []
    cursor = 0
    for token in tokens:
        if token.kind == "audio":
            if token.audio_index is None or token.audio_index > len(audios):
                raise GeneratorError(f"Thiếu audio cho fragment tại {token.location}")
            media = audios[token.audio_index - 1]
            duration = frame_us(max(1, seconds_to_frame(media.duration_seconds, fps)), fps)
        else:
            if token.source_number is None or token.source_number not in videos:
                raise GeneratorError(f"Thiếu video nguồn cho fragment tại {token.location}")
            media = videos[token.source_number]
            if token.source_out_seconds > media.duration_seconds + 0.05:
                raise GeneratorError(
                    f"TH{token.source_number} kết thúc ở {token.source_out_seconds:.2f}s "
                    f"nhưng video chỉ dài {media.duration_seconds:.2f}s ({token.location})"
                )
            start_frame = seconds_to_frame(token.source_in_seconds, fps)
            end_frame = seconds_to_frame(token.source_out_seconds, fps)
            duration = frame_us(max(1, end_frame - start_frame), fps)
        placed.append(PlacedToken(token, cursor, duration, media))
        cursor += duration
    return placed


def find_video_gaps(placed: list[PlacedToken]) -> list[tuple[int, int]]:
    """Return uncovered ranges on the main video track."""
    if not placed:
        return []
    timeline_duration = placed[-1].timeline_start_us + placed[-1].duration_us
    intervals = sorted(
        (
            item.timeline_start_us,
            item.timeline_start_us + item.duration_us,
        )
        for item in placed
        if item.token.kind == "video"
    )
    gaps: list[tuple[int, int]] = []
    cursor = 0
    for start, end in intervals:
        if start > cursor:
            gaps.append((cursor, start - cursor))
        cursor = max(cursor, end)
    if cursor < timeline_duration:
        gaps.append((cursor, timeline_duration - cursor))
    return gaps


MATERIAL_KEYS = (
    "ai_translates",
    "audio_balances",
    "audio_effects",
    "audio_fades",
    "audio_track_indexes",
    "audios",
    "beats",
    "canvases",
    "chromas",
    "color_curves",
    "common_mask",
    "digital_human_model_dressing",
    "digital_humans",
    "drafts",
    "effects",
    "flowers",
    "green_screens",
    "handwrites",
    "hsl",
    "images",
    "log_color_wheels",
    "loudnesses",
    "manual_beautys",
    "manual_deformations",
    "material_animations",
    "material_colors",
    "multi_language_refs",
    "placeholder_infos",
    "placeholders",
    "plugin_effects",
    "primary_color_wheels",
    "realtime_denoises",
    "shapes",
    "smart_crops",
    "smart_relights",
    "sound_channel_mappings",
    "speeds",
    "stickers",
    "tail_leaders",
    "text_templates",
    "texts",
    "time_marks",
    "transitions",
    "video_effects",
    "video_trackings",
    "videos",
    "vocal_beautifys",
    "vocal_separations",
)


def empty_materials(existing: Any = None) -> dict[str, list[dict[str, Any]]]:
    keys = list(MATERIAL_KEYS)
    if isinstance(existing, dict):
        keys.extend(key for key in existing if key not in keys)
    return {key: [] for key in keys}


def base_draft(name: str, project_id: str, fps: int, platform: str, app_version: str) -> dict[str, Any]:
    now = int(time.time())
    platform_info = {
        "app_id": 359289,
        "app_source": "cc",
        "app_version": app_version,
        "os": platform,
    }
    return {
        "canvas_config": {"background": None, "height": 1080, "ratio": "16:9", "width": 1920},
        "color_space": 0,
        "config": {
            "adjust_max_index": 1,
            "attachment_info": [],
            "combination_max_index": 1,
            "export_range": None,
            "extract_audio_last_index": 1,
            "lyrics_recognition_id": "",
            "lyrics_sync": True,
            "lyrics_taskinfo": [],
            "maintrack_adsorb": True,
            "material_save_mode": 0,
            "multi_language_current": "none",
            "multi_language_list": [],
            "multi_language_main": "none",
            "multi_language_mode": "none",
            "original_sound_last_index": 1,
            "record_audio_last_index": 1,
            "sticker_max_index": 1,
            "subtitle_keywords_config": None,
            "subtitle_recognition_id": "",
            "subtitle_sync": True,
            "subtitle_taskinfo": [],
            "system_font_list": [],
            "use_float_render": False,
            "video_mute": False,
            "zoom_info_params": None,
        },
        "cover": None,
        "create_time": now,
        "duration": 0,
        "extra_info": None,
        "fps": float(fps),
        "free_render_index_mode_on": False,
        "group_container": None,
        "id": project_id,
        "is_drop_frame_timecode": False,
        "keyframe_graph_list": [],
        "keyframes": {key: [] for key in ("adjusts", "audios", "effects", "filters", "handwrites", "stickers", "texts", "videos")},
        "last_modified_platform": copy.deepcopy(platform_info),
        "lyrics_effects": [],
        "materials": empty_materials(),
        "mutable_config": None,
        "name": name,
        "path": "",
        "platform": platform_info,
        "relationships": [],
        "render_index_track_mode_on": False,
        "retouch_cover": None,
        "source": "default",
        "static_cover_image_path": "",
        "time_marks": None,
        "tracks": [],
        "update_time": now,
    }


def load_template(path: Path) -> dict[str, Any]:
    candidate = path.expanduser().resolve()
    if candidate.is_dir():
        names = ("draft_info.json", "draft_content.json")
        match = next((candidate / name for name in names if (candidate / name).is_file()), None)
        if match is None:
            raise GeneratorError(f"Template không có draft_info.json/draft_content.json: {candidate}")
        candidate = match
    existing_file(candidate, "Template draft")
    try:
        data = json.loads(candidate.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GeneratorError(f"Template draft không phải JSON hợp lệ: {candidate}") from exc
    if not isinstance(data, dict):
        raise GeneratorError("Template draft phải là JSON object")
    return data


def prepare_draft(
    *,
    name: str,
    project_id: str,
    fps: int,
    platform: str,
    app_version: str | None,
    template: Path | None,
) -> dict[str, Any]:
    if template is None:
        return base_draft(name, project_id, fps, platform, app_version or "9.1.0")
    draft = load_template(template)
    draft["id"] = project_id
    draft["name"] = name
    draft["duration"] = 0
    draft["fps"] = float(fps)
    draft["canvas_config"] = {"background": None, "height": 1080, "ratio": "16:9", "width": 1920}
    draft["tracks"] = []
    draft["materials"] = empty_materials(draft.get("materials"))
    draft["keyframes"] = {key: [] for key in ("adjusts", "audios", "effects", "filters", "handwrites", "stickers", "texts", "videos")}
    for key in ("platform", "last_modified_platform"):
        value = draft.get(key)
        if not isinstance(value, dict):
            value = {}
            draft[key] = value
        value.update({"app_source": "cc", "os": platform})
        if app_version:
            value["app_version"] = app_version
        elif not value.get("app_version"):
            value["app_version"] = "9.1.0"
    draft["update_time"] = int(time.time())
    return draft


def new_track(kind: str, name: str) -> dict[str, Any]:
    return {
        "id": uid(),
        "type": kind,
        "name": name,
        "attribute": 0,
        "segments": [],
        "is_default_name": False,
        "flag": 0,
    }


def companion_materials(draft: dict[str, Any], video: bool) -> list[str]:
    speed = {"id": uid(), "type": "speed", "speed": 1, "mode": 0, "curve_speed": None}
    placeholder = {
        "id": uid(),
        "type": "placeholder_info",
        "error_path": "",
        "error_text": "",
        "meta_type": "none",
        "res_path": "",
        "res_text": "",
    }
    channel = {"id": uid(), "type": "none", "audio_channel_mapping": 0, "is_config_open": False}
    vocal = {
        "id": uid(),
        "type": "vocal_separation",
        "choice": 0,
        "enter_from": "",
        "final_algorithm": "",
        "production_path": "",
        "removed_sounds": [],
        "time_range": None,
    }
    entries = [
        ("speeds", speed),
        ("placeholder_infos", placeholder),
        ("sound_channel_mappings", channel),
        ("vocal_separations", vocal),
    ]
    if video:
        entries.extend(
            [
                (
                    "canvases",
                    {
                        "id": uid(),
                        "type": "canvas_color",
                        "album_image": "",
                        "blur": 0,
                        "color": "",
                        "image": "",
                        "image_id": "",
                        "image_name": "",
                        "source_platform": 0,
                        "team_id": "",
                    },
                ),
                (
                    "material_colors",
                    {
                        "id": uid(),
                        "type": "material_color",
                        "gradient_angle": 90,
                        "gradient_colors": [],
                        "gradient_percents": [],
                        "height": 0,
                        "is_color_clip": False,
                        "is_gradient": False,
                        "solid_color": "",
                        "width": 0,
                    },
                ),
            ]
        )
    for key, material in entries:
        draft["materials"][key].append(material)
    return [material["id"] for _, material in entries]


def base_segment(
    *,
    material_id: str,
    track_id: str,
    target_start: int,
    duration: int,
    source_start: int,
    companion_ids: list[str],
    video: bool,
) -> dict[str, Any]:
    return {
        "id": uid(),
        "material_id": material_id,
        "raw_segment_id": track_id,
        "target_timerange": {"start": target_start, "duration": duration},
        "source_timerange": {"start": source_start, "duration": duration},
        "speed": 1,
        "volume": 1,
        "visible": True,
        "reverse": False,
        "clip": {
            "alpha": 1,
            "rotation": 0,
            "scale": {"x": 1, "y": 1},
            "transform": {"x": 0, "y": 0},
            "flip": {"horizontal": False, "vertical": False},
        }
        if video
        else None,
        "render_index": 14000 if video else 11000,
        "track_render_index": 0,
        "track_attribute": 0,
        "extra_material_refs": companion_ids,
        "common_keyframes": [],
        "keyframe_refs": [],
    }


def media_identity(media: MediaInfo) -> MediaIdentity:
    """Return stable, non-empty source identifiers in CapCut's native formats.

    CapCut uses ``id`` for an individual timeline material and ``unique_id`` /
    ``local_material_id`` to recognise the backing local file.  Empty source
    identifiers make recent CapCut versions keep the segment but mark its media
    as unlinked.  Include stat information so replacing a file at the same path
    produces a new identity without hashing multi-gigabyte video files.
    """
    if media.path.is_file():
        stat = media.path.stat()
        fingerprint = f"{media.path}\0{stat.st_size}\0{stat.st_mtime_ns}"
    else:
        # A bundled path is intentionally computed before its file is staged.
        fingerprint = (
            f"{media.path}\0{media.duration_seconds:.9f}\0"
            f"{media.width}x{media.height}\0{int(media.has_audio)}"
        )
    source_uuid = uuid.uuid5(uuid.NAMESPACE_URL, fingerprint)
    local_uuid = uuid.uuid5(uuid.NAMESPACE_OID, fingerprint)
    return MediaIdentity(unique_id=source_uuid.hex, local_material_id=str(local_uuid))


def video_material(
    material_id: str, media: MediaInfo, identity: MediaIdentity
) -> dict[str, Any]:
    return {
        "id": material_id,
        "unique_id": identity.unique_id,
        "path": str(media.path),
        "material_name": media.path.name,
        "material_id": "",
        "type": "video",
        "duration": round(media.duration_seconds * MICROSECONDS),
        "width": media.width,
        "height": media.height,
        "category_id": "",
        "category_name": "local",
        "check_flag": 62978047,
        "crop": {
            "lower_left_x": 0,
            "lower_left_y": 1,
            "lower_right_x": 1,
            "lower_right_y": 1,
            "upper_left_x": 0,
            "upper_left_y": 0,
            "upper_right_x": 1,
            "upper_right_y": 0,
        },
        "has_audio": media.has_audio,
        "extra_type_option": 0,
        "formula_id": "",
        "freeze": None,
        "intensifies_audio_path": "",
        "intensifies_path": "",
        "is_ai_generate_content": False,
        "is_copyright": False,
        "is_text_edit_overdub": False,
        "is_unified_beauty_mode": False,
        "local_id": "",
        "local_material_id": identity.local_material_id,
        "material_url": "",
        "media_path": "",
        "object_locked": None,
        "origin_material_id": "",
        "request_id": "",
        "reverse_path": "",
        "source_platform": 0,
        "stable": {"matrix_path": "", "stable_level": 0, "time_range": {"duration": 0, "start": 0}},
        "team_id": "",
        "video_algorithm": {
            "algorithms": [],
            "deflicker": None,
            "motion_blur_config": None,
            "noise_reduction": None,
            "path": "",
            "quality_enhance": None,
            "time_range": None,
        },
    }


def audio_material(
    material_id: str, media: MediaInfo, identity: MediaIdentity
) -> dict[str, Any]:
    return {
        "id": material_id,
        "unique_id": identity.unique_id,
        "path": str(media.path),
        "name": media.path.name,
        "duration": round(media.duration_seconds * MICROSECONDS),
        "type": "extract_music",
        "category_id": "",
        "category_name": "local",
        "check_flag": 1,
        "local_material_id": "",
        "music_id": "",
        "request_id": "",
        "source_platform": 0,
        "team_id": "",
        "text_id": "",
        "tone_category_id": "",
        "tone_category_name": "",
        "tone_effect_id": "",
        "tone_effect_name": "",
        "tone_platform": "",
        "tone_second_category_id": "",
        "tone_second_category_name": "",
        "tone_speaker": "",
        "tone_type": "",
        "wave_points": [],
    }


def populate_draft(
    draft: dict[str, Any],
    placed: list[PlacedToken],
    videos: dict[int, MediaInfo],
    audios: list[MediaInfo],
    fps: int,
    blank_media: MediaInfo | None = None,
) -> None:
    video_track = new_track("video", "Source video")
    audio_track = new_track("audio", "Voice-over")
    draft["tracks"] = [video_track, audio_track]

    video_identities = {
        number: media_identity(media) for number, media in videos.items()
    }
    audio_identities = [media_identity(media) for media in audios]

    for item in placed:
        token = item.token
        is_video = token.kind == "video"
        if is_video:
            assert token.source_number is not None
            # Native CapCut drafts use a distinct material id for each placed
            # video clip, while clips from the same file share source ids.
            material_id = uid()
            draft["materials"]["videos"].append(
                video_material(
                    material_id,
                    item.media,
                    video_identities[token.source_number],
                )
            )
            source_start = frame_us(seconds_to_frame(token.source_in_seconds, fps), fps)
            track = video_track
        else:
            assert token.audio_index is not None
            material_id = uid()
            draft["materials"]["audios"].append(
                audio_material(
                    material_id,
                    item.media,
                    audio_identities[token.audio_index - 1],
                )
            )
            source_start = 0
            track = audio_track
        companions = companion_materials(draft, is_video)
        track["segments"].append(
            base_segment(
                material_id=material_id,
                track_id=track["id"],
                target_start=item.timeline_start_us,
                duration=item.duration_us,
                source_start=source_start,
                companion_ids=companions,
                video=is_video,
            )
        )
    draft["duration"] = placed[-1].timeline_start_us + placed[-1].duration_us

    if blank_media is not None:
        blank_identity = media_identity(blank_media)
        video_segments = video_track["segments"]
        for start, duration in find_video_gaps(placed):
            material_id = uid()
            material = video_material(material_id, blank_media, blank_identity)
            material["material_name"] = "Blank frame"
            material["has_audio"] = False
            draft["materials"]["videos"].append(material)
            video_segments.append(
                base_segment(
                    material_id=material_id,
                    track_id=video_track["id"],
                    target_start=start,
                    duration=duration,
                    source_start=0,
                    companion_ids=companion_materials(draft, True),
                    video=True,
                )
            )
        video_segments.sort(key=lambda value: value["target_timerange"]["start"])


def meta_material(media: MediaInfo, kind: str) -> dict[str, Any]:
    return {
        "ai_group_type": "",
        "create_time": -1,
        "duration": round(media.duration_seconds * MICROSECONDS),
        "enter_from": 0,
        "extra_info": media.path.name,
        "file_Path": str(media.path),
        "height": media.height if kind == "video" else 0,
        "id": uid(),
        "import_time": -1,
        "import_time_ms": -1,
        "item_source": 1,
        "material_color_tag": "",
        "md5": "",
        "metetype": kind,
        "roughcut_time_range": {"duration": -1, "start": -1},
        "sub_time_range": {"duration": -1, "start": -1},
        "type": 0,
        "width": media.width if kind == "video" else 0,
    }


def build_meta(
    *,
    draft: dict[str, Any],
    output: Path,
    canonical_file: str,
    videos: dict[int, MediaInfo],
    audios: list[MediaInfo],
) -> dict[str, Any]:
    now_us = int(time.time() * MICROSECONDS)
    imported = [meta_material(media, "video") for _, media in sorted(videos.items())]
    imported.extend(meta_material(media, "music") for media in audios)
    return {
        "cloud_draft_cover": False,
        "cloud_draft_sync": False,
        "cloud_package_completed_time": "",
        "draft_cloud_last_action_download": False,
        "draft_cover": "",
        "draft_fold_path": str(output),
        "draft_id": draft["id"],
        "draft_is_ai_shorts": False,
        "draft_is_invisible": False,
        "draft_json_file": str(output / canonical_file),
        "draft_materials": [
            {"type": 0, "value": imported},
            *({"type": number, "value": []} for number in (1, 2, 3, 6, 7, 8)),
        ],
        "draft_materials_copied_info": [],
        "draft_name": draft["name"],
        "draft_new_version": "",
        "draft_root_path": str(output.parent),
        "draft_segment_extra_info": [],
        "draft_timeline_materials_size": 0,
        "draft_timeline_materials_size_": 0,
        "draft_type": "",
        "tm_draft_create": now_us,
        "tm_draft_modified": now_us,
        "tm_draft_removed": 0,
        "tm_duration": draft["duration"],
    }


def build_manifest(
    *,
    content: Path,
    placed: list[PlacedToken],
    video_dir: Path,
    audio_dir: Path,
    fps: int,
) -> dict[str, Any]:
    fragments = []
    for index, item in enumerate(placed, 1):
        token = item.token
        record: dict[str, Any] = {
            "index": index,
            "kind": token.kind,
            "section": token.section,
            "location": token.location,
            "timeline_start_seconds": item.timeline_start_us / MICROSECONDS,
            "timeline_end_seconds": (item.timeline_start_us + item.duration_us) / MICROSECONDS,
            "duration_seconds": item.duration_us / MICROSECONDS,
            "source_file": str(item.media.path),
        }
        if token.kind == "video":
            record.update(
                {
                    "source_number": token.source_number,
                    "source_in_seconds": token.source_in_seconds,
                    "source_out_seconds": token.source_out_seconds,
                }
            )
        else:
            record.update({"audio_index": token.audio_index, "text": token.text})
        fragments.append(record)
    return {
        "generator": "generate_capcut_project.py",
        "content": str(content),
        "video_dir": str(video_dir),
        "audio_dir": str(audio_dir),
        "fps": fps,
        "duration_seconds": sum(item.duration_us for item in placed) / MICROSECONDS,
        "video_fragments": sum(item.token.kind == "video" for item in placed),
        "audio_fragments": sum(item.token.kind == "audio" for item in placed),
        "fragments": fragments,
    }


def validate_draft(
    draft: dict[str, Any],
    placed: list[PlacedToken],
    staged_media_paths: set[Path] | None = None,
    expected_segment_count: int | None = None,
) -> None:
    staged_media_paths = staged_media_paths or set()
    expected_segment_count = expected_segment_count or len(placed)
    materials = draft.get("materials", {})
    material_ids: set[str] = set()
    for values in materials.values():
        if not isinstance(values, list):
            raise GeneratorError("materials trong draft không hợp lệ")
        for material in values:
            material_id = material.get("id") if isinstance(material, dict) else None
            if material_id:
                if material_id in material_ids:
                    raise GeneratorError(f"Material ID bị trùng: {material_id}")
                material_ids.add(material_id)

    primary_materials = [
        material
        for key in ("videos", "audios")
        for material in materials.get(key, [])
    ]
    for material in primary_materials:
        media_path = Path(material.get("path", ""))
        if not media_path.is_absolute() or (
            not media_path.is_file() and media_path not in staged_media_paths
        ):
            raise GeneratorError(f"Media path không link được: {media_path}")
        if not material.get("unique_id"):
            raise GeneratorError(f"Media thiếu unique_id: {media_path}")
        if material.get("type") == "video" and not material.get("local_material_id"):
            raise GeneratorError(f"Video thiếu local_material_id: {media_path}")

    segment_ids: set[str] = set()
    segment_count = 0
    max_end = 0
    for track in draft.get("tracks", []):
        previous_start = -1
        for segment in track.get("segments", []):
            segment_count += 1
            if segment["id"] in segment_ids:
                raise GeneratorError(f"Segment ID bị trùng: {segment['id']}")
            segment_ids.add(segment["id"])
            if segment["material_id"] not in material_ids:
                raise GeneratorError("Segment trỏ tới primary material không tồn tại")
            if any(ref not in material_ids for ref in segment.get("extra_material_refs", [])):
                raise GeneratorError("Segment trỏ tới companion material không tồn tại")
            start = segment["target_timerange"]["start"]
            duration = segment["target_timerange"]["duration"]
            if start < previous_start or duration <= 0:
                raise GeneratorError(f"Track {track['name']} có timerange không hợp lệ")
            previous_start = start
            max_end = max(max_end, start + duration)
            if track["type"] == "audio" and segment.get("clip") is not None:
                raise GeneratorError("Audio segment phải có clip=null")
            if track["type"] == "video" and not isinstance(segment.get("clip"), dict):
                raise GeneratorError("Video segment phải có clip object")
    if segment_count != expected_segment_count:
        raise GeneratorError("Số segment trong draft khác số fragment đã đặt")
    if len(primary_materials) != segment_count:
        raise GeneratorError("Mỗi segment phải có một CapCut material riêng để link media")
    if max_end != draft.get("duration"):
        raise GeneratorError("Duration của draft không khớp segment cuối")
    if any(
        not item.media.path.is_file() and item.media.path not in staged_media_paths
        for item in placed
    ):
        raise GeneratorError("Có media path không tồn tại sau khi tạo draft")


def atomic_write_json(path: Path, data: Any, pretty: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                data,
                handle,
                ensure_ascii=False,
                indent=2 if pretty else None,
                separators=None if pretty else (",", ":"),
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def draft_store_key(index: dict[str, Any]) -> str:
    for key, value in index.items():
        if isinstance(value, list) and any(
            isinstance(entry, dict) and ("draft_fold_path" in entry or "draft_id" in entry)
            for entry in value
        ):
            return key
    for key, value in index.items():
        if isinstance(value, list) and re.search(r"all_draft_store|draft_store", key, re.IGNORECASE):
            return key
    return "all_draft_store"


def register_in_store(install_dir: Path, meta: dict[str, Any]) -> None:
    index_path = install_dir / "root_meta_info.json"
    if index_path.exists():
        try:
            raw = index_path.read_text(encoding="utf-8-sig")
            index = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise GeneratorError(
                f"Không đọc được {index_path}; draft đã tạo nhưng chưa đăng ký"
            ) from exc
        if not isinstance(index, dict):
            raise GeneratorError(f"{index_path} không phải JSON object")
    else:
        raw = ""
        index = {}

    key = draft_store_key(index)
    entries = index.get(key, [])
    if not isinstance(entries, list):
        entries = []
    entry = {
        key: copy.deepcopy(value)
        for key, value in meta.items()
        if key
        in {
            "draft_cover",
            "draft_fold_path",
            "draft_id",
            "draft_is_ai_shorts",
            "draft_is_invisible",
            "draft_json_file",
            "draft_name",
            "draft_new_version",
            "draft_root_path",
            "draft_timeline_materials_size",
            "tm_draft_create",
            "tm_draft_modified",
            "tm_draft_removed",
            "tm_duration",
        }
    }
    entries = [item for item in entries if not isinstance(item, dict) or item.get("draft_fold_path") != meta["draft_fold_path"]]
    entries.append(entry)
    index[key] = entries
    if raw:
        backup = index_path.with_name(f"root_meta_info.json.bak.{int(time.time())}")
        shutil.copy2(index_path, backup)
    atomic_write_json(index_path, index)


def generate_black_video(path: Path, duration_us: int, width: int, height: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    duration_seconds = max(duration_us / MICROSECONDS, 0.1)
    try:
        completed = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"color=c=black:s={width}x{height}:r=1",
                "-t",
                f"{duration_seconds:.6f}",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-crf",
                "35",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                "-y",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
    except FileNotFoundError as exc:
        raise GeneratorError("Không tìm thấy ffmpeg để tạo blank frame") from exc
    except (subprocess.SubprocessError, OSError) as exc:
        details = completed.stderr.strip() if "completed" in locals() else ""
        raise GeneratorError(
            "Không tạo được blank frame" + (f": {details}" if details else "")
        ) from exc


def write_draft_directory(
    *,
    output: Path,
    canonical_file: str,
    draft: dict[str, Any],
    meta: dict[str, Any],
    manifest: dict[str, Any],
    media_copies: list[MediaCopy],
    generated_videos: list[GeneratedVideo],
) -> None:
    if output.exists():
        raise GeneratorError(f"Output đã tồn tại, hãy chọn thư mục mới: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    try:
        for generated in generated_videos:
            try:
                relative = generated.destination.relative_to(output)
            except ValueError as exc:
                raise GeneratorError(
                    f"Generated media nằm ngoài draft: {generated.destination}"
                ) from exc
            generate_black_video(
                temp_dir / relative,
                generated.duration_us,
                generated.width,
                generated.height,
            )
        for media_copy in media_copies:
            try:
                relative = media_copy.destination.relative_to(output)
            except ValueError as exc:
                raise GeneratorError(
                    f"Bundle media nằm ngoài draft: {media_copy.destination}"
                ) from exc
            bundled_path = temp_dir / relative
            bundled_path.parent.mkdir(parents=True, exist_ok=True)
            copied = False
            if sys.platform == "darwin":
                # APFS clone-on-write avoids duplicating gigabytes of source
                # data while producing a separate file CapCut can access.
                completed = subprocess.run(
                    ["/bin/cp", "-c", str(media_copy.source), str(bundled_path)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                copied = completed.returncode == 0
                if not copied and bundled_path.exists():
                    bundled_path.unlink()
            if not copied:
                shutil.copy2(media_copy.source, bundled_path)
        atomic_write_json(temp_dir / canonical_file, draft)
        atomic_write_json(temp_dir / "draft_meta_info.json", meta)
        atomic_write_json(temp_dir / "timeline_manifest.json", manifest, pretty=True)
        os.replace(temp_dir, output)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def generate_capcut_project(
    *,
    project_dir: Path,
    content: Path,
    output: Path,
    name: str,
    video_dir: Path | None = None,
    audio_dir: Path | None = None,
    fps: int = DEFAULT_FPS,
    platform: str = "mac",
    app_version: str | None = None,
    template: Path | None = None,
    write_output: bool = True,
    install_dir: Path | None = None,
    bundle_media: bool | None = None,
    fill_video_gaps: bool = True,
) -> GenerationSummary:
    project_dir = existing_directory(project_dir.expanduser().resolve(), "Project dir")
    content = existing_file(content.expanduser().resolve(), "Content DOCX")
    output = output.expanduser().resolve()
    if bundle_media is None:
        bundle_media = install_dir is not None
    if fps <= 0:
        raise GeneratorError("FPS phải lớn hơn 0")

    tokens = parse_docx_timeline(content)
    expected_numbers = {
        token.source_number
        for token in tokens
        if token.kind == "video" and token.source_number is not None
    }
    expected_audio_count = sum(token.kind == "audio" for token in tokens)
    found_video_dir, video_paths = discover_video_media(project_dir, video_dir, expected_numbers)
    found_audio_dir, audio_paths = discover_audio_media(project_dir, audio_dir, expected_audio_count)
    video_infos, audio_infos = probe_all(video_paths, audio_paths)
    media_copies: list[MediaCopy] = []
    if bundle_media:
        video_infos, audio_infos, media_copies = prepare_bundled_media(
            output, video_infos, audio_infos
        )
    placed = place_tokens(tokens, video_infos, audio_infos, fps)
    gaps = find_video_gaps(placed) if fill_video_gaps else []
    generated_videos: list[GeneratedVideo] = []
    blank_media: MediaInfo | None = None
    if gaps:
        # One reusable black source is enough; each gap gets its own material
        # and segment so CapCut's magnetic main track cannot close the space.
        blank_duration_us = max(duration for _, duration in gaps) + frame_us(1, fps)
        blank_path = output / "media" / "generated" / "blank.mp4"
        blank_media = MediaInfo(
            path=blank_path,
            duration_seconds=blank_duration_us / MICROSECONDS,
            width=1920,
            height=1080,
            has_audio=False,
        )
        generated_videos.append(GeneratedVideo(blank_path, blank_duration_us))

    project_id = uid()
    draft = prepare_draft(
        name=name,
        project_id=project_id,
        fps=fps,
        platform=platform,
        app_version=app_version,
        template=template,
    )
    populate_draft(draft, placed, video_infos, audio_infos, fps, blank_media)
    staged_media_paths = {media_copy.destination for media_copy in media_copies}
    staged_media_paths.update(video.destination for video in generated_videos)
    validate_draft(
        draft,
        placed,
        staged_media_paths,
        expected_segment_count=len(placed) + len(gaps),
    )
    canonical_file = "draft_info.json" if platform == "mac" else "draft_content.json"
    meta = build_meta(
        draft=draft,
        output=output,
        canonical_file=canonical_file,
        videos=video_infos,
        audios=audio_infos,
    )
    manifest = build_manifest(
        content=content,
        placed=placed,
        video_dir=found_video_dir,
        audio_dir=found_audio_dir,
        fps=fps,
    )

    if write_output:
        write_draft_directory(
            output=output,
            canonical_file=canonical_file,
            draft=draft,
            meta=meta,
            manifest=manifest,
            media_copies=media_copies,
            generated_videos=generated_videos,
        )
        if install_dir is not None:
            register_in_store(install_dir, meta)

    return GenerationSummary(
        output=output,
        canonical_file=canonical_file,
        video_dir=found_video_dir,
        audio_dir=found_audio_dir,
        video_count=len(video_infos),
        audio_count=len(audio_infos),
        video_cut_count=sum(token.kind == "video" for token in tokens),
        duration_us=draft["duration"],
        fps=fps,
        installed=install_dir is not None and write_output,
        bundled=bundle_media,
        blank_count=len(gaps),
    )


def safe_draft_name(value: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|]", "-", one_line(value)).strip(". ")
    if not cleaned:
        raise GeneratorError("Tên draft không hợp lệ")
    return cleaned


def resolve_arguments(args: argparse.Namespace) -> argparse.Namespace:
    project_dir = (
        existing_directory(Path(args.project_dir).expanduser().resolve(), "Project dir")
        if args.project_dir
        else resolve_default_project()
    )
    content = (
        existing_file(Path(args.content).expanduser().resolve(), "Content DOCX")
        if args.content
        else existing_file(project_dir / DEFAULT_CONTENT_NAME, "Content DOCX")
    )
    name = safe_draft_name(args.name or f"{project_dir.name} CapCut")
    install_dir = (
        existing_directory(Path(args.install_dir).expanduser().resolve(), "CapCut drafts dir")
        if args.install_dir
        else None
    )
    if args.output and install_dir is not None:
        raise GeneratorError("Chỉ dùng một trong hai: --output hoặc --install-dir")
    output = (
        Path(args.output).expanduser().resolve()
        if args.output
        else install_dir / name
        if install_dir is not None
        else project_dir / "capcut_output" / name
    )
    args.project_dir = project_dir
    args.content = content
    args.name = name
    args.install_dir = install_dir
    args.output = output.resolve()
    if args.bundle_media is None:
        args.bundle_media = install_dir is not None
    return args


def build_parser() -> argparse.ArgumentParser:
    default_platform = "mac" if sys.platform == "darwin" else "windows"
    parser = argparse.ArgumentParser(
        description="Tạo CapCut Desktop draft từ content.docx, video source và voice-over"
    )
    parser.add_argument("--project-dir", help="Thư mục project; mặc định tự tìm Bài Karen 0509")
    parser.add_argument("--content", help="File content.docx")
    parser.add_argument("--video-dir", help="Thư mục video nguồn (videos/video/vides)")
    parser.add_argument("--audio-dir", help="Thư mục voice-over (audio/au), có thể chứa thư mục con")
    parser.add_argument("--output", help="Thư mục draft đầu ra; phải chưa tồn tại")
    parser.add_argument("--name", help="Tên project hiển thị trong CapCut")
    parser.add_argument("--fps", type=int, default=DEFAULT_FPS, help=f"FPS timeline (mặc định {DEFAULT_FPS})")
    parser.add_argument("--platform", choices=("mac", "windows"), default=default_platform)
    parser.add_argument(
        "--app-version",
        help="Version ghi trong schema; mặc định 9.1.0, hoặc giữ version của --template-draft",
    )
    parser.add_argument(
        "--template-draft",
        help="JSON hoặc thư mục draft trống do đúng bản CapCut hiện tại tạo (khuyến nghị nếu draft mặc định không mở)",
    )
    parser.add_argument(
        "--install-dir",
        help="Tạo và đăng ký trực tiếp trong com.lveditor.draft; hãy đóng CapCut trước",
    )
    parser.add_argument(
        "--bundle-media",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Đóng gói media vào draft để tránh lỗi quyền macOS; "
            "mặc định bật khi dùng --install-dir"
        ),
    )
    parser.add_argument(
        "--fill-video-gaps",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Chèn clip nền đen vào khoảng không có hình để CapCut không close gap",
    )
    parser.add_argument("--dry-run", action="store_true", help="Parse/probe/validate nhưng không ghi file")
    return parser


def main() -> int:
    parser = build_parser()
    try:
        args = resolve_arguments(parser.parse_args())
        summary = generate_capcut_project(
            project_dir=args.project_dir,
            content=args.content,
            output=args.output,
            name=args.name,
            video_dir=Path(args.video_dir) if args.video_dir else None,
            audio_dir=Path(args.audio_dir) if args.audio_dir else None,
            fps=args.fps,
            platform=args.platform,
            app_version=args.app_version,
            template=Path(args.template_draft) if args.template_draft else None,
            write_output=not args.dry_run,
            install_dir=args.install_dir,
            bundle_media=args.bundle_media,
            fill_video_gaps=args.fill_video_gaps,
        )
        print("=" * 72)
        print("CapCut Draft Generator")
        print("=" * 72)
        print(f"[CONTENT] {args.content}")
        print(f"[VIDEO] {summary.video_dir}: {summary.video_count} source / {summary.video_cut_count} cut")
        print(f"[AUDIO] {summary.audio_dir}: {summary.audio_count} voice-over")
        print(f"[BLANK] {summary.blank_count} khoảng trống được giữ bằng clip nền đen")
        print(f"[TIMELINE] {summary.duration_us / MICROSECONDS:.2f}s @ {summary.fps} fps")
        print("[VALIDATE] OK - media, timerange và material reference hợp lệ")
        if args.dry_run:
            print("[DRY RUN] Không ghi project")
        else:
            print(f"[OUTPUT] {summary.output}")
            print(f"[DRAFT] {summary.canonical_file}")
            print("[MANIFEST] timeline_manifest.json")
            if summary.bundled:
                print("[MEDIA] Đã đóng gói vào draft để CapCut luôn truy cập được")
            if summary.installed:
                print("[INSTALL] Đã đăng ký vào root_meta_info.json (có backup nếu index đã tồn tại)")
            else:
                print("[NOTE] Dùng --install-dir để project xuất hiện tự động trong CapCut Desktop")
        return 0
    except GeneratorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
