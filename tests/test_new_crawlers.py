"""Tests for the Envato Elements / Free Website Templates / WP Astra crawlers and crawler_core.
No network and no Chrome needed:  python -m unittest tests.test_new_crawlers   (from the crawler_studio folder)
"""
import csv
import os
import sys
import tempfile
import threading
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from bs4 import BeautifulSoup

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent / "crawlers"))

import crawler_core as core                     # noqa: E402
import envato_elements_crawler as envato        # noqa: E402
import freewebsitetemplates_crawler as fwt      # noqa: E402
import wpastra_crawler as astra                 # noqa: E402
import local_ai_cleanup as cleanup              # noqa: E402
from local_ai_cleanup import (                  # noqa: E402
    _apply_edits, _external_urls, _strip_known_tracking, discover_packages,
)

# ---------------------------------------------------------------- fixtures (shapes taken from the live pages)
FWT_PAGE1 = """<html><body><ol>
<li><a href="https://freewebsitetemplates.com/preview/rehabilitation-yoga/"><img alt="Rehabilitation Yoga Template" src="x.jpg"></a>
 <ul><li><a href="https://freewebsitetemplates.com/download/rehabilitation-yoga/">Download</a></li>
 <li><a href="https://freewebsitetemplates.com/preview/rehabilitation-yoga/">View</a></li></ul></li>
<li><a href="https://freewebsitetemplates.com/website/knollwalters/"><img alt="Law Firm"></a>
 <a href="https://freewebsitetemplates.com/website/knollwalters/">Get it on Wix for free</a></li>
<li><a href="https://freewebsitetemplates.com/preview/running/"><img alt="Running Website Template"></a></li>
</ol>
<a href="https://freewebsitetemplates.com/">1</a> <a href="https://freewebsitetemplates.com/templates/page-2">2</a>
<a href="https://freewebsitetemplates.com/templates/page-3">3</a> <a href="https://freewebsitetemplates.com/templates/page-27">27</a>
</body></html>"""
FWT_PAGE2 = """<ol><li><a href="/preview/space-science/" title="Space Science Template"><img></a></li>
<li><a href="/preview/running/"><img alt="Running Website Template"></a></li></ol>
<a href="/templates/page-27">27</a>"""

ASTRA_PAGE1 = """<div>Showing <strong>1</strong>–<strong>12</strong> of <strong>184</strong> templates</div>
<a href="https://websitedemos.net/brandstore-02?customize=template">Brandstore</a>
<h3>Brandstore</h3><a href="https://wpastra.com/templates/brandstore-02/">Details</a>
<a href="https://websitedemos.net/brandstore-02?customize=template">Live Preview</a>
<a href="https://websitedemos.net/web-agency-04?customize=template">Agency</a>
<a href="https://wpastra.com/templates/web-agency-04/">Details</a>
<a href="https://websitedemos.net/web-agency-04?customize=template">Live Preview</a>
<ul><li><a href="https://wpastra.com/website-templates/business/">1</a></li>
<li><a href="https://wpastra.com/website-templates/business/page/2/">2</a></li>
<li><a href="https://wpastra.com/website-templates/business/page/16/">16</a></li></ul>"""
ASTRA_PAGE2 = """<a href="https://websitedemos.net/mountain-02?customize=template">Mountain</a>
<a href="https://websitedemos.net/web-agency-04?customize=template">Agency</a>"""

