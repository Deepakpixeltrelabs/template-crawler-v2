"""
crawler_core.py  -  shared engine for the Envato Elements, Free Website Templates and WP Astra crawlers
(provider crawlers for Website Crawler Studio)

You normally never run or edit this file. Each provider crawler (envato_elements_crawler.py,
freewebsitetemplates_crawler.py, wpastra_crawler.py) only knows two site-specific things:

    1. how to find the templates on a listing page            (collect_jobs)
    2. how to find a template's live-preview address          (resolve_preview)

Everything else is done here, with the same proven code the Webflow crawler uses:

    * open the live preview in Chrome, scroll it so lazy images/animations load
    * download every CSS / JS / image / font / media file and rewrite the page to use local copies
    * validate the package, then re-open it in Chrome with the network blocked as an offline test
    * build in a temp folder and move into place only when finished; never overwrite a finished package
    * log.csv with one row per attempt; statuses completed / partial / failed / no_preview

Output (inside the output folder chosen by the app), per template:
    <slug>/index.html, assets/{css,js,images,fonts,media,other}/, asset-manifest.json,
           metadata.json, offline-validation.txt
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
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

# ------------------------------------------------------------------ CONFIG
TEMPLATE_ATTEMPTS = 2                 # tries per template before it is marked failed
DELAY_BETWEEN_TEMPLATES_SEC = 3
PAGE_LOAD_TIMEOUT = 60
# Adaptive settle policy: fast pages proceed quickly; busy pages get more time.
ASSET_SETTLE_MIN_SEC = float(os.environ.get("STUDIO_ASSET_SETTLE_MIN", "0.8"))
ASSET_SETTLE_MAX_SEC = float(os.environ.get("STUDIO_ASSET_SETTLE_MAX", "6.0"))
ASSET_SETTLE_QUIET_SEC = float(os.environ.get("STUDIO_ASSET_SETTLE_QUIET", "0.7"))
WINDOW_SIZE = (1440, 900)
BROWSER_OFFLINE_CHECK = True          # re-open the saved file in Chrome with network blocked
MAX_ASSET_BYTES = 80 * 1024 * 1024
FETCH_TRIES = 2                       # keep retries, but fail fast on unavailable assets
RETRY_NO_PREVIEW = os.environ.get("STUDIO_FORCE_RETRY_NO_PREVIEW", "0") == "1"              # True = try templates again that earlier had no live preview
ALLOW_PRIVATE_HOSTS = False           # tests only: allow fetching from 127.0.0.1 etc.

TRACKING_HINTS = (
    "google-analytics", "googletagmanager", "doubleclick", "facebook.net",
    "facebook.com/tr", "hotjar", "clarity.ms", "segment.io", "segment.com",
    "mixpanel", "sentry.io", "plausible.io", "fullstory", "hs-scripts",
    "hsforms", "linkedin.com/px", "snap.licdn", "tiktok.com/i18n/pixel",
)
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HTTP_HEADERS = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9"}


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


# ----------------------------------------------------------- URL resilience
_PAGE_QUERY_KEYS = ("page", "paged", "p", "page_no", "page_num", "page_number",
                    "pagenum", "pageindex", "page_index", "currentpage", "current_page",
                    "pagenumber", "page-number", "page-number-index")
_PAGE_PATH_RE = re.compile(r"/(page[-_/]?)(\d+)(/?)$", re.I)


def normalize_http_url(raw_url, base_url=None):
    """Resolve a URL without discarding meaningful query parameters.

    This is intentionally provider-neutral: preview domains, listing routes and asset
    domains often differ, and the URL the provider redirects to is the useful base.
    """
    from html import unescape
    raw = unescape(str(raw_url or "").strip()).strip("\"'")
    if not raw or raw.lower().startswith(SKIP_PREFIXES):
        return None
    if raw.startswith("//"):
        raw = "https:" + raw
    elif base_url:
        raw = urljoin(base_url, raw)
    elif not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw.lstrip("/")
    try:
        parsed = urlparse(raw)
    except Exception:
        return None
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        return None
    # Keep the query exactly as entered; only fragments cannot be sent to a server.
    return urlunparse((parsed.scheme.lower(), parsed.netloc, parsed.path or "/", parsed.params,
                       parsed.query, ""))


def _page_number_from_url(url, label=""):
    """Read common page numbers from path/query/visible label without guessing item IDs."""
    try:
        p = urlparse(url)
        q = parse_qs(p.query)
        for key, values in q.items():
            if key.lower() in _PAGE_QUERY_KEYS and values and str(values[0]).isdigit():
                return int(values[0])
        m = _PAGE_PATH_RE.search(p.path or "")
        if m:
            return int(m.group(2))
        # A trailing /2/ is considered pagination only with a numeric page label or
        # explicit pagination semantics; never treat an arbitrary numeric item slug as a page.
        txt = (label or "").strip().lower()
        label_page = re.search(r"\bpage\s*[-:#]?\s*(\d+)\b", txt)
        if label_page:
            return int(label_page.group(1))
        if re.fullmatch(r"\d+", txt) or any(w in txt for w in ("next", "previous", "last page")):
            m = re.search(r"/(\d+)/?$", p.path or "")
            if m:
                return int(m.group(1))
    except Exception:
        pass
    return None


def _build_page_url(current_url, page_number, examples=()):
    """Infer page URL syntax from real pagination links while preserving filters/query."""
    current = urlparse(current_url)
    example = None
    example_num = None
    for href in examples:
        n = _page_number_from_url(href)
        if n and n != 1:
            example, example_num = urlparse(href), n
            break
    # If provider uses a query parameter, reuse its actual key rather than forcing ?page=.
    if example is not None:
        eq = parse_qs(example.query)
        ekey = next((k for k in eq if k.lower() in _PAGE_QUERY_KEYS), None)
        if ekey:
            params = parse_qs(current.query)
            # Drop stale/alternate pagination parameters so changing provider query
            # conventions cannot leave contradictory ?page=1&pageNumber=2 pairs.
            for key in list(params):
                if key.lower() in _PAGE_QUERY_KEYS:
                    params.pop(key, None)
            params[ekey] = [str(page_number)]
            return urlunparse((current.scheme or example.scheme, current.netloc or example.netloc,
                               current.path or example.path, "", urlencode(params, doseq=True), ""))
        # /page/27/, /page-27, /templates/page_27/ etc.
        m = _PAGE_PATH_RE.search(example.path or "")
        if m:
            path = (example.path or "")[:m.start()] + "/" + m.group(1) + str(page_number) + m.group(3)
            # Regex keeps the page marker but the slash-style route should remain /page/2/.
            if m.group(1).lower() == "page/":
                path = (example.path or "")[:m.start()] + "/page/" + str(page_number) + m.group(3)
            elif m.group(1).lower() == "page-":
                path = (example.path or "")[:m.start()] + "/page-" + str(page_number) + m.group(3)
            elif m.group(1).lower() == "page_":
                path = (example.path or "")[:m.start()] + "/page_" + str(page_number) + m.group(3)
            return urlunparse((current.scheme or example.scheme, current.netloc or example.netloc,
                               path, "", current.query, ""))
        # Last-segment number, where visible link text proved that it is a page link.
        m = re.search(r"/(\d+)/?$", example.path or "")
        if m:
            path = (example.path or "")[:m.start()] + "/" + str(page_number) + m.group(0)[-1:] if m.group(0).endswith("/") else (example.path or "")[:m.start()] + "/" + str(page_number)
            return urlunparse((current.scheme or example.scheme, current.netloc or example.netloc,
                               path, "", current.query, ""))

    # If the current URL itself is page-numbered, replace it using its own style.
    cq = parse_qs(current.query)
    ckey = next((k for k in cq if k.lower() in _PAGE_QUERY_KEYS), None)
    if ckey:
        cq[ckey] = [str(page_number)]
        return urlunparse((current.scheme, current.netloc, current.path, "", urlencode(cq, doseq=True), ""))
    cm = _PAGE_PATH_RE.search(current.path or "")
    if cm:
        return _build_page_url(urlunparse((current.scheme, current.netloc,
            (current.path or "")[:cm.start()] + "/", "", current.query, "")), page_number, examples)
    return None


def discover_pagination_targets(html, current_url):
    """Discover pagination from actual anchors and infer missing numeric pages.

    Returns {page_number: URL}. No provider-specific route is assumed; user-entered
    filters and hostnames are retained. This is used as the preferred strategy before
    a provider's legacy URL builder is considered.
    """
    current_url = normalize_http_url(current_url)
    if not current_url:
        return {}
    base = urlparse(current_url)
    current_number = _page_number_from_url(current_url) or 1
    targets = {current_number: current_url}
    soup = BeautifulSoup(html or "", "html.parser")
    examples = []
    for tag in soup.find_all(["a", "link"]):
        href = tag.get("href")
        if not href:
            continue
        absolute = normalize_http_url(href, current_url)
        if not absolute:
            continue
        p = urlparse(absolute)
        # Pagination should stay on the listing host (www/no-www is treated as same site).
        h1, h2 = (base.hostname or "").lower().removeprefix("www."), (p.hostname or "").lower().removeprefix("www.")
        if h1 != h2:
            continue
        rel_values = tag.get("rel") or []
        if not isinstance(rel_values, list):
            rel_values = [str(rel_values)]
        label = " ".join([tag.get_text(" ", strip=True), str(tag.get("aria-label", "")),
                          str(tag.get("title", "")), str(tag.get("data-page", "")),
                          " ".join(str(x) for x in rel_values)])
        label_low = label.lower()
        rel_next = "next" in label_low or "next" in [str(x).lower() for x in (tag.get("rel") or [])]
        number = _page_number_from_url(absolute, label)
        explicit = (number is not None or rel_next or any(x in label_low for x in ("next page", "last page", "previous page"))
                    or bool(re.search(r"[?&](?:page|paged|p|page_no|page_num|page_number|pagenum|pageindex|page_index|currentpage|current_page|pagenumber|page-number)=", absolute, re.I))
                    or bool(re.search(r"/(?:page[-_/]?\d+)(?:/|$)", p.path, re.I)))
        if not explicit:
            continue
        if number and number >= 1:
            targets[number] = absolute
            examples.append(absolute)
        elif rel_next and absolute != current_url:
            # A cursor-based provider may expose Next without a numeric page. Number it
            # relative to the page we are currently viewing; the next response can reveal
            # another cursor URL and the caller can continue following it.
            targets.setdefault(current_number + 1, absolute)
            examples.append(absolute)
    if len(targets) > 1:
        highest = max(targets)
        # Fill omitted middle numbers only when a numeric route pattern can be inferred.
        for n in range(current_number + 1, highest):
            if n not in targets:
                generated = _build_page_url(current_url, n, examples)
                if generated:
                    targets[n] = generated
    return dict(sorted(targets.items()))


def split_srcset_candidates(value):
    """Split srcset candidates without treating commas inside URLs as separators.

    Some CDNs (notably Wix) put image transforms in the URL path, for example
    ``.../v1/fill/w_228,h_335,al_c,q_90,enc_avif``. Splitting on every comma
    turns those transform tokens into fake asset URLs. This parser preserves the
    URL through the first whitespace, then reads an optional width/density
    descriptor up to the candidate-separating comma.
    """
    text = str(value or "")
    result = []
    i, n = 0, len(text)
    while i < n:
        while i < n and (text[i].isspace() or text[i] == ","):
            i += 1
        if i >= n:
            break

        start = i
        while i < n and not text[i].isspace():
            # Support the common descriptor-free form "url1,url2" while keeping
            # transform commas (which are followed by tokens, not another URL).
            if text[i] == "," and re.match(r"\s*(?:https?:)?//", text[i + 1:], re.I):
                break
            i += 1
        token = text[start:i]
        had_trailing_comma = token.endswith(",")
        url = token.rstrip(",")
        if not url:
            if i < n and text[i] == ",":
                i += 1
            continue
        if had_trailing_comma:
            result.append((url, ""))
            continue
        if i < n and text[i] == ",":
            result.append((url, ""))
            i += 1
            continue

        while i < n and text[i].isspace():
            i += 1
        desc_start = i
        while i < n and text[i] != ",":
            i += 1
        descriptor = text[desc_start:i].strip()
        result.append((url, descriptor))
        if i < n and text[i] == ",":
            i += 1
    return result


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
        if _looks_like_html_payload(raw, mime):
            suffix = Path(urlparse(absolute).path).suffix.lower()
            if suffix in (".css", ".js", ".mjs", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif",
                          ".svg", ".ico", ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp4", ".webm", ".mp3"):
                reason = "HTML/challenge page returned for a static asset"
                self.failed.append({"url": absolute, "reason": reason})
                self.log(f"    asset rejected: {absolute} ({reason})")
                return None
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
        for url, descriptor in split_srcset_candidates(value):
            local = self.local_ref(url, base, self.root)
            if local:
                url = local
            parts.append((url + (" " + descriptor if descriptor else "")).strip())
        return ", ".join(parts)

    # -- whole page
    def build(self, html):
        soup = BeautifulSoup(html, "html.parser")
        base = self.page_url
        base_tag = soup.find("base", href=True)
        if base_tag:
            base = urljoin(self.page_url, base_tag["href"])

        # Remove known analytics/pixel endpoints before packaging. Do not remove every
        # external iframe: maps, video and forms can be real page content; unresolved
        # functional embeds are reported by validation instead of silently deleted.
        for tag in list(soup.find_all(["script", "img", "iframe", "link", "source", "video", "audio"])):
            refs = [tag.get(a) for a in ("src", "href", "poster", "data-src", "data-original") if tag.get(a)]
            if any(is_tracking(urljoin(base, ref)) for ref in refs):
                self.removed_trackers.extend(ref for ref in refs if is_tracking(urljoin(base, ref)))
                tag.decompose()

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
            "removed_trackers": sorted(set(self.removed_trackers)),
            "remote_hosts_in_local_js": sorted(self.js_remote_hosts)},
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
        for attr in ("src", "poster", "data-src", "data-lazy-src", "data-original", "data-original-src",
                     "data-image-url", "data-background-image", "data-bg-image", "data-fallback-src",
                     "data-bg", "data-background", "data-bg-src", "data-lazy-background", "data-poster",
                     "data-poster-url", "data-lazyload"):
            if tag.has_attr(attr):
                check(f"{n}[{attr}]", tag[attr], pkg)
        for attr in ("srcset", "data-srcset", "data-lazy-srcset", "imagesrcset", "data-imagesrcset"):
            if tag.has_attr(attr):
                for candidate_url, _descriptor in split_srcset_candidates(tag[attr]):
                    check(f"{n}[{attr}]", candidate_url, pkg)
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
    elif res["remote_refs"] or res["iframes"]:
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
    # 1) webdriver-manager first: it is the method that already works on this PC (also used by the Wix crawler)
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
                window.scrollBy(0, 650); y += 650; steps++;
                if (y >= document.documentElement.scrollHeight || steps > 80) {
                    clearInterval(t); window.scrollTo(0, 0); resolve();
                }
            }, 80);
        });""")
    time.sleep(1.0)


