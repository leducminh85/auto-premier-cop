"""Prepare a release on this PC; publish latest.json only after the EXE is ready."""
import argparse
import json
import os
from pathlib import Path
import shutil
import tempfile
from urllib.parse import urlsplit

from app_update import file_hash, https_url, validate_release, version_tuple

DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "update-server"


def publish(exe: Path, root: Path, notes: str = "") -> Path:
    exe = exe.resolve(strict=True)
    config = json.loads(exe.with_suffix(".release.json").read_text(encoding="utf-8"))
    manifest_url = https_url(config["manifest_url"])
    if urlsplit(manifest_url).path != "/latest.json" or urlsplit(manifest_url).query:
        raise ValueError("Địa chỉ cập nhật phải có dạng https://ten-mien/latest.json")
    version_tuple(config["version"])
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    releases = root / "releases"
    releases.mkdir(exist_ok=True)
    destination = releases / f"Dumi-Cut-Room-{config['version']}.exe"
    digest = file_hash(exe)
    release = validate_release({
        "version": config["version"], "platform": config["platform"],
        "url": manifest_url.rsplit("/", 1)[0] + "/releases/" + destination.name,
        "sha256": digest, "size": exe.stat().st_size, "notes": notes,
    })
    latest = root / "latest.json"
    if latest.exists():
        old = json.loads(latest.read_text(encoding="utf-8"))
        if version_tuple(release["version"]) < version_tuple(old["version"]):
            raise ValueError("Phiên bản mới phải cao hơn phiên bản đang phát hành.")
    if destination.exists() and file_hash(destination) != digest:
        raise ValueError("Phiên bản này đã có file khác. Hãy tăng số phiên bản rồi build lại.")
    with tempfile.TemporaryDirectory(prefix="publish-", dir=root) as temporary:
        staged = Path(temporary) / "release.exe"
        shutil.copyfile(exe, staged)
        if file_hash(staged) != digest:
            raise ValueError("File thay đổi trong khi sao chép. Hãy thử lại.")
        if not destination.exists():
            os.replace(staged, destination)
        manifest = Path(temporary) / "latest.json"
        manifest.write_text(json.dumps(release, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(manifest, latest)
    return latest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--notes", default="")
    args = parser.parse_args()
    print(publish(args.exe, args.root, args.notes))


if __name__ == "__main__":
    main()
