#!/usr/bin/env python3
"""Build a relinked Premiere XML for the ``Bài Karen 0509`` project.

``content.docx`` is the authority for the order of the 56 source-video cuts.
``Bài 0509.xml`` is used as a blueprint because it also records the project's
103 voice-over slots and the small source-time adjustments made by Premiere.

The script deliberately writes a new XML by default.  It never overwrites the
reference XML unless a different reference/output arrangement is supplied.
"""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from docx import Document

from generate_xml import (
    AUDIO_EXTENSIONS,
    VIDEO_EXTENSIONS,
    GeneratorError,
    existing_directory,
    existing_file,
    extract_timecodes,
    local_path_from_url,
    natural_sort_key,
    path_to_url,
    probe_media,
    sort_audios,
    write_xml_atomic,
)


DEFAULT_PROJECT_NAME = "Bài Karen 0509"
DEFAULT_CONTENT_NAME = "content.docx"
DEFAULT_XML_NAME = "Bài 0509.xml"
SCENARIO_RE = re.compile(r"vào\s+tình\s+huống\s*(\d+)", re.IGNORECASE)
VIDEO_NUMBER_RE = re.compile(r"vid(?:eo)?\s*(\d+)", re.IGNORECASE)
TH_NUMBER_RE = re.compile(r"TH\s*(\d+)", re.IGNORECASE)
REFERENCE_TIMECODE_TOLERANCE_SECONDS = 1.25


@dataclass(frozen=True)
class VideoCut:
    source_number: int
    source_in_seconds: float
    source_out_seconds: float
    location: str


@dataclass(frozen=True)
class Blueprint:
    root: ET.Element
    sequence: ET.Element
    video_clips: list[ET.Element]
    narration_clips: list[ET.Element]
    timeline_clips: list[ET.Element]
    fps: int


def nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def one_line(value: str) -> str:
    return " ".join(value.split())


def int_text(element: ET.Element, child: str, default: int = 0) -> int:
    value = element.findtext(child)
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


def set_child_text(element: ET.Element, child: str, value: object) -> None:
    target = element.find(child)
    if target is None:
        target = ET.SubElement(element, child)
    target.text = str(value)


def find_named_child(directory: Path, expected_name: str) -> Path | None:
    expected = nfc(expected_name)
    for path in directory.iterdir():
        if nfc(path.name) == expected:
            return path
    return None


def resolve_default_project() -> Path:
    repository = Path(__file__).resolve().parent.parent
    candidates = [Path.cwd(), repository]
    for base in candidates:
        if nfc(base.name) == nfc(DEFAULT_PROJECT_NAME) and base.is_dir():
            return base.resolve()
        if base.is_dir():
            match = find_named_child(base, DEFAULT_PROJECT_NAME)
            if match is not None and match.is_dir():
                return match.resolve()
    raise GeneratorError(
        f"Không tự tìm thấy project {DEFAULT_PROJECT_NAME!r}; hãy truyền --project-dir"
    )


def resolve_project_file(
    project_dir: Path, supplied: str | None, default_name: str, label: str
) -> Path:
    if supplied:
        return existing_file(Path(supplied).expanduser().resolve(), label)
    match = find_named_child(project_dir, default_name)
    if match is None or not match.is_file():
        raise GeneratorError(f"Không tìm thấy {default_name!r} trong {project_dir}")
    return match.resolve()