ENVATO_LIST1 = """<a href="https://elements.envato.com/baosh-digital-agency-php-template-LA4VQP3" title="Baosh - Digital Agency PHP Template ">
<img alt="Preview: Baosh - Digital Agency PHP Template "></a>
<a href="https://elements.envato.com/baosh-digital-agency-php-template-LA4VQP3">Baosh - Digital Agency PHP Template</a>
<a href="https://elements.envato.com/user/Thememx/web-templates">Thememx</a>
<a href="https://elements.envato.com/arcado-nft-portfolio-html-template-HTZDSUJ"><img alt="Preview: Arcado - Nft Portfolio HTML Template "></a>
<a href="https://elements.envato.com/web-templates/portfolio">portfolio</a>
<a href="/web-templates?page=2">2</a>"""
ENVATO_LIST2 = """<a href="https://elements.envato.com/maze-agency-portfolio-html-template-GEZWRVL">Maze - Agency Portfolio HTML Template</a>
<a href="https://elements.envato.com/arcado-nft-portfolio-html-template-HTZDSUJ">Arcado</a>"""
ENVATO_ITEM = """<h1>Baosh - Digital Agency PHP Template</h1>
<a href="https://elements.envato.com/pricing">Subscribe to download</a>
<a href="https://thememxpro.com/php-template/baosh/index.php?storefront=envato-elements">Live preview</a>
<a href="https://elements.envato.com/web-templates/agency" title="Agency in Web Templates">agency</a>"""
ENVATO_ITEM_NO_PREVIEW = """<h1>Figma kit</h1><a href="https://elements.envato.com/pricing">Subscribe to download</a>"""


class FakeFetcher:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get(self, url, ok=None, scroll=False, force_browser=False):
        self.calls.append(url)
        if url not in self.pages:
            raise core.FetchError(f"no page {url}")
        return self.pages[url]


class UrlResilienceTests(unittest.TestCase):
    def test_query_pagination_preserves_filters_and_infers_gaps(self):
        html = """<nav><a href="/templates?criteria=painter&pageNumber=2">2</a>
        <a href="/templates?criteria=painter&pageNumber=4">4</a></nav>"""
        found = core.discover_pagination_targets(html, "https://example.com/templates?criteria=painter")
        self.assertEqual(found[2], "https://example.com/templates?criteria=painter&pageNumber=2")
        self.assertEqual(found[3], "https://example.com/templates?criteria=painter&pageNumber=3")
        self.assertEqual(found[4], "https://example.com/templates?criteria=painter&pageNumber=4")

    def test_query_pagination_replaces_old_page_key_and_reads_aria_label(self):
        html = """<nav><a href="/templates?criteria=painter&pageNumber=3" aria-label="Go to page 3">Go</a></nav>"""
        found = core.discover_pagination_targets(html, "https://example.com/templates?criteria=painter&page=1")
        self.assertEqual(found[3], "https://example.com/templates?criteria=painter&pageNumber=3")

    def test_path_pagination_supports_new_route_and_trailing_slash(self):
        html = """<nav><a href="/archive/page/2/">2</a><a href="/archive/page/4/">4</a></nav>"""
        found = core.discover_pagination_targets(html, "https://example.com/archive/")
        self.assertEqual(found[2], "https://example.com/archive/page/2/")
        self.assertEqual(found[3], "https://example.com/archive/page/3/")
        self.assertEqual(found[4], "https://example.com/archive/page/4/")

    def test_relative_and_protocol_relative_urls_are_normalized(self):
        self.assertEqual(core.normalize_http_url("../assets/app.js", "https://demo.example/a/page/"),
                         "https://demo.example/a/assets/app.js")
        self.assertEqual(core.normalize_http_url("//cdn.example/app.js", "https://demo.example/"),
                         "https://cdn.example/app.js")
        self.assertIsNone(core.normalize_http_url("data:image/png;base64,AAA", "https://demo.example/"))

    def test_srcset_parser_preserves_commas_inside_wix_transform_urls(self):
        value = (
            "https://static.wixstatic.com/media/abc123.jpg/v1/fill/"
            "w_228,h_335,al_c,q_90,enc_avif,quality_auto 228w, "
            "https://static.wixstatic.com/media/def456.jpg/v1/fill/"
            "w_456,h_700,al_c,q_85 456w"
        )
        found = core.split_srcset_candidates(value)
        self.assertEqual(len(found), 2)
        self.assertEqual(found[0][0], "https://static.wixstatic.com/media/abc123.jpg/v1/fill/w_228,h_335,al_c,q_90,enc_avif,quality_auto")
        self.assertEqual(found[0][1], "228w")
        self.assertEqual(found[1][0], "https://static.wixstatic.com/media/def456.jpg/v1/fill/w_456,h_700,al_c,q_85")
        self.assertEqual(found[1][1], "456w")

    def test_srcset_parser_supports_regular_and_descriptor_free_candidates(self):
        self.assertEqual(
            core.split_srcset_candidates("https://a.test/a.jpg 1x, https://b.test/b.jpg 2x"),
            [("https://a.test/a.jpg", "1x"), ("https://b.test/b.jpg", "2x")],
        )
        self.assertEqual(
            core.split_srcset_candidates("https://a.test/a.jpg, https://b.test/b.jpg"),
            [("https://a.test/a.jpg", ""), ("https://b.test/b.jpg", "")],
        )

    def test_browser_resource_match_handles_transformed_cdn_asset_paths(self):
        target = "https://static.example/media/c837a6_photo.jpg/v1/fill/w_337"
        loaded_near = "https://static.example/media/c837a6_photo.jpg/v1/fill/w_300"
        loaded_far = "https://static.example/media/c837a6_photo.jpg/v1/fill/w_900"
        resources = {
            loaded_near: {"request_id": "123", "mime_type": "image/jpeg", "status": 200},
            loaded_far: {"request_id": "456", "mime_type": "image/jpeg", "status": 200},
        }
        matched_url, info = core._find_browser_resource(target, resources)
        self.assertEqual(matched_url, loaded_near)
        self.assertEqual(info["status"], 200)


