"""Regression tests for the Wix crawler's recursive local dependency handling."""
from __future__ import annotations

import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse


def _install_selenium_import_stubs():
    """The localization helper has no browser dependency, but lives in the crawler module."""
    names = [
        "selenium", "selenium.webdriver", "selenium.webdriver.chrome",
        "selenium.webdriver.chrome.options", "selenium.webdriver.chrome.service",
        "selenium.webdriver.common", "selenium.webdriver.common.by",
        "selenium.webdriver.support", "selenium.webdriver.support.ui",
        "selenium.webdriver.support.expected_conditions", "selenium.common",
        "selenium.common.exceptions", "webdriver_manager", "webdriver_manager.chrome",
    ]
    saved = {name: sys.modules.get(name) for name in names}
    modules = {name: types.ModuleType(name) for name in names}
    webdriver = modules["selenium.webdriver"]
    modules["selenium"].webdriver = webdriver
    webdriver.Chrome = lambda *a, **k: None
    webdriver.chrome = modules["selenium.webdriver.chrome"]
    modules["selenium.webdriver.chrome"].options = modules["selenium.webdriver.chrome.options"]
    modules["selenium.webdriver.chrome"].service = modules["selenium.webdriver.chrome.service"]
    modules["selenium.webdriver.chrome.options"].Options = type("Options", (), {})
    modules["selenium.webdriver.chrome.service"].Service = type("Service", (), {})
    modules["selenium.webdriver.common"].by = modules["selenium.webdriver.common.by"]
    modules["selenium.webdriver.common.by"].By = type("By", (), {"CSS_SELECTOR": "css selector", "XPATH": "xpath"})
    modules["selenium.webdriver.support"].ui = modules["selenium.webdriver.support.ui"]
    modules["selenium.webdriver.support"].expected_conditions = modules["selenium.webdriver.support.expected_conditions"]
    modules["selenium.webdriver.support.ui"].WebDriverWait = type("WebDriverWait", (), {})
    modules["selenium.webdriver.support.expected_conditions"].presence_of_element_located = lambda *a, **k: None
    modules["selenium.common"].exceptions = modules["selenium.common.exceptions"]
    modules["selenium.common.exceptions"].TimeoutException = type("TimeoutException", (Exception,), {})
    modules["selenium.common.exceptions"].NoSuchElementException = type("NoSuchElementException", (Exception,), {})
    modules["webdriver_manager"].chrome = modules["webdriver_manager.chrome"]
    modules["webdriver_manager.chrome"].ChromeDriverManager = type("ChromeDriverManager", (), {})
    sys.modules.update(modules)
    return saved, names


class WixDependencyLocalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        crawler_dir = Path(__file__).resolve().parents[1] / "crawlers"
        sys.path.insert(0, str(crawler_dir))
        saved, names = _install_selenium_import_stubs()
        try:
            cls.wix = importlib.import_module("wix_template_html_assets")
        finally:
            for name in names:
                old = saved[name]
                if old is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = old

    def test_remote_static_and_dynamic_js_imports_are_downloaded_and_rewritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            (package / "assets" / "js").mkdir(parents=True)
            source_local = "assets/js/main.mjs"
            source_url = "https://static.example.test/services/site/main.mjs"
            source = (
                'import { boot } from "../chunks/boot.js";'
                'const lazy = import("https://static.example.test/chunks/lazy.js");'
                'import "bare-package";'
                'fetch("https://api.example.test/v1/health");'
            )
            requested = []

            def fake_save(url, context, base_url):
                requested.append((url, context, base_url))
                name = Path(urlparse(url).path).name
                local = f"assets/js/local_{name}"
                target = package / local
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("// mocked downloaded dependency", encoding="utf-8")
                return local

            rewritten = self.wix.v4_rewrite_js_imports(
                source, source_url, source_local, package, fake_save
            )

            self.assertEqual(len(requested), 2)
            self.assertIn('from "./local_boot.js"', rewritten)
            self.assertIn('import("./local_lazy.js")', rewritten)
            self.assertIn('import "bare-package"', rewritten)
            self.assertIn('fetch("https://api.example.test/v1/health")', rewritten)
            self.assertTrue(all(r[1] == "JavaScript module import" for r in requested))

    def test_crawl_package_recursively_saves_css_urls_imports_and_js_modules(self):
        html = (
            '<!doctype html><html><head>'
            '<link rel="stylesheet" href="https://cdn.example.test/site.css">'
            '<script type="module" src="https://cdn.example.test/js/main.mjs"></script>'
            '<style data-href="https://cdn.example.test/inline.css">.already-inline{color:red}</style>'
            '</head><body><main class="hero">Keep this page</main></body></html>'
        )
        assets = {
            "https://cdn.example.test/site.css": (b'@import "./extra.css";.hero{background:url("./hero.png")}', "text/css"),
            "https://cdn.example.test/extra.css": (b'@font-face{src:url("./font.woff2")}', "text/css"),
            "https://cdn.example.test/hero.png": (b"fake-png-content", "image/png"),
            "https://cdn.example.test/font.woff2": (b"fake-font-content", "font/woff2"),
            "https://cdn.example.test/js/main.mjs": (
                b'import { boot } from "./dep.js";const lazy=import("./lazy.js");', "text/javascript"),
            "https://cdn.example.test/js/dep.js": (b"export const boot = true;", "text/javascript"),
            "https://cdn.example.test/js/lazy.js": (b"export const lazy = true;", "text/javascript"),
        }

        class FakeDriver:
            current_url = "https://cdn.example.test/template/index.html"
            def execute_script(self, _script):
                return html

        def fake_download(_driver, _session, url, _cache, _referer, _resources):
            return assets.get(url)

        with tempfile.TemporaryDirectory() as td:
            package = Path(td) / "package"
            with patch.object(self.wix, "scroll_full_page", lambda _driver: None), \
                 patch.object(self.wix, "_build_authenticated_session", lambda _driver: object()), \
                 patch.object(self.wix, "_browser_resource_map", lambda _driver: {}), \
                 patch.object(self.wix, "v4_download", fake_download):
                index_path, manifest = self.wix._v4_build_package(FakeDriver(), "https://cdn.example.test/template/index.html", package)

            output_html = Path(index_path).read_text(encoding="utf-8")
            downloaded = {row["url"] for row in manifest}
            for url in assets:
                self.assertIn(url, downloaded)
            self.assertNotIn("https://cdn.example.test/site.css", output_html)
            self.assertNotIn("https://cdn.example.test/js/main.mjs", output_html)
            self.assertNotIn("data-href=", output_html)
            local_js = package / next(row["local"] for row in manifest if row["url"].endswith("main.mjs"))
            js_text = local_js.read_text(encoding="utf-8")
            self.assertNotIn('from "./dep.js"', js_text)
            self.assertNotIn('import("./lazy.js")', js_text)
            self.assertTrue(Path(package, "asset-manifest.json").is_file())

    def test_relative_module_import_uses_module_url_as_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            (package / "assets" / "js").mkdir(parents=True)
            requested = []

            def fake_save(url, context, base_url):
                requested.append((url, base_url))
                dest = package / "assets/js/bundle_shared.js"
                dest.write_text("// shared", encoding="utf-8")
                return "assets/js/bundle_shared.js"

            self.wix.v4_rewrite_js_imports(
                'export { x } from "../shared/chunk.js";',
                "https://cdn.example.test/app/main.js",
                "assets/js/main.js",
                package,
                fake_save,
            )
            self.assertEqual(requested, [("https://cdn.example.test/shared/chunk.js", "https://cdn.example.test/app/main.js")])


if __name__ == "__main__":
    unittest.main()
