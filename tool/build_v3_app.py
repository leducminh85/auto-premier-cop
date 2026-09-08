#!/usr/bin/env python3
"""Build the v3 desktop app with PyInstaller for the current operating system."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


TOOL_DIR = Path(__file__).resolve().parent
REPOSITORY = TOOL_DIR.parent
ENTRY_POINT = TOOL_DIR / "generate_simple_xml_v3_app.py"
ASSET_DIR = TOOL_DIR / "assets"
LOGO_PATH = ASSET_DIR / "xmlcutroom-logo.png"


def platform_build_settings() -> tuple[str, Path, str]:
    if sys.platform == "darwin":
        return "xmlcutroom-mac", ASSET_DIR / "xmlcutroom.icns", "--onedir"
    if os.name == "nt":
        return "xmlcutroom-win", ASSET_DIR / "xmlcutroom.ico", "--onefile"
    return "xmlcutroom-linux", LOGO_PATH, "--onefile"


def find_ffprobe() -> Path:
    configured = os.environ.get("FFPROBE_BINARY", "").strip()
    if configured:
        path = Path(configured).expanduser().resolve()
        if path.is_file():
            return path
        raise SystemExit(f"FFPROBE_BINARY không phải file hợp lệ: {path}")

    executable = shutil.which("ffprobe")
    if executable is None:
        executable = shutil.which("ffprobe.exe")
    if executable is None:
        raise SystemExit(
            "Không tìm thấy ffprobe trong PATH. Hãy cài FFmpeg trước khi build."
        )
    return Path(executable).resolve()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dist-dir",
        type=Path,
        default=REPOSITORY / "dist",
        help="Thư mục chứa artifact sau khi build (mặc định: repo/dist)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    ffprobe = find_ffprobe()
    app_name, icon_path, bundle_mode = platform_build_settings()
    for asset in (LOGO_PATH, icon_path):
        if not asset.is_file():
            raise SystemExit(f"Không tìm thấy asset build: {asset}")
    build_root = REPOSITORY / "build"
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        bundle_mode,
        "--windowed",
        "--name",
        app_name,
        "--icon",
        str(icon_path),
        "--paths",
        str(TOOL_DIR),
        "--distpath",
        str(args.dist_dir.resolve()),
        "--workpath",
        str(build_root / "pyinstaller"),
        "--specpath",
        str(build_root / "pyinstaller-spec"),
        "--add-binary",
        f"{ffprobe}{os.pathsep}.",
        "--add-data",
        f"{LOGO_PATH}{os.pathsep}.",
        str(ENTRY_POINT),
    ]
    print(f"Building {app_name} with {ffprobe.name}...")
    subprocess.run(command, cwd=REPOSITORY, check=True)

    artifact = args.dist_dir.resolve() / (
        f"{app_name}.exe" if os.name == "nt" else app_name
    )
    if sys.platform == "darwin":
        artifact = args.dist_dir.resolve() / f"{app_name}.app"
    print(f"Build complete: {artifact}")


if __name__ == "__main__":
    main()