class FreeWebsiteTemplatesTests(unittest.TestCase):
    def setUp(self):
        fwt.DELAY_BETWEEN_PAGES_SEC = 0
        fwt.MAX_LISTING_PAGES = fwt.MAX_TEMPLATES = 0

    def test_classify(self):
        self.assertEqual(fwt.classify_url("https://freewebsitetemplates.com/")[0], "listing")
        self.assertEqual(fwt.classify_url("https://freewebsitetemplates.com/templates/page-5"), ("listing", 5))
        self.assertEqual(fwt.classify_url("https://freewebsitetemplates.com/preview/running/"), ("template", "running"))
        self.assertEqual(fwt.classify_url("https://freewebsitetemplates.com/download/running/"), ("template", "running"))
        self.assertEqual(fwt.classify_url("https://example.com/")[0], "unsupported")

    def test_cards_skip_wix_and_titles(self):
        cards = fwt.extract_cards(FWT_PAGE1)
        self.assertEqual([c["slug"] for c in cards], ["rehabilitation-yoga", "running"])   # Wix card skipped, no duplicates
        self.assertEqual(cards[0]["title"], "Rehabilitation Yoga Template")
        self.assertEqual(fwt.last_page_number(FWT_PAGE1), 27)

    def test_collect_all_pages(self):
        pages = {"https://freewebsitetemplates.com/": FWT_PAGE1}
        for n in range(2, 28):
            pages[f"https://freewebsitetemplates.com/templates/page-{n}"] = FWT_PAGE2
        f = FakeFetcher(pages)
        jobs, bad = fwt.collect_jobs([fwt.DEFAULT_URL], f)
        self.assertEqual(bad, [])
        self.assertEqual({j["slug"] for j in jobs}, {"rehabilitation-yoga", "running", "space-science"})   # de-duplicated
        self.assertEqual(len(f.calls), 27)          # page 1 is fetched once and reused; pages 2..27 once each
        self.assertEqual(jobs[0]["preview_url"], "https://freewebsitetemplates.com/preview/rehabilitation-yoga/")

    def test_single_page_and_limit(self):
        f = FakeFetcher({"https://freewebsitetemplates.com/templates/page-2": FWT_PAGE2})
        jobs, _ = fwt.collect_jobs(["https://freewebsitetemplates.com/templates/page-2"], f)
        self.assertEqual(len(jobs), 2)
        self.assertEqual(f.calls, ["https://freewebsitetemplates.com/templates/page-2"])
        fwt.MAX_TEMPLATES = 1
        pages = {"https://freewebsitetemplates.com/": FWT_PAGE1}
        jobs, _ = fwt.collect_jobs([fwt.DEFAULT_URL], FakeFetcher(pages))
        self.assertEqual(len(jobs), 1)