def _resource_map(driver):
    """url -> response metadata for resources Chrome actually received."""
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
            response = p.get("response", {})
            url = response.get("url", "").split("#", 1)[0]
            if not url:
                continue
            out[url] = {
                "request_id": p.get("requestId"),
                "mime_type": response.get("mimeType", ""),
                "status": response.get("status"),
                "resource_type": p.get("type", ""),
            }
        except Exception:
            continue
    return out


def _successful_browser_response(info):
    """A 404 HTML response must never be saved as a CSS/JS asset."""
    try:
        status = float(info.get("status"))
    except (TypeError, ValueError, AttributeError):
        return True  # compatibility with injected test fetchers / old Chrome logs
    return 200 <= status < 300


def _asset_filename_from_path(path):
    """Get the real filename even when a CDN appends transformations after it.

    Example: /media/photo.webp/v1/fill/w_640 should resolve as photo.webp, not w_640.
    """
    path = unquote(path or "")
    matches = re.findall(
        r"([^/]+\.(?:css|mjs|js|jpe?g|png|gif|webp|avif|svg|ico|woff2?|ttf|otf|eot|mp4|webm|mp3|ogg|mov))(?:/|$)",
        path, re.I)
    return (matches[-1] if matches else Path(path).name).lower()


def _looks_like_html_payload(raw, mime=""):
    """Detect an HTTP 200 challenge/HTML page masquerading as a static asset."""
    head = (raw or b"")[:2048].lstrip().lower()
    html_prefix = head.startswith((b"<!doctype html", b"<html", b"<head", b"<body", b"<title"))
    html_mime = (mime or "").split(";", 1)[0].strip().lower() == "text/html"
    return html_prefix or (html_mime and b"<" in head[:256])


