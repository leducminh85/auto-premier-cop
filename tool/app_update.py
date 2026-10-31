"""HTTPS updates for the Windows one-file build. No project files are touched."""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

APP_VERSION = "3.1.2"
DEFAULT_UPDATE_URL = "https://auto-premier-updates.wevic.vn/latest.json"
MAX_DOWNLOAD = 2 * 1024 ** 3


class UpdateError(RuntimeError):
    pass


def version_tuple(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not re.fullmatch(r"\d{1,6}\.\d{1,6}\.\d{1,6}", value):
        raise UpdateError("Thông tin phiên bản mới không hợp lệ.")
    return tuple(map(int, value.split(".")))


def https_url(value: str) -> str:
    if not isinstance(value, str):
        raise UpdateError("Địa chỉ cập nhật không hợp lệ.")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise UpdateError("Địa chỉ cập nhật phải bắt đầu bằng https://.")
    return value


class SecureRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        https_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_url(url: str):
    request = Request(https_url(url), headers={
        "User-Agent": f"DumiCutRoom/{APP_VERSION}", "Cache-Control": "no-cache",
    })
    return build_opener(SecureRedirect()).open(request, timeout=20)


def load_config() -> dict:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    path = root / "update_config.json"
    if not path.is_file():
        return {"version": APP_VERSION, "manifest_url": DEFAULT_UPDATE_URL}
    data = json.loads(path.read_text(encoding="utf-8"))
    version_tuple(data["version"])
    if data.get("manifest_url"):
        https_url(data["manifest_url"])
    return data


def validate_release(data: object) -> dict:
    if not isinstance(data, dict):
        raise UpdateError("Thông tin cập nhật không hợp lệ.")
    version_tuple(data.get("version"))
    https_url(data.get("url"))
    if data.get("platform") != "windows-x64":
        raise UpdateError("Bản cập nhật này không dành cho Windows 64-bit.")
    if not isinstance(data.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", data["sha256"]):
        raise UpdateError("Thông tin kiểm tra file cập nhật bị thiếu.")
    if type(data.get("size")) is not int or not 0 < data["size"] <= MAX_DOWNLOAD:
        raise UpdateError("Dung lượng bản cập nhật không hợp lệ.")
    if not isinstance(data.get("notes", ""), str) or len(data.get("notes", "")) > 4000:
        raise UpdateError("Thông tin cập nhật quá dài.")
    return data


def check_release(url: str, current_version: str) -> dict | None:
    with open_url(url) as response:
        raw = response.read(65537)
    if len(raw) > 65536:
        raise UpdateError("Thông tin cập nhật quá dài.")
    try:
        release = validate_release(json.loads(raw))
    except (ValueError, TypeError, KeyError) as exc:
        raise UpdateError("Không đọc được thông tin cập nhật. Hãy thử lại sau.") from exc
    return release if version_tuple(release["version"]) > version_tuple(current_version) else None


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def download_release(release: dict, target: Path, progress=lambda value: None) -> Path:
    """Stage next to the target for atomic replacement on the same volume."""
    validate_release(release)
    target = target.resolve(strict=True)
    try:
        stage = Path(tempfile.mkdtemp(prefix=".dumi-update-", dir=target.parent))
    except OSError as exc:
        raise UpdateError("Không có quyền cập nhật ở thư mục này. Hãy nhờ người cài app hỗ trợ.") from exc
    try:
        payload = stage / "new.exe"
        received = 0
        digest = hashlib.sha256()
        deadline = time.monotonic() + 1800
        with open_url(release["url"]) as response, payload.open("xb") as output:
            while chunk := response.read(256 * 1024):
                received += len(chunk)
                if received > release["size"] or time.monotonic() > deadline:
                    raise UpdateError("Không tải được bản cập nhật đầy đủ. Hãy thử lại.")
                output.write(chunk)
                digest.update(chunk)
                progress(int(received * 100 / release["size"]))
        if received != release["size"] or digest.hexdigest() != release["sha256"]:
            raise UpdateError("File cập nhật bị thiếu hoặc bị lỗi. Hãy tải lại.")
        with payload.open("rb") as stream:
            if stream.read(2) != b"MZ":
                raise UpdateError("File cập nhật không phải ứng dụng Windows.")
        job = {
            "target": str(target), "sha256": release["sha256"],
            "old_sha256": file_hash(target), "size": release["size"],
            "parent_pid": os.getpid(), "version": release["version"],
        }
        job_path = stage / "job.json"
        job_path.write_text(json.dumps(job), encoding="utf-8")
        return job_path
    except Exception:
        shutil.rmtree(stage)  # Only our newly created staging directory.
        raise


def launch_helper(job_path: Path) -> None:
    if os.name != "nt" or not getattr(sys, "frozen", False):
        raise UpdateError("Tự cập nhật chỉ dùng được trong bản .exe trên Windows.")
    helper = job_path.parent / "updater.exe"
    shutil.copy2(sys.executable, helper)
    subprocess.Popen(
        [str(helper), "--apply-update", str(job_path)],
        env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"},
        cwd=str(job_path.parent), creationflags=subprocess.CREATE_NO_WINDOW,
    )


def wait_for_parent(pid: int, timeout: int = 120) -> None:
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x00100000, False, pid)
    if not handle:
        if ctypes.get_last_error() == 87:  # Already exited.
            return
        raise UpdateError("Không thể chờ ứng dụng đóng. Hãy thử lại.")
    try:
        if kernel.WaitForSingleObject(handle, timeout * 1000) != 0:
            raise UpdateError("Ứng dụng chưa đóng. Hãy đóng app rồi thử cập nhật lại.")
    finally:
        kernel.CloseHandle(handle)


def replace_with_retry(source: Path, destination: Path, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.25)


@contextmanager
def update_lock(target: str):
    """Serialize helpers across multiple open instances of the same app."""
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    name = "Local\\DumiUpdate-" + hashlib.sha256(target.casefold().encode()).hexdigest()
    handle = kernel.CreateMutexW(None, False, name)
    if not handle:
        raise UpdateError("Không thể bắt đầu cập nhật. Hãy thử lại.")
    acquired = False
    try:
        acquired = kernel.WaitForSingleObject(handle, 0) in (0, 128)
        if not acquired:
            raise UpdateError("Một cửa sổ khác đang cập nhật ứng dụng. Hãy chờ hoàn tất.")
        yield
    finally:
        if acquired:
            kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)