class AstraTests(unittest.TestCase):
    def setUp(self):
        astra.DELAY_BETWEEN_PAGES_SEC = 0
        astra.MAX_LISTING_PAGES = astra.MAX_TEMPLATES = 0

    def test_classify(self):
        self.assertEqual(astra.classify_url("https://wpastra.com/website-templates/business/"),
                         ("listing", ("https://wpastra.com/website-templates/business/", None)))
        self.assertEqual(astra.classify_url("https://wpastra.com/website-templates/business/page/3/"),
                         ("listing", ("https://wpastra.com/website-templates/business/", 3)))
        self.assertEqual(astra.classify_url("https://wpastra.com/website-templates/"),
                         ("listing", ("https://wpastra.com/website-templates/", None)))
        self.assertEqual(astra.classify_url("https://wpastra.com/templates/brandstore-02/"), ("template", "brandstore-02"))
        self.assertEqual(astra.classify_url("https://websitedemos.net/brandstore-02?customize=template"), ("template", "brandstore-02"))
        self.assertEqual(astra.classify_url("https://example.com/")[0], "unsupported")

    def test_cards(self):
        cards = astra.extract_cards(ASTRA_PAGE1)
        self.assertEqual([c["slug"] for c in cards], ["brandstore-02", "web-agency-04"])
        self.assertEqual(cards[0]["title"], "Brandstore")                       # not "Live Preview"
        self.assertEqual(cards[0]["preview_url"], "https://websitedemos.net/brandstore-02/")   # clean address, no ?customize
        self.assertEqual(cards[0]["template_url"], "https://wpastra.com/templates/brandstore-02/")
        self.assertEqual(astra.last_page_number(ASTRA_PAGE1), 16)
        self.assertEqual(astra.total_announced(ASTRA_PAGE1), 184)

    def test_collect_and_default(self):
        base = "https://wpastra.com/website-templates/business/"
        pages = {base: ASTRA_PAGE1}
        for n in range(2, 17):
            pages[f"{base}page/{n}/"] = ASTRA_PAGE2
        f = FakeFetcher(pages)
        jobs, bad = astra.collect_jobs([astra.DEFAULT_URL], f)
        self.assertEqual({j["slug"] for j in jobs}, {"brandstore-02", "web-agency-04", "mountain-02"})
        self.assertEqual(len(f.calls), 16)

    def test_direct_template_url(self):
        jobs, _ = astra.collect_jobs(["https://wpastra.com/templates/love-nature-02/"], FakeFetcher({}))
        self.assertEqual(jobs[0]["preview_url"], "https://websitedemos.net/love-nature-02/")


