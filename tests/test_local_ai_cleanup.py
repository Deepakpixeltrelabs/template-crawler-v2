"""Regression tests proving standalone cleanup applies and records real edits."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import local_ai_cleanup as cleanup  # noqa: E402


class LocalCleanupPipelineTests(unittest.TestCase):
    def make_package(self, root: Path) -> Path:
        src = root / "raw" / "demo-template"
        (src / "assets" / "images").mkdir(parents=True)
        (src / "assets" / "images" / "hero.jpg").write_bytes(b"fake-image-for-test")
        (src / "metadata.json").write_text(json.dumps({"title": "Demo"}), encoding="utf-8")
        (src / "index.html").write_text(
            '<!doctype html><html><head>'
            '<meta name="generator" content="Wix.com Website Builder">'
            '<script src="https://www.googletagmanager.com/gtag/js?id=G-TEST"></script>'
            '</head><body><header><h1>Bright Studio</h1></header>'
            '<img src="https://static.wixstatic.com/media/hero.jpg" alt="Hero">'
            '<script id="viewer-shell">window.templateViewer = true;</script>'
            '</body></html>', encoding="utf-8")
        (src / "asset-manifest.json").write_text(json.dumps({
            "assets": [{"url": "https://static.wixstatic.com/media/hero.jpg",
                        "local": "assets/images/hero.jpg", "category": "image"}]
        }), encoding="utf-8")
        return src

    def test_deterministic_cleanup_localizes_assets_removes_trackers_and_wrapper(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = self.make_package(root)
            dest = root / "clean" / "demo-template"
            with patch.object(cleanup, "get_models", return_value=[{"id": "test-model"}]), \
                 patch.object(cleanup, "_chat", return_value='{"edits":[]}'):
                result = cleanup.process_template(src, dest, base_url="http://local/v1", model="test-model")
            html = (dest / "index.html").read_text(encoding="utf-8")
            self.assertNotIn("googletagmanager.com", html)
            self.assertNotIn('name="generator"', html)
            self.assertIn('src="assets/images/hero.jpg"', html)
            self.assertNotIn("static.wixstatic.com", html)
            self.assertIn("Bright Studio", html)
            report = json.loads((dest / "cleanup-report.json").read_text(encoding="utf-8"))
            self.assertGreaterEqual(report["changes_applied"], 2)
            self.assertTrue(report["asset_rewrites"])
            self.assertEqual(result["path"], str(dest))
            # Source files are deliberately preserved.
            source_html = (src / "index.html").read_text(encoding="utf-8")
            self.assertIn("googletagmanager.com", source_html)
            self.assertIn("static.wixstatic.com", source_html)

    def test_llm_edits_are_applied_to_the_saved_copy_and_counted(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = self.make_package(root)
            dest = root / "clean" / "demo-template"
            response = json.dumps({"edits": [{
                "find": '<script id="viewer-shell">window.templateViewer = true;</script>',
                "replace": "",
                "reason": "This exact block is viewer-only bootstrap, not site functionality."
            }], "notes": [], "dependencies_to_review": []})
            with patch.object(cleanup, "get_models", return_value=[{"id": "test-model"}]), \
                 patch.object(cleanup, "_chat", return_value=response) as chat:
                cleanup.process_template(src, dest, base_url="http://local/v1", model="test-model")
            html = (dest / "index.html").read_text(encoding="utf-8")
            self.assertNotIn("window.templateViewer", html)
            report = json.loads((dest / "cleanup-report.json").read_text(encoding="utf-8"))
            self.assertGreaterEqual(len(report["llm_edits"]), 1)
            self.assertGreaterEqual(len(report["llm_requests"]), 1)
            self.assertEqual(report["llm_requests"][0]["edits_applied"], 1)
            self.assertEqual(report["prompt_file"], "standalone_cleanup_prompt.txt")
            self.assertGreater(chat.call_count, 0)

    def test_prompt_or_model_change_invalidates_old_cleaned_copy(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = self.make_package(root)
            dest = root / "clean" / "demo-template"
            with patch.object(cleanup, "get_models", return_value=[{"id": "model-a"}]), \
                 patch.object(cleanup, "_chat", return_value='{"edits": [], "notes": ["nothing safe"], "dependencies_to_review": []}') as chat:
                first = cleanup.process_template(src, dest, base_url="http://local/v1", model="model-a")
                second = cleanup.process_template(src, dest, base_url="http://local/v1", model="model-b")
            self.assertNotEqual(first["report"]["pipeline_signature"], second["report"]["pipeline_signature"])
            self.assertEqual(chat.call_count, 2)
            self.assertNotEqual(second["status"], "skipped")

    def test_viewer_iframe_is_removed_but_other_provider_mode_keeps_real_embed(self):
        html = ('<main><iframe data-hook="desktop-iframe" src="https://www.wix.com/view/html/abc"></iframe>'
                '<iframe title="Map" src="https://maps.example/embed/123"></iframe></main>')
        cleaned, removed = cleanup._strip_wix_generator_and_preview_iframe(html)
        self.assertNotIn('data-hook="desktop-iframe"', cleaned)
        self.assertIn('title="Map"', cleaned)
        self.assertEqual(len(removed), 1)
        wix_cleaned, wix_removed = cleanup._strip_wix_generator_and_preview_iframe(html, remove_all_iframes=True)
        self.assertNotIn('<iframe', wix_cleaned.lower())
        self.assertEqual(len(wix_removed), 2)

    def test_active_css_js_detector_ignores_navigation_and_embedded_css_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            package = Path(td)
            (package / "index.html").write_text(
                '<html><head><link rel="canonical" href="https://www.wix.com/demo">'
                '<style data-href="https://static.parastorage.com/site.css">.hero{color:red}</style>'
                '<script src="assets/js/site.js"></script></head><body>'
                '<a href="https://www.wix.com/demo/about">About</a></body></html>',
                encoding="utf-8")
            (package / "assets" / "js").mkdir(parents=True)
            (package / "assets" / "js" / "site.js").write_text(
                '// license https://github.com/example/lib', encoding="utf-8")
            self.assertEqual(cleanup._active_external_css_js_dependencies(package), [])

    def test_active_css_js_detector_flags_actual_remote_dependencies(self):
        with tempfile.TemporaryDirectory() as td:
            package = Path(td)
            (package / "index.html").write_text(
                '<html><head><link rel="stylesheet" href="https://cdn.example/site.css">'
                '<script src="https://cdn.example/app.js"></script></head><body></body></html>',
                encoding="utf-8")
            found = cleanup._active_external_css_js_dependencies(package)
            self.assertEqual({item["type"] for item in found}, {"css", "js"})
            self.assertEqual(len(found), 2)

    def test_user_prompt_is_preserved_and_wix_package_is_cleaned_deterministically(self):
        prompt = cleanup.PROMPT_PATH.read_text(encoding="utf-8")
        self.assertTrue(prompt.startswith("I am providing you with ONE MHTML file containing a SINGLE-PAGE Wix website."))
        self.assertIn("REMOVE THE IFRAME DEPENDENCY", prompt)
        self.assertIn("TEMPLATE-READY STRUCTURE", prompt)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "raw" / "wix-template"
            (src / "assets" / "images").mkdir(parents=True)
            (src / "assets" / "images" / "hero.jpg").write_bytes(b"local image")
            (src / "metadata.json").write_text(json.dumps({
                "provider": "Wix", "title": "A Test Studio", "template_url": "https://www.wix.com/template",
                "preview_url": "https://www.wix.com/demone2/demo"
            }), encoding="utf-8")
            remote_manifest_url = "https://static.wixstatic.com/media/hero.jpg/v1/fill/w_600,h_400/hero.jpg"
            (src / "asset-manifest.json").write_text(json.dumps({"source_url": "https://www.wix.com/demone2/demo", "assets": [
                {"url": remote_manifest_url, "local": "assets/images/hero.jpg", "category": "images", "bytes": 11}
            ]}), encoding="utf-8")
            (src / "index.html").write_text(
                '<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1">'
                '<meta rel="canonical" href="https://www.wix.com/demone2/demo">'
                '<style data-url="https://static.parastorage.com/services/wix-thunderbolt/main.css">'
                '.hero{background-image:url("https://static.wixstatic.com/media/hero.jpg/v1/fill/w_900,h_600/hero.jpg");color:#123;}'
                '</style><script id="wix-essential-viewer-model" type="application/json">{"wix":true}</script>'
                '<script data-url="https://static.parastorage.com/services/wix-thunderbolt/main.js">window.viewerModel={};</script>'
                '</head><body><main><h1>Keep the original page</h1><img src="https://static.wixstatic.com/media/hero.jpg/v1/fill/w_900,h_600/hero.jpg">'
                '<iframe title="Embedded viewer" src="https://example.com/frame"></iframe></main></body></html>', encoding="utf-8")
            dest = root / "clean" / "wix-template"
            with patch.object(cleanup, "get_models", return_value=[{"id": "test-model"}]), \
                 patch.object(cleanup, "_chat", return_value='{"edits":[],"notes":[],"dependencies_to_review":[]}') as chat:
                result = cleanup.process_template(src, dest, base_url="http://local/v1", model="test-model")
            html = (dest / "index.html").read_text(encoding="utf-8")
            self.assertNotIn("wix-essential-viewer-model", html)
            self.assertNotIn("parastorage.com", html)
            self.assertNotIn("wixstatic.com", html)
            self.assertNotIn("<iframe", html.lower())
            self.assertIn(".hero{", html)
            self.assertIn("Keep the original page", html)
            self.assertIn('src="assets/images/hero.jpg"', html)
            self.assertEqual(chat.call_count, 0)  # no active external CSS/JS imports; LLM is skipped
            self.assertTrue(result["report"]["llm_review_skipped_reason"])
            self.assertEqual(result["report"]["validation"]["iframe_tags"], 0)
            self.assertTrue((dest / "standalone-validation.txt").is_file())
            meta = json.loads((dest / "metadata.json").read_text(encoding="utf-8"))
            self.assertNotIn("preview_url", meta)


if __name__ == "__main__":
    unittest.main()
