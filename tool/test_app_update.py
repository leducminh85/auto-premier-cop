import hashlib
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app_update as updater
from publish_update import publish


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.target = self.root / "App with spaces.exe"
        self.target.write_bytes(b"MZold app")
        self.payload = b"MZnew app"
        self.release = {
            "version": "3.2.0", "url": "https://updates.example.com/releases/app.exe",
            "platform": "windows-x64", "size": len(self.payload),
            "sha256": hashlib.sha256(self.payload).hexdigest(), "notes": "New version",
        }

    def stage(self):
        with patch.object(updater, "open_url", return_value=BytesIO(self.payload)):
            return updater.download_release(self.release, self.target)

    def test_version_comparison_does_not_downgrade(self):
        for current, available in [("3.1.0", True), ("3.2.0", False), ("3.10.0", False)]:
            with patch.object(updater, "open_url", return_value=BytesIO(json.dumps(self.release).encode())):
                self.assertEqual(updater.check_release(self.release["url"], current) is not None, available)

    def test_cleanup_only_completed_updates_for_this_app(self):
        job = self.stage()
        updater.cleanup_completed_updates(self.target)
        self.assertTrue(job.exists())
        (job.parent / 'result.json').write_text(json.dumps({'updated': '3.2.0'}))
        updater.cleanup_completed_updates(self.root / 'other.exe')
        self.assertTrue(job.exists())
        extra = job.parent / 'personal.txt'
        extra.write_text('Keep me')
        updater.cleanup_completed_updates(self.target)
        self.assertTrue(extra.exists())
        extra.unlink()
        updater.cleanup_completed_updates(self.target)
        self.assertFalse(job.parent.exists())
        self.assertEqual(self.target.read_bytes(), b'MZold app')

    def test_cleanup_retries_locked_helper(self):
        job = self.stage()
        (job.parent / 'result.json').write_text(json.dumps({'updated': '3.2.0'}))
        with patch.object(Path, 'unlink', side_effect=PermissionError):
            updater.cleanup_completed_updates(self.target)
        self.assertTrue(job.exists())
        updater.cleanup_completed_updates(self.target)
        self.assertFalse(job.parent.exists())

    def test_invalid_metadata_and_insecure_urls_rejected(self):
        for field, value in [("version", "next"), ("url", "http://example.com/app.exe"),
                             ("url", "file:///app.exe"), ("size", -1), ("size", True),
                             ("sha256", "broken"), ("platform", "macos")]:
            with self.subTest(field=field, value=value), self.assertRaises(updater.UpdateError):
                updater.validate_release({**self.release, field: value})
        with self.assertRaises(updater.UpdateError):
            updater.SecureRedirect().redirect_request(None, None, 302, "", {}, "http://example.com/app.exe")

    def test_bad_download_never_modifies_current_app(self):
        for payload in [self.payload[:-1], b"MZbad app", self.payload + b"extra"]:
            with patch.object(updater, "open_url", return_value=BytesIO(payload)):
                with self.assertRaises(updater.UpdateError):
                    updater.download_release(self.release, self.target)
            self.assertEqual(self.target.read_bytes(), b"MZold app")
            self.assertEqual(list(self.root.glob(".dumi-update-*")), [])

    def test_network_error_cleans_partial_download(self):
        with patch.object(updater, "open_url", side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):
                updater.download_release(self.release, self.target)
        self.assertEqual(list(self.root.glob(".dumi-update-*")), [])

    def test_apply_retains_backup_and_launches_independent_app(self):
        job = self.stage()
        with patch.object(updater, "wait_for_parent") as wait, patch.object(updater.subprocess, "Popen") as launch:
            updater.apply_update(job)
        wait.assert_called_once()
        self.assertEqual(self.target.read_bytes(), self.payload)
        self.assertEqual((job.parent / "previous.exe").read_bytes(), b"MZold app")
        self.assertEqual(launch.call_args.kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"], "1")

    def test_launch_failure_restores_original(self):
        job = self.stage()
        with patch.object(updater, "wait_for_parent"), patch.object(updater.subprocess, "Popen", side_effect=OSError("launch failed")):
            with self.assertRaises(OSError):
                updater.apply_update(job)
        self.assertEqual(self.target.read_bytes(), b"MZold app")

    def test_modified_payload_or_target_rejected_before_replacement(self):
        for changed_file in ["new.exe", "target"]:
            job = self.stage()
            changed = self.target if changed_file == "target" else job.parent / changed_file
            changed.write_bytes(b"changed")
            with patch.object(updater, "wait_for_parent"), self.assertRaises(updater.UpdateError):
                updater.apply_update(job)
            self.assertFalse((job.parent / "previous.exe").exists())
            self.target.write_bytes(b"MZold app")

    def test_publish_and_prevent_same_version_replacement(self):
        self.target.with_suffix(".release.json").write_text(json.dumps({
            "version": "3.2.0", "manifest_url": "https://updates.example.com/latest.json",
            "platform": "windows-x64",
        }))
        root = self.root / "server"
        manifest = publish(self.target, root)
        data = updater.validate_release(json.loads(manifest.read_text(encoding="utf-8")))
        self.assertEqual(data["sha256"], updater.file_hash(self.target))
        self.target.write_bytes(b"MZdifferent build")
        with self.assertRaises(ValueError):
            publish(self.target, root)
        self.assertEqual(json.loads(manifest.read_text(encoding="utf-8")), data)


if __name__ == "__main__":
    unittest.main()