class EnvatoTests(unittest.TestCase):
    def setUp(self):
        envato.DELAY_BETWEEN_PAGES_SEC = 0
        envato.MAX_LISTING_PAGES = envato.MAX_TEMPLATES = 0

    def test_item_and_classify(self):
        self.assertEqual(envato.parse_item_url("https://elements.envato.com/baosh-digital-agency-php-template-LA4VQP3"),
                         ("baosh-digital-agency-php-template", "LA4VQP3"))
        self.assertIsNone(envato.parse_item_url("https://elements.envato.com/web-templates"))
        self.assertIsNone(envato.parse_item_url("https://elements.envato.com/web-templates/site-templates"))
        self.assertIsNone(envato.parse_item_url("https://elements.envato.com/user/Thememx/web-templates"))
        self.assertEqual(envato.classify_url("https://elements.envato.com/web-templates?page=3"),
                         ("listing", ("https://elements.envato.com/web-templates", 3)))
        self.assertEqual(envato.classify_url("https://example.com/x")[0], "unsupported")
        self.assertEqual(envato.listing_page_url("https://elements.envato.com/web-templates", 2),
                         "https://elements.envato.com/web-templates?page=2")

    def test_cards_only_items(self):
        cards = envato.extract_cards(ENVATO_LIST1)
        self.assertEqual([c["slug"] for c in cards], ["baosh-digital-agency-php-template-la4vqp3",
                                                      "arcado-nft-portfolio-html-template-htzdsuj"])
        self.assertEqual(cards[0]["title"], "Baosh - Digital Agency PHP Template")
        self.assertEqual(cards[1]["title"], "Arcado - Nft Portfolio HTML Template")

    def test_pagination_until_nothing_new(self):
        base = "https://elements.envato.com/web-templates"
        f = FakeFetcher({base: ENVATO_LIST1, base + "?page=2": ENVATO_LIST2, base + "?page=3": ENVATO_LIST2})
        jobs, bad = envato.collect_jobs([envato.DEFAULT_URL], f)
        self.assertEqual(len(jobs), 3)                       # page 3 only repeats page 2 -> stop
        self.assertEqual(f.calls, [base, base + "?page=2", base + "?page=3"])

    def test_page_limit(self):
        base = "https://elements.envato.com/web-templates"
        envato.MAX_LISTING_PAGES = 1
        f = FakeFetcher({base: ENVATO_LIST1, base + "?page=2": ENVATO_LIST2})
        jobs, _ = envato.collect_jobs([base], f)
        self.assertEqual(len(jobs), 2)
        self.assertEqual(f.calls, [base])

    def test_preview_extraction(self):
        info = envato.extract_preview(ENVATO_ITEM, "https://elements.envato.com/x-LA4VQP3")
        self.assertEqual(info["preview_url"], "https://thememxpro.com/php-template/baosh/index.php?storefront=envato-elements")
        self.assertEqual(info["title"], "Baosh - Digital Agency PHP Template")
        self.assertEqual(envato.extract_preview(ENVATO_ITEM_NO_PREVIEW)["preview_url"], "")

    def test_resolve_preview_no_preview(self):
        job = {"template_url": "https://elements.envato.com/figma-kit-ABCDEFG"}
        f = FakeFetcher({job["template_url"]: ENVATO_ITEM_NO_PREVIEW})
        with self.assertRaises(core.NoPreview):
            envato.resolve_preview(job, f)


