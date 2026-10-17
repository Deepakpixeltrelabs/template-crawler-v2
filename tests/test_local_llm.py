"""Offline tests for the Windows local model installer/launcher helpers."""
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import local_llm  # noqa: E402


class LocalLLMInstallerTests(unittest.TestCase):
    def test_selects_official_windows_x64_cpu_asset(self):
        assets = [
            {"name": "llama-b1-bin-win-cuda-12-x64.zip", "browser_download_url": "https://example/cuda.zip"},
            {"name": "llama-b1-bin-win-cpu-arm64.zip", "browser_download_url": "https://example/arm.zip"},
            {"name": "llama-b1-bin-win-cpu-x64.zip", "browser_download_url": "https://example/cpu.zip"},
            {"name": "llama-b1-bin-win-cpu-x64-mtmd.zip", "browser_download_url": "https://example/mtmd.zip"},
        ]
        selected = local_llm.select_windows_cpu_asset(assets)
        self.assertEqual(selected["name"], "llama-b1-bin-win-cpu-x64.zip")

    def test_scans_recent_releases_when_stable_latest_has_no_runtime(self):
        releases = [
            {"tag_name": "v0.7.0", "assets": [
                {"name": "nightly-tag.txt", "browser_download_url": "https://example/tag.txt"},
            ]},
            {"tag_name": "b11669", "prerelease": True, "assets": [
                {"name": "llama-b11669-bin-win-cpu-x64.zip", "browser_download_url": "https://example/cpu.zip"},
                {"name": "llama-b11669-bin-win-cuda-12-x64.zip", "browser_download_url": "https://example/cuda.zip"},
            ]},
        ]
        release, asset = local_llm.select_windows_cpu_release(releases)
        self.assertEqual(release["tag_name"], "b11669")
        self.assertEqual(asset["name"], "llama-b11669-bin-win-cpu-x64.zip")

    def test_ignores_releases_without_cpu_asset(self):
        releases = [{"tag_name": "v1", "assets": []}, {"tag_name": "v2", "assets": [
            {"name": "llama-bin-win-cpu-arm64.zip", "browser_download_url": "https://example/arm.zip"},
        ]}]
        with self.assertRaises(RuntimeError):
            local_llm.select_windows_cpu_release(releases)

    def test_rejects_runtime_zip_path_traversal(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("..\\outside.txt", "bad")
            with self.assertRaises(RuntimeError):
                local_llm.safe_extract_zip(archive, root / "runtime")
            self.assertFalse((root / "outside.txt").exists())

    def test_extracts_normal_runtime_archive(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = root / "ok.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("bin/llama-server.exe", "test runtime")
                zf.writestr("bin/runtime.dll", "test dll")
            target = root / "runtime"
            local_llm.safe_extract_zip(archive, target)
            self.assertEqual((target / "bin" / "llama-server.exe").read_text(), "test runtime")
            self.assertEqual((target / "bin" / "runtime.dll").read_text(), "test dll")

    def test_model_config_is_pinned_and_uses_gguf(self):
        self.assertTrue(local_llm.MODEL_NAME.endswith(".gguf"))
        self.assertEqual(len(local_llm.MODEL_SHA256), 64)
        self.assertIn("Qwen2.5-Coder-3B-Instruct-GGUF", local_llm.MODEL_URL)
        self.assertIn("Q4_K_M", local_llm.MODEL_NAME)


if __name__ == "__main__":
    unittest.main()
