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

from content_parser import read_content

from generate_xml import (
    AUDIO_EXTENSIONS,
    VIDEO_EXTENSIONS,
    GeneratorError,
    existing_directory,
    existing_file,
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
TH_NUMBER_RE = re.compile(r"TH\s*(\d+)", re.IGNORECASE)
REFERENCE_TIMECODE_TOLERANCE_SECONDS = 1.25


@dataclass(frozen=True)
class VideoCut:
    source_number: int
    source_in_seconds: float
    source_out_seconds: float
    location: str


@dataclass(frozen=True)
class ArticleComponent:
    key: str
    title: str
    present: bool
    video_cut_count: int
    text_block_count: int
    timecodes: tuple[str, ...]


@dataclass(frozen=True)
class Blueprint:
    root: ET.Element
    sequence: ET.Element
    video_clips: list[ET.Element]
    narration_clips: list[ET.Element]
    timeline_clips: list[ET.Element]
    fps: int


@dataclass(frozen=True)
class GenerationSummary:
    output: Path
    video_dir: Path
    audio_dir: Path
    video_cut_count: int
    narration_count: int
    duration_seconds: float
    fps: int
    max_timecode_drift: float
    removed_blur_effect_count: int = 0


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
    return [VideoCut(e.source, e.start, e.end, e.location)
            for e in read_content(docx_path) if e.kind == "video"]


def format_timecode(seconds: float) -> str:
    total_seconds = round(seconds)
    minutes, remainder = divmod(total_seconds, 60)
    return f"{minutes:02d}:{remainder:02d}"


def inspect_docx_components(docx_path: Path) -> list[ArticleComponent]:
    sections = {key: {"present": False, "cuts": [], "texts": 0}
                for key in ["intro", *(f"th{i}" for i in range(1, 7)), "outro"]}
    for event in read_content(docx_path):
        data = sections[event.section]
        data["present"] = True
        if event.kind == "video":
            data["cuts"].append(f"{format_timecode(event.start)} - {format_timecode(event.end)}")
        elif event.kind == "audio":
            data["texts"] += 1

    labels = [("intro", "Intro")]
    labels.extend((f"th{number}", f"Tình huống {number}") for number in range(1, 7))
    labels.append(("outro", "Kết bài"))
    result: list[ArticleComponent] = []
    for key, title in labels:
        data = sections[key]
        timecodes = tuple(str(value) for value in data["cuts"])
        result.append(
            ArticleComponent(
                key=key,
                title=title,
                present=bool(data["present"]),
                video_cut_count=len(timecodes),
                text_block_count=int(data["texts"]),
                timecodes=timecodes,
            )
        )
    return result


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


def remove_video_blur_effects(blueprint: Blueprint) -> int:
    """Remove blur filters inherited from the reference timeline's video clips."""
    removed = 0
    video_clips = blueprint.sequence.findall("./media/video/track/clipitem")
    for clip in video_clips:
        for filter_element in list(clip.findall("filter")):
            effect = filter_element.find("effect")
            if effect is None:
                continue
            identifiers = (
                effect.findtext("effectid") or "",
                effect.findtext("name") or "",
                effect.findtext("effectcategory") or "",
            )
            if any("blur" in value.casefold() for value in identifiers):
                clip.remove(filter_element)
                removed += 1
    return removed


def video_blur_effects(blueprint: Blueprint) -> list[str]:
    result: list[str] = []
    video_clips = blueprint.sequence.findall("./media/video/track/clipitem")
    for clip in video_clips:
        for effect in clip.findall("./filter/effect"):
            identifiers = (
                effect.findtext("effectid") or "",
                effect.findtext("name") or "",
                effect.findtext("effectcategory") or "",
            )
            if any("blur" in value.casefold() for value in identifiers):
                result.append(
                    effect.findtext("effectid") or effect.findtext("name") or "Blur"
                )
    return result


def validate_written_xml(
    output: Path, expected_video_count: int, expected_narration_count: int
) -> None:
    """Parse the written file again and verify its critical output invariants."""
    written = load_blueprint(output)
    if len(written.video_clips) != expected_video_count:
        raise GeneratorError(
            f"XML đã ghi có {len(written.video_clips)} video cut; "
            f"cần {expected_video_count}"
        )
    if len(written.narration_clips) != expected_narration_count:
        raise GeneratorError(
            f"XML đã ghi có {len(written.narration_clips)} voice-over; "
            f"cần {expected_narration_count}"
        )
    remaining_blurs = video_blur_effects(written)
    if remaining_blurs:
        raise GeneratorError(
            f"XML đã ghi vẫn còn {len(remaining_blurs)} hiệu ứng làm mờ trên video"
        )


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


def generate_project_xml(
    *,
    project_dir: Path,
    content: Path,
    reference_xml: Path,
    output: Path,
    video_dir: Path | None = None,
    audio_dir: Path | None = None,
    sequence_name: str | None = None,
    write_output: bool = True,
) -> GenerationSummary:
    """Generate and validate one XML; shared by the CLI and desktop app."""
    project_dir = existing_directory(project_dir.expanduser().resolve(), "Project dir")
    content = existing_file(content.expanduser().resolve(), "Content DOCX")
    reference_xml = existing_file(reference_xml.expanduser().resolve(), "Reference XML")
    output = output.expanduser().resolve()
    if output == reference_xml:
        raise GeneratorError("Output không được ghi đè reference XML; hãy chọn output khác")

    cuts = parse_docx_video_cuts(content)
    blueprint = load_blueprint(reference_xml)
    discovered_video_dir, videos = discover_video_media(
        project_dir, str(video_dir.expanduser().resolve()) if video_dir else None
    )
    discovered_audio_dir, audios = discover_audio_media(
        project_dir,
        str(audio_dir.expanduser().resolve()) if audio_dir else None,
        len(blueprint.narration_clips),
    )

    relink_blueprint(blueprint, videos, audios)
    removed_blur_effect_count = remove_video_blur_effects(blueprint)
    if sequence_name:
        set_child_text(blueprint.sequence, "name", sequence_name)
    max_drift = validate_blueprint(blueprint, cuts, videos, audios)
    if write_output:
        write_xml_atomic(blueprint.root, output)
        validate_written_xml(output, len(cuts), len(blueprint.narration_clips))

    return GenerationSummary(
        output=output,
        video_dir=discovered_video_dir,
        audio_dir=discovered_audio_dir,
        video_cut_count=len(cuts),
        narration_count=len(blueprint.narration_clips),
        duration_seconds=int_text(blueprint.sequence, "duration") / blueprint.fps,
        fps=blueprint.fps,
        max_timecode_drift=max_drift,
        removed_blur_effect_count=removed_blur_effect_count,
    )


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
        summary = generate_project_xml(
            project_dir=args.project_dir,
            content=args.content,
            reference_xml=args.reference_xml,
            output=args.output,
            video_dir=Path(args.video_dir) if args.video_dir else None,
            audio_dir=Path(args.audio_dir) if args.audio_dir else None,
            sequence_name=args.sequence_name,
            write_output=not args.dry_run,
        )
        print("=" * 72)
        print("Simple Premiere XML Generator v3 - Bài Karen 0509")
        print("=" * 72)
        print(f"[PROJECT] {args.project_dir}")
        print(f"[CONTENT] {args.content.name}: {summary.video_cut_count} video cut")
        print(
            f"[BLUEPRINT] {args.reference_xml.name}: {summary.video_cut_count} video + "
            f"{summary.narration_count} voice-over"
        )
        print(f"[VIDEO] {summary.video_dir}: 6 file")
        print(f"[AUDIO] {summary.audio_dir}: {summary.narration_count} file")
        print(f"[TIMELINE] {summary.duration_seconds:.2f}s @ {summary.fps} fps")
        print(
            f"[TIMECODE] Thứ tự khớp DOCX; giữ offset đã tinh chỉnh trong XML "
            f"(drift lớn nhất {summary.max_timecode_drift:.3f}s)"
        )
        print(
            f"[CLEANUP] Đã xóa {summary.removed_blur_effect_count} "
            "hiệu ứng làm mờ khỏi video"
        )
        print("[VALIDATE] OK")

        if args.dry_run:
            print("[DRY RUN] Không ghi XML")
            return 0
        print(f"[OUTPUT] {args.output} ({args.output.stat().st_size / 1024:.1f} KB)")
        return 0
    except GeneratorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