class CrawlLoopTests(unittest.TestCase):
    """run_crawl with a fake browser and fake capture: statuses, skipping, no_preview memory, logging."""

    def setUp(self):
        core.DELAY_BETWEEN_TEMPLATES_SEC = 0
        self.captured = []

        class FakeDriver:
            def quit(s): pass

        def fake_capture(driver, preview_url, out_root, slug, meta):
            self.captured.append((slug, preview_url, meta["provider"]))
            if slug == "bad":
                raise RuntimeError("boom")
            d = Path(out_root) / slug
            d.mkdir(parents=True, exist_ok=True)
            (d / "metadata.json").write_text('{"status": "completed"}', encoding="utf-8")
            return {"status": "completed", "path": str(d), "assets": 3, "warnings": [], "title": slug}

        self._orig = (core.make_driver, core.driver_alive, core.capture_site)
        core.make_driver = lambda headless=True, light=False: FakeDriver()
        core.driver_alive = lambda d: True
        core.capture_site = fake_capture

    def tearDown(self):
        core.make_driver, core.driver_alive, core.capture_site = self._orig

    def _run(self, out, jobs):
        def collect(urls, fetcher):
            return jobs, []

        def resolve(job, fetcher):
            if job["slug"] == "nopreview":
                raise core.NoPreview("none")
            return {"preview_url": f"https://demo.example/{job['slug']}/", "title": job["slug"]}
        return core.run_crawl(provider="test", label="Test", default_url="https://x/", urls=[], output_dir=out,
                              collect_jobs=collect, resolve_preview=resolve)

    def test_flow(self):
        jobs = [{"slug": "good", "template_url": "u1"}, {"slug": "bad", "template_url": "u2"},
                {"slug": "nopreview", "template_url": "u3"}]
        with tempfile.TemporaryDirectory() as out:
            self.assertEqual(self._run(out, jobs), (1, 2, 0))
            self.assertEqual([c[0] for c in self.captured], ["good", "bad", "bad"])     # bad retried once, nopreview never captured
            self.assertEqual(self.captured[0][2], "test")                                # provider reaches metadata
            rows = list(csv.reader(open(os.path.join(out, "log.csv"), encoding="utf-8")))
            self.assertEqual({r[1]: r[5] for r in rows[1:]}, {"good": "completed", "bad": "failed", "nopreview": "no_preview"})
            self.captured.clear()
            self.assertEqual(self._run(out, jobs), (0, 1, 2))    # good done, nopreview remembered, only bad retried
            self.assertEqual([c[0] for c in self.captured], ["bad", "bad"])

    def test_no_templates_is_a_failure(self):
        with tempfile.TemporaryDirectory() as out:
            self.assertEqual(self._run(out, []), (0, 1, 0))


class CoreSmokeTest(unittest.TestCase):
    """The extracted capture engine still builds an offline package (local server, no Chrome)."""

    def test_package_builder(self):
        import requests
        with tempfile.TemporaryDirectory() as site, tempfile.TemporaryDirectory() as pkg:
            Path(site, "style.css").write_text("body{background:url(bg.png)}", encoding="utf-8")
            Path(site, "bg.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 50)
            Path(site, "app.js").write_text("console.log(1)", encoding="utf-8")
            html = ('<html><head><link rel="stylesheet" href="style.css"><script src="app.js"></script></head>'
                    '<body><img src="bg.png" data-original-src="bg.png" data-bg-image="bg.png" '
                    'data-lazy-srcset="bg.png 1x" style="background-image:url(bg.png)"><h1>Hello</h1>'
                    + "x" * 600 + '</body></html>')
            handler = partial(SimpleHTTPRequestHandler, directory=site)
            srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            old = core.ALLOW_PRIVATE_HOSTS
            core.ALLOW_PRIVATE_HOSTS = True
            try:
                base = f"http://127.0.0.1:{srv.server_address[1]}/index.html"

                def fetch(url, referer):
                    r = requests.get(url, timeout=10)
                    return (r.content, r.headers.get("Content-Type", "")) if r.ok else None
                b = core.PackageBuilder(base, pkg, fetch, log=lambda *a: None)
                n = b.build(html)
                self.assertGreaterEqual(n, 3)
                v = core.validate_package(Path(pkg))
                self.assertEqual(v["status"], "completed", v)
                index = Path(pkg, "index.html")
                self.assertTrue(index.exists())
                saved = BeautifulSoup(index.read_text(encoding="utf-8"), "html.parser").find("img")
                self.assertTrue(saved["src"].startswith("assets/images/"))
                self.assertTrue(saved["data-original-src"].startswith("assets/images/"))
                self.assertTrue(saved["data-bg-image"].startswith("assets/images/"))
                self.assertIn("assets/images/", saved["data-lazy-srcset"])
                self.assertIn("assets/images/", saved["style"])
            finally:
                core.ALLOW_PRIVATE_HOSTS = old
                srv.shutdown()

    def test_tracking_resources_removed_but_functional_iframes_are_reported(self):
        with tempfile.TemporaryDirectory() as pkg:
            def fetch(url, referer):
                return None
            html = (
                '<html><head><script src="https://www.googletagmanager.com/gtag/js?id=x"></script>'
                '</head><body><iframe src="https://www.youtube.com/embed/demo"></iframe>'
                '<p>Template</p></body></html>'
            )
            builder = core.PackageBuilder("https://demo.example/", pkg, fetch, log=lambda *a: None)
            builder.build(html)
            index_text = Path(pkg, "index.html").read_text(encoding="utf-8")
            self.assertNotIn("googletagmanager.com", index_text)
            self.assertIn("youtube.com/embed/demo", index_text)
            self.assertTrue(any("googletagmanager.com" in x for x in builder.removed_trackers))
            result = core.validate_package(pkg)
            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["iframes"], ["https://www.youtube.com/embed/demo"])

    def test_html_challenge_is_not_saved_as_javascript(self):
        with tempfile.TemporaryDirectory() as pkg:
            def fetch(url, referer):
                return b"<!doctype html><html><body>Access denied" + b"x" * 5000 + b"</body></html>", "text/html"
            builder = core.PackageBuilder("https://demo.example/", pkg, fetch, log=lambda *a: None)
            builder.build("<html><head><script src='/assets/app.js'></script></head><body>" + "x" * 600 + "</body></html>")
            self.assertEqual(list(Path(pkg, "assets", "js").glob("*")), [])
            self.assertTrue(any("HTML/challenge page" in item["reason"] for item in builder.failed))


