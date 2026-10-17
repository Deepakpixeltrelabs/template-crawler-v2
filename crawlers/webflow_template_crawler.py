"""
Webflow template crawler  (provider crawler for Website Crawler Studio)

What it does
------------
Takes a Webflow URL typed on the main page and saves the template as an
offline-usable package (index.html + assets/ folders):

  * https://webflow.com/templates/html/<slug>   -> one template. The crawler reads
        the template page, finds its "Live preview" site (https://<name>.webflow.io/),
        opens that site in Chrome and saves it.
  * https://webflow.com/templates/category/...  -> a listing page. Every
        /templates/html/<slug> card found on that page is crawled the same way.
        (Only the page you give is read - pagination is NOT followed; it has not
        been verified for Webflow listing pages.)
  * https://<name>.webflow.io/                  -> a live template site, captured directly.

Output (inside the output folder chosen by the app)
---------------------------------------------------
  <slug>/index.html, assets/{css,js,images,fonts,media,other}/, asset-manifest.json,
         metadata.json, offline-validation.txt
  log.csv                 one row per template attempt
  _incomplete/<slug>/     a capture whose validation FAILED (never treated as done)

Safety rules built in
---------------------
  * Each template is built in a temp folder, validated, then moved into place.
  * A package already marked "completed" is never overwritten - it is skipped.
  * An interrupted run leaves only a temp folder, which is deleted on the next run.
  * Status is "completed" only if every local file exists and nothing still points
    at a remote stylesheet/script/image/font. Otherwise "partial" (with the list).

Entry point used by runner.py:  crawl(urls, output_dir)
Run alone:  python webflow_template_crawler.py <url> [<url> ...] [--out DIR]
"""
import csv
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ------------------------------------------------------------------ CONFIG
MAX_TEMPLATES_PER_LISTING = 0         # 0 = crawl ALL templates a listing shows. Set a number (e.g. 100) to cap one run
TEMPLATE_ATTEMPTS = 2                 # tries per template before it is marked failed
DELAY_BETWEEN_TEMPLATES_SEC = 3
PAGE_LOAD_TIMEOUT = 60
WINDOW_SIZE = (1440, 900)
BROWSER_OFFLINE_CHECK = True          # re-open the saved file in Chrome with network blocked
MAX_ASSET_BYTES = 80 * 1024 * 1024
FETCH_TRIES = 3                       # direct-download attempts per asset
DEFAULT_URL = "https://webflow.com/templates/all"   # used when no URL is given (empty box in the app, or run directly)
RETRY_NO_PREVIEW = os.environ.get("STUDIO_FORCE_RETRY_NO_PREVIEW", "0") == "1"              # True = try templates again that earlier had no live preview
MAX_SCROLL_ROUNDS = 2000                # safety limit only; scrolling stops earlier once no new cards load
ALLOW_PRIVATE_HOSTS = False           # tests only: allow fetching from 127.0.0.1 etc.

TRACKING_HINTS = (
    "google-analytics", "googletagmanager", "doubleclick", "facebook.net",
    "facebook.com/tr", "hotjar", "clarity.ms", "segment.io", "segment.com",
    "mixpanel", "sentry.io", "plausible.io", "fullstory", "hs-scripts",
    "hsforms", "linkedin.com/px", "snap.licdn", "tiktok.com/i18n/pixel",
)
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# -------------------------------------------------------- URL classification
TEMPLATE_URL_RE = re.compile(
    r"^https?://(?:www\.)?webflow\.com/templates/html/([a-z0-9][a-z0-9\-]*)/?$", re.I)
SITE_URL_RE = re.compile(r"^https?://([a-z0-9][a-z0-9\-]*)\.webflow\.io(?:/.*)?$", re.I)
LISTING_URL_RE = re.compile(r"^https?://(?:www\.)?webflow\.com/templates(?:/.*)?$", re.I)


def classify_url(url):
    """Prefer known Webflow routes, then infer template vs listing from path semantics."""
    clean = url.split("#", 1)[0].split("?", 1)[0].strip()
    parsed = urlparse(clean)
    host = (parsed.hostname or "").lower()
    m = TEMPLATE_URL_RE.match(clean)
    if m:
        return "template", m.group(1).lower()
    m = SITE_URL_RE.match(clean)
    if m:
        return "site", m.group(1).lower()
    if host in ("webflow.com", "www.webflow.com"):
        path = parsed.path.strip("/")
        parts = [x for x in path.split("/") if x]
        if not path or path.lower() in ("templates", "templates/all") or any(
                x.lower() in ("all", "category", "categories", "tag", "tags", "search", "collection", "collections", "marketplace") for x in parts):
            return "listing", None
        if parts and parts[0].lower() == "templates":
            ignored = {"all", "html", "category", "categories", "search", "tags"}
            candidate = parts[-1]
            if candidate.lower() not in ignored and re.fullmatch(r"[a-z0-9][a-z0-9-]*", candidate, re.I):
                return "template", candidate.lower()
            return "listing", None
        return "unsupported", None
    if LISTING_URL_RE.match(clean):
        return "listing", None
    return "unsupported", None


# ------------------------------------------------- template / listing parsing
def extract_template_info(html, page_url=""):
    """Read a webflow.com/templates/html/<slug> page. Returns a dict."""
    soup = BeautifulSoup(html, "html.parser")
    info = {"title": "", "description": "", "preview_url": None, "categories": []}

    # title / description from JSON-LD (WebPage node), else <title>
    for j in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(j.string or "")
        except Exception:
            continue
        for node in (data.get("@graph") or [data]) if isinstance(data, dict) else []:
            if isinstance(node, dict) and node.get("@type") == "WebPage" and node.get("name"):
                info["title"] = info["title"] or node["name"]
                info["description"] = info["description"] or node.get("description", "")
    if not info["title"] and soup.title and soup.title.string:
        info["title"] = soup.title.string.strip()

    # live-preview URL: 1) the preview button  2) any <name>.webflow.io in page data
    #                   3) derive from preview.webflow.com/preview/<name>
    for a in soup.select("a.wfdt-button[href]"):
        if SITE_URL_RE.match(urljoin(page_url or "https://webflow.com/", a["href"])):
            info["preview_url"] = urljoin(page_url or "https://webflow.com/", a["href"])
            break
    if not info["preview_url"]:
        for a in soup.find_all("a", href=True):
            label = (a.get_text(" ", strip=True) + " " + str(a.get("aria-label", ""))).lower()
            candidate_url = urljoin(page_url or "https://webflow.com/", a["href"])
            if any(word in label for word in ("live preview", "preview site", "view site", "visit site")) and SITE_URL_RE.match(candidate_url):
                info["preview_url"] = candidate_url
                break
    if not info["preview_url"]:
        m = re.search(r"https://([a-z0-9][a-z0-9\-]*)\.webflow\.io/", html)
        if m:
            info["preview_url"] = m.group(0)
    if not info["preview_url"]:
        m = re.search(r"https://preview\.webflow\.com/preview/([a-z0-9][a-z0-9\-]*)", html)
        if m:
            info["preview_url"] = f"https://{m.group(1)}.webflow.io/"

    # provider categories = breadcrumb category links (slugs, e.g. real-estate-websites)
    seen = []
    for a in soup.select(".wfdt-breadcrumb-categories a[href]"):
        m = re.search(r"/templates/category/([a-z0-9\-]+)", a["href"])
        if m and m.group(1) not in seen:
            seen.append(m.group(1))
    info["categories"] = seen
    return info