def parse_docx_video_cuts(docx_path: Path) -> list[VideoCut]:
    """Read timecodes without assuming that TH1-TH6 occupy table indexes 1-6."""
    try:
        document = Document(docx_path)
    except Exception as exc:
        raise GeneratorError(f"Không đọc được DOCX: {docx_path}") from exc
    if not document.tables:
        raise GeneratorError("DOCX không có bảng nội dung")

    cuts: list[VideoCut] = []
    current_scenario: int | None = None
    seen_teasers: set[tuple[int, str]] = set()

    for table_index, table in enumerate(document.tables):
        for row_index, row in enumerate(table.rows):
            cells = [one_line(cell.text) for cell in row.cells]
            row_type = cells[0] if cells else ""
            text = cells[1] if len(cells) > 1 else ""

            scenario_match = SCENARIO_RE.search(row_type)
            if scenario_match:
                current_scenario = int(scenario_match.group(1))

            segments = extract_timecodes(text)
            if not segments:
                continue

            if "đoạn kịch tính" in text.lower():
                video_match = VIDEO_NUMBER_RE.search(text)
                if video_match is None:
                    raise GeneratorError(
                        f"Thiếu số video tại bảng {table_index + 1}, dòng {row_index + 1}"
                    )
                source_number = int(video_match.group(1))
            elif "tình huống" in row_type.lower() or row_type.lower() in {
                "nội dung",
                "",
            }:
                if current_scenario is None:
                    raise GeneratorError(
                        f"Không xác định được tình huống tại bảng {table_index + 1}, "
                        f"dòng {row_index + 1}"
                    )
                source_number = current_scenario
            else:
                continue

            if not 1 <= source_number <= 6:
                raise GeneratorError(f"Số video ngoài TH1-TH6: {source_number}")

            if "tình huống" in row_type.lower():
                teaser_key = (source_number, one_line(text).casefold())
                if teaser_key in seen_teasers:
                    continue
                seen_teasers.add(teaser_key)

            location = f"bảng {table_index + 1}, dòng {row_index + 1}"
            for source_in, source_out in segments:
                cuts.append(VideoCut(source_number, source_in, source_out, location))

    if not cuts:
        raise GeneratorError("Không tìm thấy timecode video trong DOCX")
    return cuts


def source_number(name: str) -> int | None:
    stem = Path(name).stem.strip()
    if stem.isdigit():
        value = int(stem)
        return value if 1 <= value <= 6 else None
    match = TH_NUMBER_RE.search(stem)
    if match:
        value = int(match.group(1))
        return value if 1 <= value <= 6 else None
    return None


def clip_start(clip: ET.Element) -> int:
    return int_text(clip, "start", -1)


def clip_kind(clip: ET.Element) -> str:
    suffix = Path(clip.findtext("name") or "").suffix.lower()
    if suffix in AUDIO_EXTENSIONS:
        return "narration"
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    return "unknown"