if __name__ == "__main__":
    unittest.main()


class LocalAICleanupTests(unittest.TestCase):
    def test_removes_tracker_tags_without_reserializing_other_markup(self):
        html = ('<!doctype html><html><head>'
                '<script src="https://www.googletagmanager.com/gtag/js?id=G-DEMO"></script>'
                '<link rel="preconnect" href="https://www.googletagmanager.com">'
                '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter">'
                '</head><body><main><p class="x">Keep this page</p>'
                '<iframe src="https://www.youtube.com/embed/demo"></iframe>'
                '</main></body></html>')
        cleaned, removed = _strip_known_tracking(html)
        self.assertEqual(len(removed), 2)
        self.assertNotIn("googletagmanager.com", cleaned)
        self.assertIn('href="https://fonts.googleapis.com/css2?family=Inter"', cleaned)
        self.assertIn('src="https://www.youtube.com/embed/demo"', cleaned)
        self.assertIn('<p class="x">Keep this page</p>', cleaned)

    def test_only_applies_exact_unique_edits(self):
        source = '<html><body><script src="https://www.googletagmanager.com/gtag.js"></script><p>Useful website text here</p></body></html>'
        find = '<script src="https://www.googletagmanager.com/gtag.js"></script>'
        cleaned, applied, rejected = _apply_edits(source, [{"find": find, "replace": "", "reason": "tracker"}], True)
        self.assertNotIn("googletagmanager", cleaned)
        self.assertEqual(len(applied), 1)
        self.assertEqual(rejected, [])

        repeated = source + find
        unchanged, applied, rejected = _apply_edits(repeated, [{"find": find, "replace": "", "reason": "tracker"}], True)
        self.assertEqual(unchanged, repeated)
        self.assertEqual(len(applied), 0)
        self.assertEqual(len(rejected), 1)

    def test_rejects_edit_that_erases_visible_content(self):
        original_text = "This is a substantial visible page with navigation, services and contact information."
        source = f"<html><body><main><p>{original_text}</p></main></body></html>"
        cleaned, applied, rejected = _apply_edits(source, [{
            "find": f"<main><p>{original_text}</p></main>", "replace": "", "reason": "bad removal"
        }], True)
        self.assertEqual(cleaned, source)
        self.assertEqual(applied, [])
        self.assertTrue(rejected)

    def test_does_not_allow_new_external_hosts(self):
        source = '<html><body><p>Useful page content stays here.</p><script>var a=1;</script></body></html>'
        cleaned, applied, rejected = _apply_edits(source, [{
            "find": '<script>var a=1;</script>',
            "replace": '<script src="https://new-unexpected.example/app.js"></script>',
            "reason": "should not be allowed"
        }], True)
        self.assertEqual(cleaned, source)
        self.assertFalse(applied)
        self.assertTrue(rejected)

    def test_external_url_inventory(self):
        urls = _external_urls('x="https://cdn.example/a.css" href="https://fonts.example/a.css"')
        self.assertEqual(urls, ["https://cdn.example/a.css", "https://fonts.example/a.css"])

    def test_process_creates_separate_copy_and_preserves_source(self):
        class FakeResponse:
            def raise_for_status(self):
                return None
            def json(self):
                return {"choices": [{"message": {"content": '{"edits": [], "notes": [], "dependencies_to_review": []}'}}]}

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp, "raw", "template-a")
            source.mkdir(parents=True)
            original_html = ('<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1">'
                             '<script src="https://www.googletagmanager.com/gtag/js?id=x"></script>'
                             '</head><body><main><p>A visible website section with enough content.</p></main></body></html>')
            (source / "index.html").write_text(original_html, encoding="utf-8")
            (source / "metadata.json").write_text('{"title":"Test"}', encoding="utf-8")
            destination = Path(tmp, "cleaned", "template-a")
            old_get_models = cleanup.get_models
            old_post = cleanup.requests.post
            try:
                cleanup.get_models = lambda base_url=cleanup.BASE_URL: [{"id": "fake-local-model"}]
                cleanup.requests.post = lambda *a, **k: FakeResponse()
                result = cleanup.process_template(source, destination, model="fake-local-model")
            finally:
                cleanup.get_models = old_get_models
                cleanup.requests.post = old_post
            self.assertTrue((destination / "index.html").is_file())
            self.assertTrue((destination / "cleanup-report.json").is_file())
            self.assertTrue((destination / "standalone-validation.txt").is_file())
            self.assertIn("googletagmanager.com", original_html)  # raw source untouched
            self.assertNotIn("googletagmanager.com", (destination / "index.html").read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "cleaned")

    def test_external_urls_in_css_and_js_are_selected_without_a_file_count_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            (package / "index.html").write_text("<html></html>", encoding="utf-8")
            (package / "one.css").write_text(".hero{background:url(https://cdn.example/hero.jpg)}", encoding="utf-8")
            (package / "two.js").write_text('const api="https://api.example/v1";', encoding="utf-8")
            (package / "three.js").write_text('const x="https://other.example/file";', encoding="utf-8")
            selected = {p.name for p in cleanup._code_files(package)}
            self.assertEqual(selected, {"index.html", "one.css", "two.js", "three.js"})

    def test_large_files_are_reviewed_in_bounded_segments(self):
        first = " https://external.example/first.js "
        second = " https://external.example/second.js "
        text = ("a" * 10000) + first + ("b" * 70000) + second + ("c" * 70000)
        segments = cleanup._review_segments(text)
        self.assertGreater(len(segments), 1)
        self.assertTrue(all(0 < len(part) <= cleanup.MAX_FILE_CHARS for part in segments))
        self.assertTrue(any(first.strip() in part for part in segments))
        self.assertTrue(any(second.strip() in part for part in segments))

    def test_discovers_completed_template_packages(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp, "template-a")
            package.mkdir()
            (package / "index.html").write_text("<html></html>", encoding="utf-8")
            (package / "metadata.json").write_text("{}", encoding="utf-8")
            assets = package / "assets" / "css"
            assets.mkdir(parents=True)
            (assets / "site.css").write_text("body{}", encoding="utf-8")
            found = discover_packages(tmp)
            self.assertEqual(found, [package])