def webflow_template_slug(url):
    """Infer a template slug from a real Webflow template URL without rebuilding its route."""
    parsed = urlparse(url)
    if (parsed.hostname or "").lower() not in ("webflow.com", "www.webflow.com"):
        return ""
    parts = [x for x in parsed.path.strip("/").split("/") if x]
    if not parts or parts[0].lower() != "templates":
        return ""
    ignored = {"all", "html", "category", "categories", "search", "tags", "collection", "collections", "marketplace"}
    if any(x.lower() in ignored for x in parts[1:-1]):
        return ""
    candidate = parts[-1].lower()
    if candidate in ignored or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", candidate):
        return ""
    return candidate


def extract_listing_templates(html, base="https://webflow.com/"):
    """Template cards on a listing page -> [(slug, exact URL)] in page order, de-duplicated."""
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()

    def add(href):
        full = urljoin(base, href).split("#", 1)[0]
        parsed = urlparse(full)
        candidate = webflow_template_slug(full)
        if candidate and candidate not in seen:
            seen.add(candidate)
            out.append((candidate, full))

    cards = soup.select("a.tmcard-link[href]") or soup.select("a[href]")
    for a in cards:
        add(a["href"])
    for j in soup.find_all("script", type="application/ld+json"):   # ItemList fallback
        try:
            data = json.loads(j.string or "")
        except Exception:
            continue
        if isinstance(data, dict) and data.get("@type") == "ItemList":
            for it in data.get("itemListElement", []):
                if isinstance(it, dict) and it.get("url"):
                    add(it["url"])
    return out


# ------------------------------------------------------------ asset helpers
MIME_EXT = {
    "text/css": ".css", "text/javascript": ".js", "application/javascript": ".js",
    "application/x-javascript": ".js", "image/jpeg": ".jpg", "image/png": ".png",
    "image/gif": ".gif", "image/webp": ".webp", "image/avif": ".avif",
    "image/svg+xml": ".svg", "image/x-icon": ".ico", "image/vnd.microsoft.icon": ".ico",
    "font/woff": ".woff", "font/woff2": ".woff2", "font/ttf": ".ttf", "font/otf": ".otf",
    "application/font-woff": ".woff", "application/font-woff2": ".woff2",
    "application/vnd.ms-fontobject": ".eot", "video/mp4": ".mp4", "video/webm": ".webm",
    "audio/mpeg": ".mp3", "application/json": ".json",
}
EXT_MIME = {v: k for k, v in MIME_EXT.items()}
EXT_MIME.update({".jpeg": "image/jpeg", ".mjs": "text/javascript"})


def is_tracking(url):
    low = url.lower()
    return any(h in low for h in TRACKING_HINTS)


def guess_mime(url, content_type=""):
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    if ct and ct not in ("application/octet-stream", "binary/octet-stream", "text/plain"):
        return ct
    return EXT_MIME.get(Path(urlparse(url).path).suffix.lower(), ct or "application/octet-stream")


def asset_category(url, mime="", hint=None):
    path = urlparse(url).path.lower()
    mime = (mime or "").lower()
    if hint == "css" or mime == "text/css" or path.endswith(".css"):
        return "css"
    if hint == "js" or "javascript" in mime or path.endswith((".js", ".mjs")):
        return "js"
    if mime.startswith("font/") or "font" in mime or path.endswith((".woff", ".woff2", ".ttf", ".otf", ".eot")):
        return "fonts"
    if mime.startswith("image/") or path.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".svg", ".ico")):
        return "images"
    if mime.startswith(("video/", "audio/")) or path.endswith((".mp4", ".webm", ".mp3", ".ogg", ".mov")):
        return "media"
    return "other"


def safe_filename(url, mime, category):
    raw = unquote(Path(urlparse(url).path).name)
    raw = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._") or category.rstrip("s") or "asset"
    stem, ext = Path(raw).stem[:40] or "asset", Path(raw).suffix.lower()
    if not ext:
        ext = MIME_EXT.get((mime or "").split(";", 1)[0].lower(), "")
    return f"{stem}_{hashlib.sha1(url.encode()).hexdigest()[:10]}{ext}"


CSS_REF_RE = re.compile(
    r"""(?P<imp>@import\s+)?url\(\s*(?P<q>['"]?)(?P<u>[^)]*?)(?P=q)\s*\)"""
    r"""|@import\s+(?P<q2>['"])(?P<u2>.*?)(?P=q2)""", re.I | re.S)
SKIP_PREFIXES = ("data:", "blob:", "#", "javascript:", "mailto:", "tel:", "about:")

_host_cache = {}