def apply_update(job_path: Path) -> None:
    target = json.loads(job_path.read_text(encoding="utf-8"))["target"]
    with update_lock(target):
        _apply_update_unlocked(job_path)


def _apply_update_unlocked(job_path: Path) -> None:
    """Run only from the copied helper after the original application exits."""
    job_path = job_path.resolve(strict=True)
    stage = job_path.parent
    job = json.loads(job_path.read_text(encoding="utf-8"))
    target = Path(job["target"])
    if (not target.is_absolute() or target.resolve() != target
            or target.parent != stage.parent or not stage.name.startswith(".dumi-update-")
            or target.suffix.lower() != ".exe" or job_path.name != "job.json"):
        raise UpdateError("Vị trí cập nhật không hợp lệ.")
    payload = stage / "new.exe"
    backup = stage / "previous.exe"
    if backup.exists():
        raise UpdateError("Lần cập nhật này đã được xử lý. Hãy kiểm tra cập nhật lại.")
    if payload.stat().st_size != job["size"] or file_hash(payload) != job["sha256"]:
        raise UpdateError("File cập nhật bị lỗi. Bản hiện tại được giữ nguyên.")
    wait_for_parent(job["parent_pid"])
    if file_hash(target) != job["old_sha256"]:
        raise UpdateError("Ứng dụng đã thay đổi. Hãy mở lại app và kiểm tra cập nhật.")
    replace_with_retry(target, backup)
    try:
        replace_with_retry(payload, target)
        subprocess.Popen(
            [str(target)], cwd=str(target.parent),
            env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"},
        )
    except Exception:
        replace_with_retry(backup, target)
        raise
    (stage / "result.json").write_text(json.dumps({"updated": job["version"]}), encoding="utf-8")


def helper_main(job_path: str) -> int:
    try:
        apply_update(Path(job_path))
    except Exception as exc:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None, f"Chưa cập nhật được ứng dụng.\n{exc}\nHãy mở lại app hoặc liên hệ người cài app.",
            "Cập nhật ứng dụng", 0x10,
        )
        return 1
    return 0