def _find_browser_resource(url, resources):
    """Find the browser-fetched copy of an asset even when its CDN URL differs.

    Providers sometimes emit stale first-party paths while Chrome loads a CDN
    path. We only reuse successful responses and require a unique path/filename
    match, so unrelated JavaScript or images are not silently substituted.
    """
    key = url.split("#", 1)[0]
    exact = resources.get(key)
    if exact and _successful_browser_response(exact):
        return key, exact

    try:
        target = urlparse(key)
        target_path = target.path or "/"
        target_query = target.query
        target_name = _asset_filename_from_path(target_path)
    except Exception:
        return key, None

    good = [(candidate, info) for candidate, info in resources.items()
            if _successful_browser_response(info) and info.get("request_id")]

    # Match same path/query on a different host first.
    exact_path = []
    for candidate, info in good:
        try:
            parsed = urlparse(candidate.split("#", 1)[0])
            if (parsed.path or "/") == target_path and parsed.query == target_query:
                exact_path.append((candidate, info))
        except Exception:
            continue
    if len(exact_path) == 1:
        return exact_path[0]

    # A CDN may prefix the original path with a service/version directory.
    suffix = []
    if len(target_path) >= 18:
        for candidate, info in good:
            try:
                path = urlparse(candidate.split("#", 1)[0]).path or "/"
                if path.endswith(target_path):
                    suffix.append((candidate, info))
            except Exception:
                continue
    if len(suffix) == 1:
        return suffix[0]

    # Last resort: unique basename + compatible asset type. Never guess if ambiguous.
    if target_name:
        same_name = []
        wanted_ext = Path(target_name).suffix.lower()
        for candidate, info in good:
            try:
                parsed = urlparse(candidate.split("#", 1)[0])
                if _asset_filename_from_path(parsed.path) != target_name:
                    continue
                mime = (info.get("mime_type") or "").lower()
                if wanted_ext in (".js", ".mjs") and mime and "javascript" not in mime:
                    continue
                if wanted_ext == ".css" and mime and "css" not in mime:
                    continue
                same_name.append((candidate, info))
            except Exception:
                continue
        if len(same_name) == 1:
            return same_name[0]
        if len(same_name) > 1 and wanted_ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"):
            # Responsive image CDNs often expose several widths of the same source
            # image. Prefer the closest requested width, but never break a tie by
            # arbitrary dictionary order.
            wanted_width_match = re.search(r"(?:^|[/,_-])(?:w_|width[=/])([0-9]{2,5})", target_path, re.I)
            if wanted_width_match:
                wanted_width = int(wanted_width_match.group(1))
                scored = []
                for candidate, info in same_name:
                    cm = re.search(r"(?:^|[/,_-])(?:w_|width[=/])([0-9]{2,5})", urlparse(candidate).path, re.I)
                    if cm:
                        scored.append((abs(int(cm.group(1)) - wanted_width), candidate, info))
                if scored:
                    best_score = min(x[0] for x in scored)
                    best = [x for x in scored if x[0] == best_score]
                    if len(best) == 1:
                        return best[0][1], best[0][2]
    return key, None