def host_is_public(host):
    if ALLOW_PRIVATE_HOSTS:
        return True
    if host in _host_cache:
        return _host_cache[host]
    ok = True
    try:
        for fam, _, _, _, sa in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(sa[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                ok = False
    except Exception:
        ok = False
    _host_cache[host] = ok
    return ok


# ------------------------------------------------------------ package builder
class PackageBuilder:
    """Turns rendered HTML into index.html + assets/. Browser-free, so it can be
    tested on its own: `fetch(url, referer) -> (bytes, mime) | None` is injected."""

    def __init__(self, page_url, package_dir, fetch, log=print):
        self.page_url = page_url
        self.root = Path(package_dir)
        self.fetch = fetch
        self.log = log
        self.url2local = {}
        self.manifest = []
        self.failed = []            # [{"url", "reason"}]
        self.removed_trackers = []
        self.js_remote_hosts = set()
        self.used_names = set()
        for c in ("css", "js", "images", "fonts", "media", "other"):
            (self.root / "assets" / c).mkdir(parents=True, exist_ok=True)

    # -- core: fetch + store one asset, return absolute Path or None
    def save_asset(self, raw_url, base_url, hint=None, referer=None):
        raw_url = (raw_url or "").strip().strip("'\"")
        if not raw_url or raw_url.lower().startswith(SKIP_PREFIXES):
            return None
        if raw_url.startswith("//"):
            raw_url = "https:" + raw_url
        absolute = urljoin(base_url, raw_url).split("#", 1)[0]
        if not absolute.lower().startswith(("http://", "https://")):
            return None
        if is_tracking(absolute):
            self.removed_trackers.append(absolute)
            return None
        if absolute in self.url2local:
            return self.url2local[absolute]
        if absolute in {f["url"] for f in self.failed}:
            return None

        result = self.fetch(absolute, referer or base_url)
        if not result:
            self.failed.append({"url": absolute, "reason": "download failed"})
            self.log(f"    asset failed: {absolute}")
            return None
        raw, mime = result
        if len(raw) > MAX_ASSET_BYTES:
            self.failed.append({"url": absolute, "reason": f"larger than {MAX_ASSET_BYTES // 1048576} MB"})
            return None
        mime = guess_mime(absolute, mime)
        category = asset_category(absolute, mime, hint)
        folder = self.root / "assets" / category
        name = safe_filename(absolute, mime, category)
        n, stem, ext = 2, Path(name).stem, Path(name).suffix
        while name in self.used_names or (folder / name).exists():
            name = f"{stem}_{n}{ext}"
            n += 1
        self.used_names.add(name)
        dest = folder / name
        self.url2local[absolute] = dest            # register first: stops @import loops

        if category == "css":
            text = raw.decode("utf-8", errors="replace")
            text = self.rewrite_css(text, absolute, dest.parent)
            dest.write_text(text, encoding="utf-8")
            size = len(text.encode("utf-8"))
        else:
            dest.write_bytes(raw)
            size = len(raw)
            if category == "js":
                for h in re.findall(r"https?://([a-z0-9.\-]+)", raw.decode("utf-8", errors="ignore"), re.I):
                    if not h.endswith(("w3.org", "schema.org", "webflow.com", "example.com")):
                        self.js_remote_hosts.add(h.lower())
        self.manifest.append({"url": absolute, "local": dest.relative_to(self.root).as_posix(),
                              "category": category, "mime": mime, "bytes": size})
        return dest

    def local_ref(self, raw_url, base_url, from_dir, hint=None):
        """Download asset and return the path to write into the file located in from_dir."""
        frag = ""
        if "#" in raw_url:
            raw_url, frag = raw_url.split("#", 1)
            frag = "#" + frag
        if not raw_url.strip() or raw_url.strip().lower().startswith(SKIP_PREFIXES):
            return None
        dest = self.save_asset(raw_url, base_url, hint)
        if not dest:
            # Download failed: point at the ORIGINAL absolute URL so the gap is visible and
            # honest (reported as a remote reference) instead of leaving a dead relative path.
            absolute = urljoin(base_url, "https:" + raw_url if raw_url.startswith("//") else raw_url)
            if absolute.lower().startswith(("http://", "https://")) and not is_tracking(absolute):
                return absolute + frag
            return None
        return os.path.relpath(dest, start=from_dir).replace("\\", "/") + frag

    # -- CSS text: rewrite url(...) and @import, paths relative to `from_dir`
    def rewrite_css(self, css, css_url, from_dir):
        def repl(m):
            if m.group("u2") is not None:
                raw, is_import, quote = m.group("u2"), True, m.group("q2")
            else:
                raw, is_import, quote = (m.group("u") or "").strip(), bool(m.group("imp")), m.group("q") or ""
            if not raw or raw.lower().startswith(SKIP_PREFIXES):
                return m.group(0)
            local = self.local_ref(raw, css_url, from_dir, "css" if is_import else None)
            if not local:
                return m.group(0)
            if m.group("u2") is not None:
                return f"@import {quote}{local}{quote}"
            return f"{m.group('imp') or ''}url({quote}{local}{quote})"
        return CSS_REF_RE.sub(repl, css)

    def _srcset(self, value, base):
        parts = []
        for cand in re.split(r",\s+|,(?=https?://)", value.strip()):
            bits = cand.strip().split()
            if not bits:
                continue
            local = self.local_ref(bits[0], base, self.root)
            if local:
                bits[0] = local
            parts.append(" ".join(bits))
        return ", ".join(parts)

    # -- whole page
    def build(self, html):
        soup = BeautifulSoup(html, "html.parser")
        base = self.page_url
        base_tag = soup.find("base", href=True)
        if base_tag:
            base = urljoin(self.page_url, base_tag["href"])

        # <link>: stylesheets and icons are saved; hints (preload etc.) are dropped
        for link in soup.find_all("link"):
            href = link.get("href")
            if not href:
                continue
            rel = [x.lower() for x in (link.get("rel") or [])]
            if any(r in rel for r in ("preload", "prefetch", "modulepreload", "dns-prefetch", "preconnect", "prerender")):
                link.decompose()
                continue
            if "stylesheet" in rel:
                local = self.local_ref(href, base, self.root, "css")
                if local:
                    link["href"] = local
            elif "icon" in rel or "apple-touch-icon" in rel:
                local = self.local_ref(href, base, self.root)
                if local:
                    link["href"] = local
            for a in ("integrity", "crossorigin"):          # would block local copies
                link.attrs.pop(a, None)

        # <script src>
        for sc in soup.find_all("script", src=True):
            src = sc["src"]
            if is_tracking(urljoin(base, src)):
                self.removed_trackers.append(src)
                sc.decompose()
                continue
            local = self.local_ref(src, base, self.root, "js")
            if local:
                sc["src"] = local
            for a in ("integrity", "crossorigin"):
                sc.attrs.pop(a, None)

        # everything else
        single = ("src", "data-src", "data-lazy-src", "data-original", "data-original-src",
                  "data-image-url", "data-background-image", "data-bg-image", "data-fallback-src",
                  "data-bg", "data-background", "data-bg-src", "data-lazy-background",
                  "data-poster", "poster", "data-poster-url", "data-lazyload", "data")
        multi = ("srcset", "data-srcset", "data-lazy-srcset", "imagesrcset", "data-imagesrcset")
        for tag in soup.find_all(True):
            if getattr(tag, "decomposed", False) or tag.name in ("script", "link", "base", "iframe"):
                continue
            if tag.name == "img" and tag.get("src") and is_tracking(urljoin(base, tag["src"])):
                self.removed_trackers.append(tag["src"])
                tag.decompose()
                continue
            for attr in single:
                if tag.has_attr(attr) and (attr != "data" or tag.name == "object"):
                    local = self.local_ref(tag[attr], base, self.root)
                    if local:
                        tag[attr] = local
            for attr in multi:
                if tag.has_attr(attr):
                    tag[attr] = self._srcset(tag[attr], base)
            if tag.has_attr("data-video-urls"):          # Webflow background video
                vids = [self.local_ref(v, base, self.root) or v for v in tag["data-video-urls"].split(",") if v.strip()]
                tag["data-video-urls"] = ",".join(vids)
            if tag.name in ("image", "use"):             # svg <image href> / <use href>
                for attr in ("href", "xlink:href"):
                    v = tag.get(attr)
                    if v and not v.startswith("#"):
                        local = self.local_ref(v, base, self.root)
                        if local:
                            tag[attr] = local
            if tag.has_attr("style"):
                tag["style"] = self.rewrite_css(tag["style"], base, self.root)
            if tag.has_attr("integrity"):
                del tag["integrity"]

        for st in soup.find_all("style"):
            css = st.string if st.string is not None else st.get_text()
            st.clear()
            st.append(self.rewrite_css(css or "", base, self.root))

        for b in soup.find_all("base"):
            b.decompose()
        if soup.head is None and soup.html:
            soup.html.insert(0, soup.new_tag("head"))
        if soup.head:
            if not soup.head.find("meta", attrs={"charset": True}):
                m = soup.new_tag("meta")
                m["charset"] = "utf-8"
                soup.head.insert(0, m)
            if not soup.head.find("meta", attrs={"name": "viewport"}):
                m = soup.new_tag("meta")
                m["name"] = "viewport"
                m["content"] = "width=device-width, initial-scale=1"
                soup.head.append(m)

        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        (self.root / "index.html").write_text(
            f"<!DOCTYPE html>\n<!-- Saved from {self.page_url} on {stamp} by Website Crawler Studio -->\n"
            + str(soup), encoding="utf-8")
        (self.root / "asset-manifest.json").write_text(json.dumps({
            "source_url": self.page_url, "asset_count": len(self.manifest),
            "assets": self.manifest, "failed": self.failed,
            "removed_trackers": sorted(set(self.removed_trackers))},
            indent=2, ensure_ascii=False), encoding="utf-8")
        return len(self.manifest)


# ---------------------------------------------------------------- validation
def validate_package(package_dir):
    """Static check of the saved files. Returns dict(status, missing_local, remote_refs)."""
    pkg = Path(package_dir)
    index = pkg / "index.html"
    res = {"missing_local": [], "remote_refs": [], "iframes": []}
    if not index.exists():
        res["missing_local"].append("index.html")
        res["status"] = "failed"
        return res
    soup = BeautifulSoup(index.read_text(encoding="utf-8", errors="replace"), "html.parser")

    def check(kind, ref, base_dir):
        ref = (ref or "").strip()
        if not ref or ref.lower().startswith(("data:", "blob:", "#", "javascript:", "about:")):
            return
        if ref.startswith(("http:", "https:", "//")):
            res["remote_refs"].append({"kind": kind, "url": ref})
        elif not (base_dir / unquote(ref.split("#", 1)[0].split("?", 1)[0])).exists():
            res["missing_local"].append(f"{kind}: {ref}")

    def check_css(css, base_dir, where):
        for m in CSS_REF_RE.finditer(css):
            ref = m.group("u2") if m.group("u2") is not None else (m.group("u") or "")
            check(f"css url ({where})", ref, base_dir)

    for tag in soup.find_all(True):
        n = tag.name
        if n == "iframe" and tag.get("src", "").startswith(("http", "//")):
            res["iframes"].append(tag["src"])
            continue
        if n == "link":
            rel = [x.lower() for x in (tag.get("rel") or [])]
            if "stylesheet" in rel or "icon" in rel:
                check(f"link[{'/'.join(rel)}]", tag.get("href"), pkg)
            continue
        for attr in ("src", "poster", "data-src", "data-lazy-src", "data-original",
                     "data-image-url", "data-poster-url"):
            if tag.has_attr(attr):
                check(f"{n}[{attr}]", tag[attr], pkg)
        for attr in ("srcset", "data-srcset"):
            if tag.has_attr(attr):
                for cand in re.split(r",\s+|,(?=https?://)", tag[attr]):
                    bits = cand.strip().split()
                    if bits:
                        check(f"{n}[{attr}]", bits[0], pkg)
        if tag.has_attr("data-video-urls"):
            for v in tag["data-video-urls"].split(","):
                check(f"{n}[data-video-urls]", v, pkg)
        if tag.has_attr("style"):
            check_css(tag["style"], pkg, "inline style")
    for st in soup.find_all("style"):
        check_css(st.get_text(), pkg, "<style>")
    for css in (pkg / "assets" / "css").glob("*.css"):
        check_css(css.read_text(encoding="utf-8", errors="replace"), css.parent, css.name)

    if res["missing_local"]:
        res["status"] = "failed"          # our own rewriting pointed at a file that is not there
    elif res["remote_refs"]:
        res["status"] = "partial"
    else:
        res["status"] = "completed"
    return res


# --------------------------------------------------------------- browser part
class BrowserUnavailable(Exception):
    pass


class NoPreview(Exception):
    """The template page has no live-preview site (e.g. it only offers 'Preview in Designer'). Retrying cannot help."""


def _chrome_options(perf_log=True, headless=True, light=False):
    from selenium.webdriver.chrome.options import Options
    o = Options()
    if headless:
        o.add_argument("--headless=new")
    o.add_argument(f"--window-size={WINDOW_SIZE[0]},{WINDOW_SIZE[1]}")
    o.add_argument("--disable-gpu")
    o.add_argument("--no-sandbox")
    o.add_argument("--disable-dev-shm-usage")
    o.add_argument("--disable-background-networking")
    o.add_argument("--disable-blink-features=AutomationControlled")
    if light:      # listing-only browser: no images, so thousands of cards stay cheap
        o.add_experimental_option("prefs", {"profile.managed_default_content_settings.images": 2})
        perf_log = False
    if perf_log:
        o.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    return o


def _find_cached_chromedriver():
    """A chromedriver.exe already on this PC (from webdriver-manager or Selenium Manager), newest first."""
    import glob
    home = os.path.expanduser("~")
    pats = [os.path.join(home, ".wdm", "drivers", "chromedriver", "**", "chromedriver*"),
            os.path.join(home, ".cache", "selenium", "chromedriver", "**", "chromedriver*")]
    found = []
    for pat in pats:
        for f in glob.glob(pat, recursive=True):
            if os.path.isfile(f) and os.path.basename(f).lower() in ("chromedriver.exe", "chromedriver"):
                found.append(f)
    return sorted(found, key=os.path.getmtime, reverse=True)


def make_driver(headless=True, light=False):
    """Start Chrome. Tries, in order: webdriver-manager, a chromedriver already on this PC,
    then Selenium's built-in driver manager."""
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    errors = []

    def first_line(e):
        return (str(e).strip().splitlines() or [repr(e)])[0][:200]

    driver = None
    # 1) webdriver-manager first: it is the method that already works on this PC (Wix crawler)
    try:
        wdm = os.path.join(os.path.expanduser("~"), ".wdm")
        for name in (os.listdir(wdm) if os.path.isdir(wdm) else []):
            if name.startswith(".wdm-lock"):          # stale lock from a killed run would stall ~60 s
                try:
                    os.remove(os.path.join(wdm, name))
                except OSError:
                    pass
        from webdriver_manager.chrome import ChromeDriverManager
        driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=_chrome_options(headless=headless, light=light))
    except Exception as e:
        errors.append(f"webdriver-manager: {first_line(e)}")
        print("  note: webdriver-manager failed - trying a chromedriver already on this PC", flush=True)
    # 2) a chromedriver already downloaded on this PC
    if driver is None:
        for path in _find_cached_chromedriver()[:3]:
            try:
                print(f"  note: trying {path}", flush=True)
                driver = webdriver.Chrome(service=Service(path), options=_chrome_options(headless=headless, light=light))
                break
            except Exception as e:
                errors.append(f"cached driver: {first_line(e)}")
    # 3) Selenium's built-in manager last (it can spend minutes retrying a blocked download)
    if driver is None:
        print("  note: trying Selenium's built-in driver manager (can be slow)", flush=True)
        try:
            driver = webdriver.Chrome(options=_chrome_options(headless=headless, light=light))
        except Exception as e:
            errors.append(f"Selenium Manager: {first_line(e)}")
    if driver is None:
        raise BrowserUnavailable(" || ".join(errors) or "no driver found")
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
    try:    # present Chrome's real user agent (without "Headless") so the site treats it like a normal browser
        ua = driver.execute_script("return navigator.userAgent").replace("HeadlessChrome", "Chrome")
        driver.execute_cdp_cmd("Network.setUserAgentOverride", {"userAgent": ua})
    except Exception:
        pass
    if light:
        return driver
    try:
        driver.execute_cdp_cmd("Network.enable", {"maxTotalBufferSize": 100 * 1024 * 1024,
                                                  "maxResourceBufferSize": 20 * 1024 * 1024})
    except Exception as e:
        print(f"  note: could not enable network body capture ({e}); will download assets directly")
    return driver


def driver_alive(driver):
    try:
        driver.title
        return True
    except Exception:
        return False


def scroll_full_page(driver):
    driver.execute_script("""
        return new Promise((resolve) => {
            let y = 0, steps = 0;
            const t = setInterval(() => {
                window.scrollBy(0, 400); y += 400; steps++;
                if (y >= document.documentElement.scrollHeight || steps > 80) {
                    clearInterval(t); window.scrollTo(0, 0); resolve();
                }
            }, 150);
        });""")
    time.sleep(1.0)


def _resource_map(driver):
    """url -> {request_id, mime_type} for everything Chrome actually loaded."""
    out = {}
    try:
        logs = driver.get_log("performance")
    except Exception:
        return out
    for e in logs:
        try:
            msg = json.loads(e["message"])["message"]
            if msg.get("method") != "Network.responseReceived":
                continue
            p = msg["params"]
            url = p["response"]["url"].split("#", 1)[0]
            out[url] = {"request_id": p["requestId"], "mime_type": p["response"].get("mimeType", "")}
        except Exception:
            continue
    return out


class BrowserFetcher:
    """Prefer the exact bytes Chrome received; fall back to requests with the page's cookies."""

    def __init__(self, driver):
        self.driver = driver
        self.resources = _resource_map(driver)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})
        try:
            for c in driver.get_cookies():
                self.session.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))
        except Exception:
            pass

    def __call__(self, url, referer):
        info = self.resources.get(url)
        if info:
            try:
                r = self.driver.execute_cdp_cmd("Network.getResponseBody", {"requestId": info["request_id"]})
                import base64
                body = base64.b64decode(r["body"]) if r.get("base64Encoded") else r.get("body", "").encode("utf-8")
                return body, info.get("mime_type", "")
            except Exception:
                pass
        return http_fetch(self.session, url, referer)