def load_blueprint(xml_path: Path) -> Blueprint:
    try:
        root = ET.parse(xml_path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise GeneratorError(f"Không đọc được XML: {xml_path}") from exc

    sequence = root.find("./sequence")
    if sequence is None:
        raise GeneratorError("XML không có main sequence")
    fps = int_text(sequence, "./rate/timebase")
    if fps <= 0:
        raise GeneratorError("XML không có frame rate hợp lệ")

    video_tracks = sequence.findall("./media/video/track")
    audio_tracks = sequence.findall("./media/audio/track")
    if not video_tracks or not audio_tracks:
        raise GeneratorError("XML phải có ít nhất một video track và một audio track")

    video_clips = sorted(video_tracks[0].findall("clipitem"), key=clip_start)
    narration_track = max(
        audio_tracks,
        key=lambda track: sum(clip_kind(clip) == "narration" for clip in track.findall("clipitem")),
    )
    timeline_clips = sorted(narration_track.findall("clipitem"), key=clip_start)
    narration_clips = [clip for clip in timeline_clips if clip_kind(clip) == "narration"]
    unknown = [clip.findtext("name") or "" for clip in timeline_clips if clip_kind(clip) == "unknown"]
    if unknown:
        raise GeneratorError(f"A1 có clip không nhận dạng được: {unknown[0]!r}")

    return Blueprint(
        root=root,
        sequence=sequence,
        video_clips=video_clips,
        narration_clips=narration_clips,
        timeline_clips=timeline_clips,
        fps=fps,
    )


def files_directly_in(directory: Path, extensions: set[str]) -> list[Path]:
    return sorted(
        [
            path.resolve()
            for path in directory.iterdir()
            if path.is_file()
            and not path.name.startswith(".")
            and path.suffix.lower() in extensions
        ],
        key=lambda path: natural_sort_key(path.name),
    )


def video_map_for(directory: Path) -> dict[int, Path]:
    result: dict[int, Path] = {}
    for fallback_number, path in enumerate(files_directly_in(directory, VIDEO_EXTENSIONS), 1):
        number = source_number(path.name) or fallback_number
        if number in result:
            return {}
        result[number] = path
    return result


def discover_video_media(project_dir: Path, supplied: str | None) -> tuple[Path, dict[int, Path]]:
    if supplied:
        directory = existing_directory(Path(supplied).expanduser().resolve(), "Video dir")
        mapping = video_map_for(directory)
        if set(mapping) != set(range(1, 7)):
            raise GeneratorError(f"{directory} phải chứa đúng video 1-6/TH1-TH6")
        return directory, mapping

    preferred: list[Path] = []
    for name in ("videos", "video"):
        match = find_named_child(project_dir, name)
        if match is not None and match.is_dir():
            preferred.append(match)
    preferred.append(project_dir)

    other_directories = sorted(
        {
            path.parent
            for path in project_dir.rglob("*")
            if path.is_file()
            and path.suffix.lower() in VIDEO_EXTENSIONS
            and "Adobe Premiere Pro Auto-Save" not in path.parts
        },
        key=lambda path: (len(path.parts), natural_sort_key(str(path))),
    )
    for directory in preferred + [path for path in other_directories if path not in preferred]:
        mapping = video_map_for(directory)
        if set(mapping) == set(range(1, 7)):
            return directory.resolve(), mapping
    raise GeneratorError("Không tự tìm thấy một thư mục chứa đúng video 1-6/TH1-TH6")


def discover_audio_media(
    project_dir: Path, supplied: str | None, expected_count: int
) -> tuple[Path, list[Path]]:
    if supplied:
        directory = existing_directory(Path(supplied).expanduser().resolve(), "Audio dir")
        paths = sort_audios(files_directly_in(directory, AUDIO_EXTENSIONS))
        if len(paths) != expected_count:
            raise GeneratorError(
                f"Audio dir có {len(paths)} file; blueprint XML cần {expected_count}"
            )
        return directory, paths

    groups: dict[Path, list[Path]] = {}
    for path in project_dir.rglob("*"):
        if (
            path.is_file()
            and not path.name.startswith(".")
            and path.suffix.lower() in AUDIO_EXTENSIONS
            and "Adobe Premiere Pro Auto-Save" not in path.parts
        ):
            groups.setdefault(path.parent, []).append(path.resolve())
    exact = [
        (directory, sort_audios(paths))
        for directory, paths in groups.items()
        if len(paths) == expected_count
    ]
    if not exact:
        counts = ", ".join(
            f"{directory}: {len(paths)}"
            for directory, paths in sorted(groups.items(), key=lambda item: str(item[0]))
        )
        detail = counts or "không có audio"
        raise GeneratorError(
            f"Không tìm thấy audio dir có {expected_count} file ({detail}); hãy truyền --audio-dir"
        )
    exact.sort(
        key=lambda item: (
            len(item[0].relative_to(project_dir).parts),
            natural_sort_key(str(item[0])),
        )
    )
    return exact[0][0].resolve(), exact[0][1]


def full_file_definitions(root: ET.Element) -> dict[str, ET.Element]:
    definitions: dict[str, ET.Element] = {}
    for file_element in root.findall(".//file"):
        file_id = file_element.get("id")
        if file_id and len(file_element):
            definitions[file_id] = file_element
    return definitions


def clips_by_file_id(root: ET.Element) -> dict[str, list[ET.Element]]:
    result: dict[str, list[ET.Element]] = {}
    for clip in root.findall(".//clipitem"):
        file_element = clip.find("file")
        if file_element is not None and file_element.get("id"):
            result.setdefault(file_element.get("id", ""), []).append(clip)
    return result


def relink_blueprint(
    blueprint: Blueprint, videos: dict[int, Path], audios: list[Path]
) -> None:
    definitions = full_file_definitions(blueprint.root)
    references = clips_by_file_id(blueprint.root)

    for number, media_path in videos.items():
        matching_ids = {
            file_id
            for file_id, definition in definitions.items()
            if source_number(definition.findtext("name") or "") == number
        }
        if not matching_ids:
            raise GeneratorError(f"Blueprint XML thiếu file definition cho video {number}")
        for file_id in matching_ids:
            definition = definitions[file_id]
            set_child_text(definition, "name", media_path.name)
            set_child_text(definition, "pathurl", path_to_url(media_path))
            for clip in references.get(file_id, []):
                set_child_text(clip, "name", media_path.name)

    for clip, media_path in zip(blueprint.narration_clips, audios, strict=True):
        file_element = clip.find("file")
        file_id = file_element.get("id") if file_element is not None else None
        if not file_id or file_id not in definitions:
            raise GeneratorError(f"Clip voice-over {clip.get('id')!r} thiếu file definition")
        definition = definitions[file_id]
        set_child_text(definition, "name", media_path.name)
        set_child_text(definition, "pathurl", path_to_url(media_path))
        for related_clip in references.get(file_id, []):
            set_child_text(related_clip, "name", media_path.name)


def validate_blueprint(
    blueprint: Blueprint,
    cuts: list[VideoCut],
    videos: dict[int, Path],
    audios: list[Path],
) -> float:
    if len(blueprint.video_clips) != len(cuts):
        raise GeneratorError(
            f"DOCX có {len(cuts)} video cut nhưng blueprint XML có "
            f"{len(blueprint.video_clips)}"
        )
    if len(blueprint.narration_clips) != len(audios):
        raise GeneratorError(
            f"Blueprint XML có {len(blueprint.narration_clips)} voice-over slot nhưng "
            f"audio dir có {len(audios)} file"
        )

    blueprint_numbers = [source_number(clip.findtext("name") or "") for clip in blueprint.video_clips]
    docx_numbers = [cut.source_number for cut in cuts]
    if blueprint_numbers != docx_numbers:
        mismatch = next(
            index
            for index, (left, right) in enumerate(zip(blueprint_numbers, docx_numbers), 1)
            if left != right
        )
        raise GeneratorError(
            f"Thứ tự video khác nhau tại cut {mismatch}: XML=TH{blueprint_numbers[mismatch - 1]}, "
            f"DOCX=TH{docx_numbers[mismatch - 1]}"
        )

    max_timecode_drift = 0.0
    for clip, cut in zip(blueprint.video_clips, cuts, strict=True):
        xml_in = int_text(clip, "in") / blueprint.fps
        xml_out = int_text(clip, "out") / blueprint.fps
        max_timecode_drift = max(
            max_timecode_drift,
            abs(xml_in - cut.source_in_seconds),
            abs(xml_out - cut.source_out_seconds),
        )
    if max_timecode_drift > REFERENCE_TIMECODE_TOLERANCE_SECONDS:
        raise GeneratorError(
            f"Timecode trong XML lệch DOCX tới {max_timecode_drift:.3f}s, vượt ngưỡng "
            f"{REFERENCE_TIMECODE_TOLERANCE_SECONDS:.2f}s; cần cập nhật blueprint XML"
        )

    video_infos = {
        number: probe_media(path, blueprint.fps, require_video=True)
        for number, path in videos.items()
    }
    for cut in cuts:
        if cut.source_out_seconds > video_infos[cut.source_number].duration_seconds + 0.05:
            raise GeneratorError(
                f"Timecode TH{cut.source_number} kết thúc ở {cut.source_out_seconds:.2f}s "
                f"nhưng video dài {video_infos[cut.source_number].duration_seconds:.2f}s "
                f"({cut.location})"
            )

    previous_end = 0
    for clip in blueprint.timeline_clips:
        start = int_text(clip, "start", -1)
        end = int_text(clip, "end", -1)
        if start != previous_end or end <= start:
            raise GeneratorError(
                f"Timeline blueprint không liên tục tại clip {clip.get('id')!r}: "
                f"expected start={previous_end}, actual={start}, end={end}"
            )
        previous_end = end
    if int_text(blueprint.sequence, "duration", -1) != previous_end:
        raise GeneratorError("Sequence duration không bằng điểm kết thúc của A1")

    clip_ids = [clip.get("id") for clip in blueprint.root.findall(".//clipitem")]
    if len(clip_ids) != len(set(clip_ids)):
        raise GeneratorError("Blueprint XML có clipitem id bị trùng")
    valid_clip_ids = set(clip_ids)
    for link in blueprint.root.findall(".//link/linkclipref"):
        if link.text and link.text not in valid_clip_ids:
            raise GeneratorError(f"XML link trỏ tới clip không tồn tại: {link.text}")

    definitions = full_file_definitions(blueprint.root)
    for pathurl in blueprint.root.findall(".//pathurl"):
        if pathurl.text and pathurl.text.startswith("file:"):
            local_path = local_path_from_url(pathurl.text)
            if local_path is not None and not local_path.exists():
                raise GeneratorError(f"Media path sau relink không tồn tại: {local_path}")
    if not definitions:
        raise GeneratorError("XML không có file definition")
    return max_timecode_drift


def resolve_arguments(args: argparse.Namespace) -> argparse.Namespace:
    project_dir = (
        existing_directory(Path(args.project_dir).expanduser().resolve(), "Project dir")
        if args.project_dir
        else resolve_default_project()
    )
    reference_xml = resolve_project_file(
        project_dir, args.reference_xml, DEFAULT_XML_NAME, "Reference XML"
    )
    content = resolve_project_file(project_dir, args.content, DEFAULT_CONTENT_NAME, "Content DOCX")
    output = (
        Path(args.output).expanduser().resolve()
        if args.output
        else reference_xml.with_name(reference_xml.stem + "_v3.xml")
    )
    if output.resolve() == reference_xml.resolve():
        raise GeneratorError("Output không được ghi đè reference XML; hãy chọn --output khác")

    args.project_dir = project_dir
    args.reference_xml = reference_xml
    args.content = content
    args.output = output
    return args


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Tạo XML v3 cho Bài Karen 0509 từ content.docx và Bài 0509.xml"
    )
    parser.add_argument(
        "--project-dir",
        help="Thư mục project; mặc định tự tìm 'Bài Karen 0509' cạnh repo/tool",
    )
    parser.add_argument("--content", help="DOCX; mặc định project-dir/content.docx")
    parser.add_argument(
        "--reference-xml",
        "--xml",
        dest="reference_xml",
        help="XML blueprint; mặc định project-dir/Bài 0509.xml",
    )
    parser.add_argument("--video-dir", help="Thư mục chứa video 1-6/TH1-TH6")
    parser.add_argument("--audio-dir", help="Thư mục chứa voice-over theo natural sort")
    parser.add_argument("--output", help="XML đầu ra; mặc định Bài 0509_v3.xml")
    parser.add_argument("--sequence-name", help="Đổi tên sequence trong XML đầu ra")
    parser.add_argument("--dry-run", action="store_true", help="Kiểm tra nhưng không ghi XML")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args = resolve_arguments(args)
        cuts = parse_docx_video_cuts(args.content)
        blueprint = load_blueprint(args.reference_xml)
        video_dir, videos = discover_video_media(args.project_dir, args.video_dir)
        audio_dir, audios = discover_audio_media(
            args.project_dir, args.audio_dir, len(blueprint.narration_clips)
        )

        relink_blueprint(blueprint, videos, audios)
        if args.sequence_name:
            set_child_text(blueprint.sequence, "name", args.sequence_name)
        max_drift = validate_blueprint(blueprint, cuts, videos, audios)

        duration = int_text(blueprint.sequence, "duration") / blueprint.fps
        print("=" * 72)
        print("Simple Premiere XML Generator v3 — Bài Karen 0509")
        print("=" * 72)
        print(f"[PROJECT] {args.project_dir}")
        print(f"[CONTENT] {args.content.name}: {len(cuts)} video cut")
        print(
            f"[BLUEPRINT] {args.reference_xml.name}: {len(blueprint.video_clips)} video + "
            f"{len(blueprint.narration_clips)} voice-over"
        )
        print(f"[VIDEO] {video_dir}: {len(videos)} file")
        print(f"[AUDIO] {audio_dir}: {len(audios)} file")
        print(f"[TIMELINE] {duration:.2f}s @ {blueprint.fps} fps")
        print(
            f"[TIMECODE] Thứ tự khớp DOCX; giữ offset đã tinh chỉnh trong XML "
            f"(drift lớn nhất {max_drift:.3f}s)"
        )
        print("[VALIDATE] OK")

        if args.dry_run:
            print("[DRY RUN] Không ghi XML")
            return 0
        write_xml_atomic(blueprint.root, args.output)
        print(f"[OUTPUT] {args.output} ({args.output.stat().st_size / 1024:.1f} KB)")
        return 0
    except GeneratorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