class BrowserFetcher:
    """Prefer exact, successful bytes Chrome received; fall back to authenticated HTTP."""

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
        browser_url, info = _find_browser_resource(url, self.resources)
        if info:
            try:
                r = self.driver.execute_cdp_cmd("Network.getResponseBody", {"requestId": info["request_id"]})
                import base64
                body = base64.b64decode(r["body"]) if r.get("base64Encoded") else r.get("body", "").encode("utf-8")
                ext = Path(urlparse(browser_url).path).suffix.lower()
                is_script_asset = ext in (".css", ".js", ".mjs")
                if body and not (is_script_asset and _looks_like_html_payload(body, info.get("mime_type", ""))):
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
            r = session.get(url, headers={"Referer": referer}, timeout=(5, 14))
            r.raise_for_status()
            return r.content, r.headers.get("Content-Type", "")
        except Exception:
            time.sleep(0.2 * i)
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
    """Wait for DOM readiness, then adaptively wait for network/DOM activity to settle.

    This avoids a long fixed sleep on fast pages while allowing slow JS/lazy assets a
    bounded settling window. Websites with permanent polling/websockets never block us.
    """
    from selenium.webdriver.support.ui import WebDriverWait
    try:
        WebDriverWait(driver, seconds).until(lambda d: d.execute_script(
            "return document.readyState") in ("interactive", "complete"))
    except Exception:
        pass
    start = time.monotonic()
    quiet_since = start
    last_signature = None
    deadline = start + max(ASSET_SETTLE_MIN_SEC, ASSET_SETTLE_MAX_SEC)
    while time.monotonic() < deadline:
        try:
            state = driver.execute_script("""
              const imgs=[...document.images];
              const pending=imgs.filter(i => !i.complete).length;
              const h=document.documentElement;
              return {ready:document.readyState, pending, height:h?h.scrollHeight:0,
                count:document.querySelectorAll('*').length,
                resources:performance.getEntriesByType('resource').length};
            """)
            signature = (state.get("pending"), state.get("height"), state.get("count"), state.get("resources"))
            if signature != last_signature:
                last_signature = signature
                quiet_since = time.monotonic()
            elapsed = time.monotonic() - start
            quiet = time.monotonic() - quiet_since
            if elapsed >= ASSET_SETTLE_MIN_SEC and quiet >= ASSET_SETTLE_QUIET_SEC and state.get("pending", 0) == 0:
                break
        except Exception:
            if time.monotonic() - start >= ASSET_SETTLE_MIN_SEC:
                break
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

        # Capture static files that were fetched by JavaScript or CSS at runtime but
        # are not present as direct references in the final DOM (e.g. responsive images,
        # dynamically inserted fonts and background images). Reuse Chrome's response
        # bodies first, avoiding another network round-trip whenever possible.
        extra_assets = 0
        for resource_url, info in fetcher.resources.items():
            mime = (info.get("mime_type") or "").lower().split(";", 1)[0].strip()
            path = urlparse(resource_url).path.lower()
            static_mime = (mime.startswith(("image/", "font/", "audio/", "video/")) or
                           mime in ("text/css", "text/javascript", "application/javascript", "application/x-javascript"))
            static_ext = path.endswith((".css", ".js", ".mjs", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".svg", ".ico", ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp4", ".webm", ".mp3", ".ogg"))
            if static_mime or static_ext:
                before = len(builder.manifest)
                builder.save_asset(resource_url, current, hint=None, referer=current)
                if len(builder.manifest) > before:
                    extra_assets += 1
        if extra_assets:
            step(f"Captured {extra_assets} additional runtime assets")
            # Update manifest after runtime assets have been added.
            (tmp / "asset-manifest.json").write_text(json.dumps({
                "source_url": current, "asset_count": len(builder.manifest),
                "assets": builder.manifest, "failed": builder.failed,
                "removed_trackers": sorted(set(builder.removed_trackers))},
                indent=2, ensure_ascii=False), encoding="utf-8")
        asset_count = len(builder.manifest)

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
            "provider": meta_extra.get("provider", "unknown"), "template_id": slug, "status": status,
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


# ======================================================================================
#  Shared crawl machinery used by the provider crawlers
# ======================================================================================
def safe_slug(text, maxlen=60):
    """A folder-name-safe, length-limited id (Windows paths are short)."""
    s = re.sub(r"[^a-z0-9._-]+", "-", unquote(str(text)).lower()).strip("-.")
    return (s[:maxlen].strip("-.") or "template")


def first_line(e, n=200):
    return (str(e).strip().splitlines() or [repr(e)])[0][:n]


class FetchError(Exception):
    pass


class PageFetcher:
    """Reads listing / template pages.  Plain HTTP first (fast); if the site refuses it, or the page needs
    JavaScript (the caller's `ok(html)` check fails), the page is opened in a real Chrome instead.
    Chrome is only started if needed, and is closed by close()."""

    def __init__(self):
        self.driver = None
        self.owned = False
        self.http_blocked = 0

    def attach(self, driver):
        """Reuse the capture browser instead of starting a second Chrome."""
        self.close()
        self.driver, self.owned = driver, False

    def close(self):
        if self.driver is not None and self.owned:
            try:
                self.driver.quit()
            except Exception:
                pass
        self.driver, self.owned = None, False

    def _browser(self):
        if self.driver is not None and driver_alive(self.driver):
            return self.driver
        if self.driver is not None and self.owned:
            try:
                self.driver.quit()
            except Exception:
                pass
        self.driver, self.owned = make_driver(light=True), True
        return self.driver

    def get(self, url, ok=None, scroll=False, force_browser=False):
        """-> html text. Raises FetchError if nothing could be read."""
        html, status = None, "no answer"
        if not force_browser:
            try:
                r = requests.get(url, headers=HTTP_HEADERS, timeout=(8, 30))
                status = r.status_code
                if r.status_code == 200 and r.text:
                    html = r.text
            except Exception as e:
                status = type(e).__name__
            if html is not None and (ok is None or ok(html)):
                return html
            self.http_blocked += 1
        try:
            d = self._browser()
            try:
                d.get(url)
            except Exception as e:
                if "timeout" not in str(e).lower():
                    raise
            wait_ready(d, 25)
            if scroll:
                scroll_full_page(d)
            time.sleep(1.0)
            return d.page_source
        except BrowserUnavailable:
            raise
        except Exception as e:
            if html:
                return html                     # plain HTTP answered, even if the check did not like it
            raise FetchError(f"could not read {url} (HTTP {status}; browser: {first_line(e)})")


def run_crawl(*, provider, label, default_url, urls, output_dir, collect_jobs, resolve_preview):
    """The generic crawl loop.

      collect_jobs(urls, fetcher)      -> (jobs, bad_urls)   jobs: [{slug, template_url, listing_url, title?, preview_url?}]
      resolve_preview(job, fetcher)    -> {preview_url, title?, description?, categories?}  or raises NoPreview

    Returns (saved, failed, skipped)."""
    out_root = Path(output_dir)
    out_root.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(out_root / ".tmp", ignore_errors=True)         # leftovers of an interrupted run

    if not urls:
        urls = [default_url]
        print(f"[{provider}] no URL given - using the default: {default_url}")
    print(f"[{provider}] {len(urls)} URL(s) to read; saving into {out_root}")

    fetcher = PageFetcher()
    saved = failed = skipped = 0
    driver = None

    def chrome_error(e):
        print(f"ERROR: Chrome could not be started - nothing was crawled. {e}")
        print("       Check: Google Chrome is installed, the internet is reachable (a VPN/proxy/antivirus can block the driver download),")
        print("       and run:  python -m pip install -U selenium webdriver-manager")
        raise SystemExit(1)

    try:
        step(f"Reading the {label} listing / template pages")
        try:
            jobs, bad = collect_jobs(urls, fetcher)
        except BrowserUnavailable as e:
            chrome_error(e)
    finally:
        fetcher.close()                     # free the listing browser before the capture browser starts

    for u in bad:
        failed += 1
        print(f"  FAILED: {u} -> not a {label} template or listing URL")
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
    if not jobs:
        if not bad:
            failed += 1
            print("  FAILED: no templates were found on the page(s) - the site may be blocking the crawler or its layout changed")
        print(f"\nDone. saved: {saved}, failed: {failed}, skipped (already done): {skipped}. Output: {out_root}")
        return saved, failed, skipped

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

            result, last_err, no_preview, preview_url = None, "", False, ""
            for attempt in range(1, TEMPLATE_ATTEMPTS + 1):
                try:
                    if driver is None or not driver_alive(driver):
                        if driver is not None:
                            step("Chrome stopped responding - restarting it")
                            try:
                                driver.quit()
                            except Exception:
                                pass
                        else:
                            step("Starting Chrome")
                        driver = make_driver()
                        fetcher.attach(driver)
                    info = resolve_preview(job, fetcher)
                    preview_url = info["preview_url"]
                    step(f"Live preview: {preview_url}")
                    meta = {"provider": provider, "template_url": job.get("template_url", ""),
                            "listing_url": job.get("listing_url", ""),
                            "title": info.get("title") or job.get("title", ""),
                            "description": info.get("description", ""), "categories": info.get("categories", [])}
                    result = capture_site(driver, preview_url, out_root, job["slug"], meta)
                    break
                except BrowserUnavailable as e:
                    print(f"ERROR: Chrome could not be restarted - stopping. {e}")
                    raise SystemExit(1)
                except NoPreview as e:
                    last_err, no_preview = str(e), True
                    break                       # retrying cannot create a preview
                except Exception as e:
                    last_err = first_line(e, 300)
                    print(f"  attempt {attempt}/{TEMPLATE_ATTEMPTS} failed: {last_err}")
                    if attempt < TEMPLATE_ATTEMPTS:
                        time.sleep(DELAY_BETWEEN_TEMPLATES_SEC)

            now = datetime.now().isoformat(timespec="seconds")
            if result and result["status"] in ("completed", "partial"):
                saved += 1
                label_ok = "OK" if result["status"] == "completed" else "PARTIAL"
                print(f"  saved: {result['path']} | assets: {result['assets']} | validation: {label_ok}")
                for w in result["warnings"]:
                    print(f"  WARNING: {w}")
                append_log(out_root, [now, job["slug"], result.get("title", ""), job.get("template_url", ""), preview_url,
                                      result["status"], result["path"], result["assets"],
                                      " | ".join(result["warnings"]), ""])
            else:
                failed += 1
                err = (result or {}).get("error") or last_err or "unknown error"
                print(f"  FAILED: {tid} -> {err}")
                append_log(out_root, [now, job["slug"], "", job.get("template_url", ""), preview_url,
                                      "no_preview" if no_preview else "failed",
                                      (result or {}).get("path", ""), "", "", err])
            time.sleep(DELAY_BETWEEN_TEMPLATES_SEC)
    finally:
        fetcher.close()
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

    print(f"\nDone. saved: {saved}, failed: {failed}, skipped (already done): {skipped}. Output: {out_root}")
    return saved, failed, skipped


def standalone_main(crawl, default_out_name):
    """`python <crawler>.py [url ...] [--out DIR]` for running a provider crawler without the app."""
    args = sys.argv[1:]
    out = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), default_out_name)
    if "--out" in args:
        i = args.index("--out")
        out = args[i + 1]
        del args[i:i + 2]
    s, f, k = crawl(args, out)
    sys.exit(1 if f and not (s or k) else 0)