def http_fetch(session, url, referer, tries=None):
    tries = tries or FETCH_TRIES
    host = urlparse(url).hostname or ""
    if not host_is_public(host):
        return None
    for i in range(tries):
        try:
            r = session.get(url, headers={"Referer": referer}, timeout=30)
            r.raise_for_status()
            return r.content, r.headers.get("Content-Type", "")
        except Exception:
            time.sleep(0.5 * (i + 1))
    return None


def browser_offline_check(index_path):
    """Open the saved file with all web traffic blocked. Reports files that failed to
    load, broken local images and remote requests that were attempted."""
    from selenium import webdriver
    res = {"ran": False, "local_failed": [], "broken_images": [], "remote_attempts": [], "error": ""}
    d = None
    try:
        d = webdriver.Chrome(options=_chrome_options())
        d.set_page_load_timeout(30)
        d.execute_cdp_cmd("Network.enable", {})
        d.execute_cdp_cmd("Network.setBlockedURLs",
                          {"urls": ["http://*/*", "https://*/*", "ws://*/*", "wss://*/*"]})
        try:
            d.get(Path(index_path).resolve().as_uri())
        except Exception:
            pass
        time.sleep(2)
        try:
            scroll_full_page(d)
        except Exception:
            pass
        req, failed = {}, set()
        for e in d.get_log("performance"):
            try:
                m = json.loads(e["message"])["message"]
            except Exception:
                continue
            p = m.get("params", {})
            if m.get("method") == "Network.requestWillBeSent":
                req[p["requestId"]] = p["request"]["url"]
            elif m.get("method") == "Network.loadingFailed":
                failed.add(p["requestId"])
        res["local_failed"] = sorted({req[i] for i in failed if req.get(i, "").startswith("file:")})
        res["remote_attempts"] = sorted({u for u in req.values() if u.startswith(("http://", "https://", "ws://", "wss://"))})
        res["broken_images"] = d.execute_script("""
            return Array.from(document.images).filter(i => {
              const s = i.currentSrc || i.src || '';
              return s.startsWith('file:') && !s.toLowerCase().includes('.svg') && i.complete && i.naturalWidth === 0;
            }).map(i => i.currentSrc || i.src);""") or []
        res["ran"] = True
    except Exception as e:
        res["error"] = str(e).strip().splitlines()[0] if str(e).strip() else repr(e)
    finally:
        if d:
            try:
                d.quit()
            except Exception:
                pass
    return res


# ------------------------------------------------------------ crawl workflow
def step(msg):
    print(f"  [step] {msg}", flush=True)


def wait_ready(driver, seconds=30):
    """Bounded adaptive wait for DOM and image/resource activity to settle."""
    from selenium.webdriver.support.ui import WebDriverWait
    try:
        WebDriverWait(driver, seconds).until(lambda d: d.execute_script("return document.readyState") in ("interactive", "complete"))
    except Exception:
        pass
    start = time.monotonic(); quiet_since = start; last = None
    minimum = float(os.environ.get("STUDIO_ASSET_SETTLE_MIN", "0.8"))
    maximum = float(os.environ.get("STUDIO_ASSET_SETTLE_MAX", "6.0"))
    quiet_need = float(os.environ.get("STUDIO_ASSET_SETTLE_QUIET", "0.7"))
    while time.monotonic() - start < max(minimum, maximum):
        try:
            x = driver.execute_script("const h=document.documentElement; return {p:[...document.images].filter(i=>!i.complete).length,h:h?h.scrollHeight:0,n:document.querySelectorAll('*').length,r:performance.getEntriesByType('resource').length}")
            sig = (x.get('p'),x.get('h'),x.get('n'),x.get('r'))
            if sig != last: last=sig; quiet_since=time.monotonic()
            if time.monotonic()-start >= minimum and time.monotonic()-quiet_since >= quiet_need and x.get('p',0)==0: break
        except Exception:
            if time.monotonic()-start >= minimum: break
        time.sleep(0.2)


def retry_fs(fn, *args, tries=6):
    """Windows antivirus/indexers briefly lock freshly written folders; retry a few times."""
    for i in range(tries):
        try:
            return fn(*args)
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.7 * (i + 1))


def read_package_status(final_dir):
    try:
        return json.loads((Path(final_dir) / "metadata.json").read_text(encoding="utf-8")).get("status")
    except Exception:
        return None


def capture_site(driver, preview_url, out_root, slug, meta_extra):
    """Open the live site, build the package in a temp folder, validate, promote. Returns dict."""
    out_root = Path(out_root)
    tmp_root = out_root / ".tmp"
    tmp = tmp_root / f"{slug}-{int(time.time())}"
    final = out_root / slug
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        step(f"Opening live preview: {preview_url}")
        try:
            driver.get(preview_url)
        except Exception as e:
            if "timeout" not in str(e).lower():
                raise
            print("    page load timed out - continuing with what has rendered")
        wait_ready(driver)
        current = driver.current_url or preview_url
        html = driver.execute_script("return document.documentElement.outerHTML;")
        if not html or len(html) < 500:
            raise RuntimeError("preview page rendered empty")
        step("Scrolling the page so lazy images and animations load")
        scroll_full_page(driver)
        wait_ready(driver, seconds=8)
        driver_title = driver.title
        html = driver.execute_script("return document.documentElement.outerHTML;")

        step("Downloading and rewriting assets (CSS, JS, images, fonts)")
        fetcher = BrowserFetcher(driver)
        builder = PackageBuilder(current, tmp, fetcher, log=print)
        asset_count = builder.build(html)

        step("Validating the saved package")
        v = validate_package(tmp)
        warnings = []
        if builder.failed:
            warnings.append(f"{len(builder.failed)} asset download(s) failed")
        if v["remote_refs"]:
            warnings.append(f"{len(v['remote_refs'])} reference(s) still point at remote URLs")
        if v["iframes"]:
            warnings.append(f"{len(set(v['iframes']))} embedded iframe(s) load remote content")
        if builder.js_remote_hosts:
            warnings.append("JavaScript files mention remote hosts (not rewritten): "
                            + ", ".join(sorted(builder.js_remote_hosts)[:8]))
        if builder.removed_trackers:
            warnings.append(f"{len(set(builder.removed_trackers))} tracking script/pixel(s) removed")

        offline = None
        status = v["status"]
        if BROWSER_OFFLINE_CHECK and status != "failed":
            step("Re-opening the saved file in Chrome with the network blocked")
            offline = browser_offline_check(tmp / "index.html")
            if offline["ran"]:
                if offline["local_failed"] or offline["broken_images"]:
                    status = "partial"
                    warnings.append(f"offline browser test: {len(offline['local_failed'])} local file(s) failed to load, "
                                    f"{len(offline['broken_images'])} broken image(s)")
                if offline["remote_attempts"]:
                    warnings.append(f"offline browser test: page tried {len(offline['remote_attempts'])} remote request(s) (blocked)")
            else:
                warnings.append(f"offline browser test did not run: {offline['error']}")

        lines = [status.upper()]
        lines += [f"missing local file: {x}" for x in v["missing_local"]]
        lines += [f"remote reference: {x['kind']} -> {x['url']}" for x in v["remote_refs"]]
        lines += [f"iframe (remote): {x}" for x in v["iframes"]]
        lines += [f"download failed: {f['url']} ({f['reason']})" for f in builder.failed]
        if offline:
            lines += [f"offline test local failure: {x}" for x in offline["local_failed"]]
            lines += [f"offline test broken image: {x}" for x in offline["broken_images"]]
            lines += [f"offline test remote attempt: {x}" for x in offline["remote_attempts"]]
        lines += warnings
        (tmp / "offline-validation.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

        meta = {
            "provider": "webflow", "template_id": slug, "status": status,
            "title": meta_extra.get("title") or driver_title, "description": meta_extra.get("description", ""),
            "template_url": meta_extra.get("template_url", ""), "preview_url": preview_url,
            "final_url": current, "listing_url": meta_extra.get("listing_url", ""),
            "provider_categories": meta_extra.get("categories", []),
            "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "asset_count": asset_count, "warnings": warnings,
            "failed_assets": builder.failed,
            "browser_offline_check": offline["ran"] if offline else False,
        }
        (tmp / "metadata.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

        if status == "failed":                      # keep for inspection, never as a finished package
            bad = out_root / "_incomplete" / slug
            if bad.exists():
                retry_fs(shutil.rmtree, bad)
            bad.parent.mkdir(parents=True, exist_ok=True)
            retry_fs(os.replace, tmp, bad)
            return {"status": "failed", "path": str(bad), "assets": asset_count, "warnings": warnings,
                    "error": "validation failed: " + "; ".join(v["missing_local"][:3])}
        if final.exists():                           # only non-completed packages reach here
            retry_fs(shutil.rmtree, final)
        retry_fs(os.replace, tmp, final)
        return {"status": status, "path": str(final), "assets": asset_count, "warnings": warnings, "error": "",
                "title": meta["title"]}
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)


def append_log(out_root, row):
    path = Path(out_root) / "log.csv"
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "template_id", "title", "template_url", "preview_url", "status", "path", "assets", "warnings", "error"])
        w.writerow(row)


CARD_CSS = "a.tmcard-link, a[href*='/templates/html/']"


def load_all_cards(driver, limit, found, target=0):
    """Infinite-scroll listing. Scrolls to the bottom again and again and records every template link it sees
    into `found` ({slug: url}) after each round, so even if Chrome later crashes nothing collected is lost.

    Stops when: `target` (the total the page announces) is reached, `limit` is reached, no new cards appeared
    for 3 rounds in a row, or MAX_SCROLL_ROUNDS is hit. Returns the number of templates collected."""
    GROW_WAIT = 12

    def dom_count():
        try:
            return int(driver.execute_script(f"return document.querySelectorAll(\"{CARD_CSS}\").length;"))
        except Exception:
            return -1

    def harvest():
        try:
            page_html = driver.page_source
            page_url = driver.current_url or url
            discovered = extract_listing_templates(page_html, page_url)
        except Exception:
            return False
        for slug, real_url in discovered:
            found.setdefault(slug, real_url)
        return True

    harvest()
    last_dom, stale, rounds = dom_count(), 0, 0
    print(f"  scrolling to load more templates (start: {len(found)} found)", flush=True)
    while rounds < MAX_SCROLL_ROUNDS and stale < 3:
        if (limit and len(found) >= limit) or (target and len(found) >= target):
            break
        rounds += 1
        try:
            driver.execute_script(
                f"const c=document.querySelectorAll(\"{CARD_CSS}\");"
                "if(c.length){c[c.length-1].scrollIntoView({block:'end'});}"
                "window.scrollTo(0, document.documentElement.scrollHeight);"
                "window.dispatchEvent(new Event('scroll'));")
            driver.execute_script(
                "for (const b of document.querySelectorAll('button, a, div[role=button]')) {"
                " const t=(b.innerText||'').trim().toLowerCase();"
                " if (t==='load more' || t==='show more' || t==='view more' || t==='load more templates') { b.click(); break; } }")
        except Exception as e:
            print(f"  scroll problem: {(str(e).strip().splitlines() or [''])[0][:100]}", flush=True)
            break
        grew, waited = False, 0.0
        while waited < GROW_WAIT:
            time.sleep(1.0)
            waited += 1.0
            now = dom_count()
            if now > last_dom:
                last_dom, grew = now, True
                break
        if grew:
            stale = 0
            if not harvest():
                print("  Chrome stopped answering - keeping what was collected so far", flush=True)
                break
            if rounds % 10 == 0:
                print(f"  ...{len(found)} templates found", flush=True)
        else:
            stale += 1
            try:                                   # nudge: small scroll up then down fires the loader again
                driver.execute_script("window.scrollBy(0,-600);")
                time.sleep(0.5)
                driver.execute_script("window.scrollTo(0, document.documentElement.scrollHeight);")
            except Exception:
                pass
    harvest()
    print(f"  collected {len(found)} template link(s) from the page", flush=True)
    return len(found)


DEBUG_DIR = None


def _is_all_templates(url):
    return urlparse(url.split("?", 1)[0]).path.rstrip("/").lower() == "/templates/all"


def sitemap_templates():
    """Template URLs listed in Webflow's XML sitemap(s) -> [(slug, url)]. [] if unavailable."""
    import xml.etree.ElementTree as ET

    def first(e):
        return (str(e).strip().splitlines() or [repr(e)])[0][:120]

    def locs(u):
        r = requests.get(u, headers={"User-Agent": USER_AGENT}, timeout=(8, 40))
        r.raise_for_status()
        root = ET.fromstring(r.content)
        return [e.text.strip() for e in root.iter() if e.tag.split("}")[-1] == "loc" and e.text]

    try:
        top = locs("https://webflow.com/sitemap.xml")
    except Exception as e:
        print(f"  sitemap not available ({first(e)})", flush=True)
        return []
    out, seen, queue, done = [], set(), [], 0

    def take(urls):
        nonlocal queue
        for u in urls:
            if u.lower().split("?")[0].endswith(".xml"):
                queue.append(u)
                continue
            clean = u.split("#", 1)[0]
            slug = webflow_template_slug(clean)
            if slug and slug not in seen:
                seen.add(slug)
                out.append((slug, clean))

    take(top)
    queue.sort(key=lambda u: 0 if "template" in u.lower() else 1)     # template sitemaps first
    while queue and done < 60:
        u = queue.pop(0)
        done += 1
        try:
            take(locs(u))
        except Exception as e:
            print(f"  sitemap file skipped ({first(e)})", flush=True)
    print(f"  sitemap lists {len(out)} template page(s)", flush=True)
    return out


def dump_debug(driver, url, tag):
    """Save what Chrome actually saw, so an empty listing can be diagnosed."""
    try:
        d = Path(DEBUG_DIR) if DEBUG_DIR else Path(".")
        d.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%H%M%S")
        html = driver.page_source
        (d / f"listing_{tag}_{stamp}.html").write_text(html, encoding="utf-8", errors="replace")
        try:
            driver.save_screenshot(str(d / f"listing_{tag}_{stamp}.png"))
        except Exception:
            pass
        n_a = len(re.findall(r"<a\s", html))
        n_t = len(re.findall(r"/templates/html/", html))
        print(f"  [debug] Chrome saw: title={driver.title!r}, {n_a} links, {n_t} template-link text matches", flush=True)
        print(f"  [debug] saved what Chrome saw to: {d}  (listing_{tag}_{stamp}.html / .png)", flush=True)
    except Exception as e:
        print(f"  [debug] could not save debug files: {e}", flush=True)


def read_listing_in_browser(driver, url):
    """Open a listing page, wait for the card grid, scroll to load everything.
    -> (found [(slug, url)], cards_loaded, total_announced_by_page)"""
    for attempt in (1, 2, 3):              # a slow/hung page must not abort the whole crawl
        try:
            driver.get(url)
            break
        except Exception as e:
            print(f"  listing load problem (try {attempt}/3): {(str(e).strip().splitlines() or [''])[0][:120]}", flush=True)
    try:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        WebDriverWait(driver, 45).until(EC.presence_of_element_located((By.CSS_SELECTOR, "a.tmcard-link")))
    except Exception:
        print("  the template grid has not appeared yet (waited 45 s)", flush=True)
    total = 0
    try:     # the page announces its size, e.g. "11,646 templates"
        txt = driver.execute_script("const m=(document.body.innerText||'').match(/([\\d,]{2,})\\s+templates/i); return m?m[1]:'';")
        total = int(str(txt).replace(",", "")) if txt else 0
    except Exception:
        pass
    if total:
        print(f"  the page says it has {total} templates", flush=True)
    collected = {}
    load_all_cards(driver, MAX_TEMPLATES_PER_LISTING, collected, total)
    if not collected:                       # grid never rendered: fall back to whatever links the page source has
        try:
            return extract_listing_templates(driver.page_source, url), 0, total
        except Exception:
            return [], 0, total
    return list(collected.items()), len(collected), total


def collect_jobs(driver, urls):
    """Resolve user URLs by URL semantics first, then by rendered page content.

    Webflow occasionally changes listing/detail route shapes. For an unrecognised
    URL on its own domain, inspect the page instead of rejecting it based only on
    its path. Visible template cards mean listing; an explicit live-preview link
    means detail; otherwise use the listing discovery path as the safe fallback.
    """
    jobs, seen, bad = [], set(), []
    for url in urls:
        kind, ident = classify_url(url)
        if kind == "unsupported":
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()
            if host in ("webflow.com", "www.webflow.com"):
                page_html = ""
                try:
                    try:
                        driver.get(url)
                    except Exception as e:
                        if "timeout" not in str(e).lower():
                            raise
                    wait_ready(driver, 18)
                    page_html = driver.page_source or ""
                except Exception as e:
                    print(f"  URL-shape fallback could not read {url}: {str(e).splitlines()[0][:120]}", flush=True)
                found_cards = extract_listing_templates(page_html, url) if page_html else []
                if found_cards:
                    kind = "listing"
                    print(f"  URL-shape fallback: found {len(found_cards)} template card(s); treating URL as a listing", flush=True)
                else:
                    detail = extract_template_info(page_html, url) if page_html else {}
                    if detail.get("preview_url"):
                        ident = webflow_template_slug(url)
                        if not ident:
                            title = detail.get("title") or parsed.path.rsplit("/", 1)[-1] or "template"
                            ident = re.sub(r"[^a-z0-9-]+", "-", title.lower()).strip("-")[:42]
                            ident = (ident or "template") + "-" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
                        kind = "template"
                        print("  URL-shape fallback: found a live-preview link; treating URL as a template page", flush=True)
                    else:
                        # Unknown routes on the provider's own host can be JS-loaded
                        # listings, so let the real listing reader scroll and inspect
                        # them before declaring them invalid.
                        kind = "listing"
                        print("  URL-shape fallback: checking the rendered page as a possible listing", flush=True)
            else:
                bad.append(url)
                continue
        if kind == "template":
            if ident not in seen:
                seen.add(ident)
                jobs.append({"slug": ident, "template_url": url, "listing_url": ""})
        elif kind == "site":
            if ident not in seen:
                seen.add(ident)
                jobs.append({"slug": ident, "template_url": "", "preview_url": url, "listing_url": ""})
        else:
            step(f"Reading listing page: {url}")
            sm = []
            if _is_all_templates(url):
                step("Checking Webflow's sitemap (a quick but possibly partial list)")
                sm = sitemap_templates()
            found, total, ld = list(sm), 0, None
            try:
                ld = make_driver(light=True)             # separate light browser just for reading the listing
                br, loaded, total = read_listing_in_browser(ld, url)
                if loaded <= 0:
                    dump_debug(ld, url, "headless")
                    print("  the template grid did not appear in headless Chrome - retrying with a visible Chrome window", flush=True)
                    try:
                        ld.quit()
                    except Exception:
                        pass
                    ld = make_driver(headless=False, light=True)
                    br, loaded, total = read_listing_in_browser(ld, url)
                    if loaded <= 0:
                        dump_debug(ld, url, "visible")
                have = {sl for sl, _ in found}
                found += [(sl, u) for sl, u in br if sl not in have]
                print(f"  sitemap: {len(sm)} | browser: {len(br)} | combined: {len(found)}", flush=True)
            except Exception as e:
                print(f"  browser listing failed: {(str(e).strip().splitlines() or [repr(e)])[0][:150]}", flush=True)
            finally:
                if ld is not None:
                    try:
                        ld.quit()
                    except Exception:
                        pass
            if total and len(found) < total * 0.98:
                print(f"  WARNING: found {len(found)} of the {total} templates the page announces - the list is incomplete", flush=True)
            print(f"  listing has {len(found)} template link(s)")
            if MAX_TEMPLATES_PER_LISTING and len(found) > MAX_TEMPLATES_PER_LISTING:
                print(f"  limiting to the first {MAX_TEMPLATES_PER_LISTING} (MAX_TEMPLATES_PER_LISTING)")
                found = found[:MAX_TEMPLATES_PER_LISTING]
            for slug, turl in found:
                if slug not in seen:
                    seen.add(slug)
                    jobs.append({"slug": slug, "template_url": turl, "listing_url": url})
    return jobs, bad


def trusted_preview_url(html):
    """Preview-site address from the plain page HTML, only from places where Webflow states it explicitly:
    the 'preview' button, or the page's previewIframeUrl data. None if not sure (the browser path then decides)."""
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.select("a.wfdt-button[href]"):
        if SITE_URL_RE.match(a["href"]):
            return a["href"]
    m = re.search(r'previewIframeUrl(?:&quot;|")\s*:\s*\{(?:&quot;|")href(?:&quot;|")\s*:\s*(?:&quot;|")(https://[a-z0-9\-]+\.webflow\.io/)', html)
    return m.group(1) if m else None


def process_job(driver, job, out_root):
    meta = {"template_url": job.get("template_url", ""), "listing_url": job.get("listing_url", ""),
            "title": "", "description": "", "categories": []}
    preview = job.get("preview_url")
    if not preview:
        info = None
        try:   # fast path: the preview link is often in the plain page HTML, which loads in seconds
            r = requests.get(job["template_url"], headers={"User-Agent": USER_AGENT}, timeout=(8, 25))
            if r.status_code == 200:
                sure = trusted_preview_url(r.text)
                if sure:
                    info = extract_template_info(r.text, job["template_url"])
                    info["preview_url"] = sure
                    step("Read the live-preview link from the page HTML (fast path)")
        except Exception:
            pass
        if info is None:
            step(f"Opening template page: {job['template_url']}")
            try:
                driver.get(job["template_url"])
            except Exception as e:
                if "timeout" not in str(e).lower():
                    raise
            wait_ready(driver, 20)
            try:
                from selenium.webdriver.common.by import By
                from selenium.webdriver.support.ui import WebDriverWait
                from selenium.webdriver.support import expected_conditions as EC
                WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.CSS_SELECTOR, "a.wfdt-button")))
            except Exception:
                pass
            info = extract_template_info(driver.page_source, job["template_url"])
        meta.update(title=info["title"], description=info["description"], categories=info["categories"])
        preview = info["preview_url"]
        if not preview:
            raise NoPreview("no live-preview (.webflow.io) link on the template page (it may only offer Preview in Designer)")
        step(f"Found live preview site: {preview}")
    return capture_site(driver, preview, out_root, job["slug"], meta)


def crawl(urls, output_dir):
    """Returns (saved, failed, skipped). Raises SystemExit(1) if Chrome cannot start."""
    out_root = Path(output_dir)
    out_root.mkdir(parents=True, exist_ok=True)
    global DEBUG_DIR
    DEBUG_DIR = out_root / "_debug"
    shutil.rmtree(out_root / ".tmp", ignore_errors=True)     # leftovers of an interrupted run

    if not urls:
        urls = [DEFAULT_URL]
        print(f"[webflow] no URL given - using the default: {DEFAULT_URL}")
    print(f"[webflow] {len(urls)} URL(s) to read; saving into {out_root}")
    step("Starting Chrome")
    try:
        driver = make_driver()
    except BrowserUnavailable as e:
        print(f"ERROR: Chrome could not be started - nothing was crawled. {e}")
        print("       Check: Google Chrome is installed, the internet is reachable (a VPN/proxy/antivirus can block the driver download),")
        print("       and run:  python -m pip install -U selenium webdriver-manager")
        raise SystemExit(1)

    saved = failed = skipped = 0
    force_refresh = os.environ.get("STUDIO_FORCE_REFRESH", "0") == "1"
    no_preview_done = set()
    try:
        with open(out_root / "log.csv", newline="", encoding="utf-8") as lf:
            for row in csv.reader(lf):
                if len(row) > 5 and row[5] == "no_preview":
                    no_preview_done.add(row[1])
    except OSError:
        pass
    try:
        jobs, bad = collect_jobs(driver, urls)
        for u in bad:
            failed += 1
            print(f"  FAILED: {u} -> not a Webflow template, listing or .webflow.io URL")
        retry_ids_raw = os.environ.get("STUDIO_RETRY_TEMPLATE_IDS", "").strip()
        if retry_ids_raw:
            try:
                retry_ids = set(json.loads(retry_ids_raw))
                before = len(jobs)
                jobs = [j for j in jobs if j.get("slug") in retry_ids]
                print(f"  recovery filter: selected {len(jobs)} of {before} discovered template(s)")
            except (ValueError, TypeError):
                print("  WARNING: invalid recovery filter ignored")
        print(f"  found {len(jobs)} unique templates to crawl")

        for job in jobs:
            final = out_root / job["slug"]
            tid = job.get("template_url") or job.get("preview_url") or job["slug"]
            print(f"\n=== Template: {job['slug']} ===")
            if job["slug"] in no_preview_done and not RETRY_NO_PREVIEW and not force_refresh:
                skipped += 1
                print("  skip (no live preview - found earlier, see log.csv)")
                continue
            if read_package_status(final) == "completed" and not force_refresh:
                skipped += 1
                print(f"  skip (already done): {tid}")
                continue

            result, last_err, no_preview = None, "", False
            for attempt in range(1, TEMPLATE_ATTEMPTS + 1):
                try:
                    if not driver_alive(driver):
                        step("Chrome stopped responding - restarting it")
                        try:
                            driver.quit()
                        except Exception:
                            pass
                        driver = make_driver()
                    result = process_job(driver, job, out_root)
                    break
                except BrowserUnavailable as e:
                    print(f"ERROR: Chrome could not be restarted - stopping. {e}")
                    raise SystemExit(1)
                except NoPreview as e:
                    last_err = str(e)
                    no_preview = True
                    break                       # retrying cannot create a preview
                except Exception as e:
                    last_err = (str(e).strip().splitlines() or [repr(e)])[0][:300]
                    print(f"  attempt {attempt}/{TEMPLATE_ATTEMPTS} failed: {last_err}")
                    if attempt < TEMPLATE_ATTEMPTS:
                        time.sleep(DELAY_BETWEEN_TEMPLATES_SEC)

            now = datetime.now().isoformat(timespec="seconds")
            if result and result["status"] in ("completed", "partial"):
                saved += 1
                label = "OK" if result["status"] == "completed" else "PARTIAL"
                print(f"  saved: {result['path']} | assets: {result['assets']} | validation: {label}")
                for w in result["warnings"]:
                    print(f"  WARNING: {w}")
                append_log(out_root, [now, job["slug"], result.get("title", ""), job.get("template_url", ""), "", result["status"],
                                      result["path"], result["assets"], " | ".join(result["warnings"]), ""])
            else:
                failed += 1
                err = (result or {}).get("error") or last_err or "unknown error"
                print(f"  FAILED: {tid} -> {err}")
                append_log(out_root, [now, job["slug"], "", job.get("template_url", ""), "", "no_preview" if no_preview else "failed",
                                      (result or {}).get("path", ""), "", "", err])
            time.sleep(DELAY_BETWEEN_TEMPLATES_SEC)
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    print(f"\nDone. saved: {saved}, failed: {failed}, skipped (already done): {skipped}. Output: {out_root}")
    return saved, failed, skipped


if __name__ == "__main__":
    args = sys.argv[1:]
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webflow_output")
    if "--out" in args:
        i = args.index("--out")
        out = args[i + 1]
        del args[i:i + 2]
    s, f, k = crawl(args, out)
    sys.exit(1 if f and not (s or k) else 0)
