import base64
import csv
import json
import os
import re
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
)
from webdriver_manager.chrome import ChromeDriverManager

# ----------------------- CONFIG -----------------------

LISTING_URLS = [

        #Industry
         "https://www.wix.com/website/templates/html/business",
         "https://www.wix.com/website/templates/html/industrial",
         "https://www.wix.com/website/templates/html/health-wellness",
          "https://www.wix.com/website/templates/html/events",
         "https://www.wix.com/website/templates/html/education",
          "https://www.wix.com/website/templates/html/communities",
          "https://www.wix.com/website/templates/html/fashion-and-style",
          "https://www.wix.com/website/templates/html/beauty-and-hair",
          "https://www.wix.com/website/templates/html/design",
          "https://www.wix.com/website/templates/html/photography",
          "https://www.wix.com/website/templates/html/music",
          "https://www.wix.com/website/templates/html/creative-arts",
          "https://www.wix.com/website/templates/html/video",
          "https://www.wix.com/website/templates/html/restaurants-food",
          "https://www.wix.com/website/templates/html/travel-tourism",
]

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "screenshots")
LOG_FILE = os.path.join(OUTPUT_DIR, "log.csv")
PAGE_LOAD_TIMEOUT = 60
DELAY_BETWEEN_SHOTS_SEC = 3.5
WINDOW_SIZE = (1440, 900)

# Offline packaging / validation settings
OFFLINE_VALIDATION = True
OFFLINE_VALIDATION_TIMEOUT = 30
ASSET_RETRY_COUNT = 2

# ------------------- VALIDATED SELECTORS -------------------
SEL_VIEW_LINK = 'a[data-hook="hover-panel__previewLink"]'
SEL_TITLE = '[data-hook="template__title"]'
SEL_GOOD_FOR = '[data-hook="info-panel__goodFor"]'
SEL_DESCRIPTION = '[data-hook="info-panel__description"]'
SEL_LAST_PAGE_LINK = 'li[data-hook="numericPagination__lastArrowLink"] a'
SEL_DESKTOP_IFRAME = 'iframe[data-hook="desktop-iframe"]'
SEL_DIALOG = "dialog"
SEL_DIALOG_CLOSE_BUTTON = "dialog button"  # the only <button> in the dialog is the X

# -------------------------------------------------------


def slugify(text: str) -> str:
    s = re.sub(r"^https?://", "", text)
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s


def ensure_dir(d):
    os.makedirs(d, exist_ok=True)


def load_done_set():
    done = set()
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("status") == "OK":
                    done.add(row["viewUrl"])
    else:
        ensure_dir(OUTPUT_DIR)
        with open(LOG_FILE, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(
                ["listing", "wh_id", "title", "viewUrl", "liveUrl", "status", "file"]
            )
    return done


def append_log(listing, wh_id, title, view_url, live_url, status, file_path=""):
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([listing, wh_id, title, view_url, live_url, status, file_path])


def wh_id_from_url(url: str) -> str:
    m = re.search(r"/view/html/([a-zA-Z0-9\-]+)", url)
    return m.group(1) if m else slugify(url)[:40]


def make_driver():
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument(f"--window-size={WINDOW_SIZE[0]},{WINDOW_SIZE[1]}")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-background-networking")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )

    # Chrome performance logs give us request IDs for resources actually
    # loaded by the rendered Wix page. Network.getResponseBody() can then
    # recover the exact bytes Chrome received, including Wix CDN assets that
    # a plain requests call may reject.
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)

    try:
        driver.execute_cdp_cmd("Network.enable", {
            "maxTotalBufferSize": 100 * 1024 * 1024,
            "maxResourceBufferSize": 20 * 1024 * 1024,
        })
    except Exception as e:
        print(f"  Network CDP enable warning: {e}")

    return driver


SEL_PAGE_LINK = 'a[data-hook="pageLink__link"]'


def _page_number_from_link(href="", label=""):
    """Read pagination numbers from path URLs, query URLs, or accessible labels."""
    href = href or ""
    patterns = (
        r"/(?:page[-/])?(\d+)/?(?:[?#].*)?$",
        r"[?&](?:page|p|pageNumber|page_number|currentPage)=(\d+)(?:&|$)",
        r"(?:page|pagination)[=/ -](\d+)",
    )
    for pattern in patterns:
        match = re.search(pattern, href, re.I)
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                pass
    label_match = re.search(r"(?:page\s*)?(\d+)", (label or "").strip(), re.I)
    if label_match:
        try:
            return int(label_match.group(1))
        except ValueError:
            pass
    return None


def get_pagination_targets(driver, listing_url):
    """Return {page_number: actual_href} discovered from the rendered pagination."""
    targets = {1: listing_url}
    selectors = [SEL_PAGE_LINK, SEL_LAST_PAGE_LINK, 'a[rel="next"]',
                 'a[aria-label*="page" i]', 'a[data-page]']
    seen = set()
    for selector in selectors:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
        except Exception:
            elements = []
        for el in elements:
            try:
                href = el.get_attribute("href") or ""
                if not href or href in seen:
                    continue
                seen.add(href)
                label = " ".join(filter(None, [el.text, el.get_attribute("aria-label"), el.get_attribute("data-page")]))
                number = _page_number_from_link(href, label)
                if number:
                    targets[number] = urljoin(listing_url, href)
            except Exception:
                continue
    return targets


def _fallback_page_url(listing_url, page_number, known_targets=None):
    """Construct a fallback page URL without corrupting existing query parameters."""
    targets = known_targets or {}
    # Infer pagination style from real links already observed on the page.
    values = list(targets.values())
    for candidate in values:
        num = _page_number_from_link(candidate)
        if not num or num == 1:
            continue
        parsed_candidate = urlparse(candidate)
        if re.search(r"/(?:page[-/])?\d+/?$", parsed_candidate.path, re.I):
            base = urlparse(listing_url)
            suffix = re.search(r"/(?:page[-/])?(\d+)/?$", parsed_candidate.path, re.I)
            root_path = parsed_candidate.path[:suffix.start()] if suffix else base.path
            return urlunparse((base.scheme or parsed_candidate.scheme, base.netloc or parsed_candidate.netloc,
                               root_path.rstrip("/") + f"/{page_number}", "", base.query, ""))
        q = parse_qs(parsed_candidate.query)
        for key in ("page", "p", "pageNumber", "page_number", "currentPage"):
            if key in q:
                base = urlparse(listing_url)
                params = parse_qs(base.query)
                for stale_key in list(params):
                    if stale_key.lower() in {"page", "p", "pagenumber", "page_number", "currentpage", "current_page", "paged", "pageindex", "page_index"}:
                        params.pop(stale_key, None)
                params[key] = [str(page_number)]
                return urlunparse((base.scheme, base.netloc, base.path, "", urlencode(params, doseq=True), ""))

    base = urlparse(listing_url)
    params = parse_qs(base.query)
    for key in ("page", "p", "pageNumber", "page_number", "currentPage"):
        if key in params:
            for stale_key in list(params):
                if stale_key.lower() in {"page", "p", "pagenumber", "page_number", "currentpage", "current_page", "paged", "pageindex", "page_index"}:
                    params.pop(stale_key, None)
            params[key] = [str(page_number)]
            return urlunparse((base.scheme, base.netloc, base.path, "", urlencode(params, doseq=True), ""))
    # Wix search/criteria pages commonly use query pagination; older category pages
    # use /html/<category>/<N>. Preserve all search criteria in both cases.
    if "/website/templates" in (base.path or "") and "/html/" not in (base.path or ""):
        params["page"] = [str(page_number)]
        return urlunparse((base.scheme, base.netloc, base.path, "", urlencode(params, doseq=True), ""))
    return urlunparse((base.scheme, base.netloc, base.path.rstrip("/") + f"/{page_number}", "", base.query, ""))


def get_last_page_number(driver, default=1):
    """Wait for results, then infer page count from actual link URLs/labels."""
    try:
        WebDriverWait(driver, 15).until(lambda d: d.execute_script(
            "return !!document.querySelector('a[data-hook=\"hover-panel__previewLink\"],a[href*=\"/view/html/\"]')"))
    except Exception:
        pass
    targets = get_pagination_targets(driver, driver.current_url or "https://www.wix.com/website/templates")
    numbers = list(targets)
    return max(numbers) if numbers else default


def collect_cards(driver):
    """Find template view URLs using the current selector plus route-based fallbacks."""
    view_links = []
    for selector in (SEL_VIEW_LINK, 'a[href*="/view/html/"]', 'a[href*="/website-template/view/"]'):
        try:
            view_links.extend(driver.find_elements(By.CSS_SELECTOR, selector))
        except Exception:
            continue
    cards, seen = [], set()
    for link in view_links:
        try:
            href = link.get_attribute("href")
        except Exception:
            href = ""
        if not href or href in seen:
            continue
        parsed = urlparse(href)
        if (parsed.hostname or "").lower() not in ("wix.com", "www.wix.com"):
            continue
        if not re.search(r"/(?:website-template/)?view/html/[^/?#]+", parsed.path, re.I):
            continue
        seen.add(href)
        cards.append({"view_url": href, "wh_id": wh_id_from_url(href),
                      "title": (getattr(link, "text", "") or "").strip()})
    # If an alternate markup no longer exposes the known data-hook, do not
    # return an empty grid silently: inspect all links for the stable view route.
    if not cards:
        try:
            anchors = driver.find_elements(By.CSS_SELECTOR, "a[href]")
        except Exception:
            anchors = []
        for link in anchors:
            try:
                href = link.get_attribute("href") or ""
                if href in seen or not re.search(r"/(?:website-template/)?view/html/[^/?#]+", urlparse(href).path, re.I):
                    continue
                seen.add(href)
                cards.append({"view_url": href, "wh_id": wh_id_from_url(href),
                              "title": (link.get_attribute("aria-label") or link.get_attribute("title") or link.text or "").strip()})
            except Exception:
                continue
    return cards


def enrich_titles(driver, cards):
    """Match title elements (id="template-title-<wh_id>--N") to cards by wh_id."""
    by_id = {c["wh_id"]: c for c in cards}
    title_els = driver.find_elements(By.CSS_SELECTOR, SEL_TITLE)
    for el in title_els:
        el_id = el.get_attribute("id") or ""
        m = re.search(r"template-title-(.+?)--\d+", el_id)
        if not m:
            continue
        wh_id = m.group(1)
        if wh_id in by_id:
            try:
                title_text = el.find_element(By.TAG_NAME, "div").text
            except NoSuchElementException:
                title_text = el.text
            by_id[wh_id]["title"] = title_text.strip()
    for c in cards:
        c.setdefault("title", "")
    return cards


def get_live_template_url(driver, view_url):
    """Open a Wix view page and discover the actual preview iframe without relying on one hook."""
    driver.get(view_url)
    try:
        iframe = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, SEL_DESKTOP_IFRAME))
        )
        src = iframe.get_attribute("src")
        if src and src.startswith(("http://", "https://")):
            return urljoin(driver.current_url or view_url, src)
    except TimeoutException:
        pass
    try:
        frames = driver.find_elements(By.CSS_SELECTOR, "iframe[src]")
    except Exception:
        frames = []
    candidates = []
    for frame in frames:
        try:
            src = urljoin(driver.current_url or view_url, frame.get_attribute("src") or "")
            if not src.startswith(("http://", "https://")) or src.rstrip("/") == (driver.current_url or view_url).rstrip("/"):
                continue
            rect = driver.execute_script("const r=arguments[0].getBoundingClientRect();return [r.width,r.height];", frame)
            area = float(rect[0] or 0) * float(rect[1] or 0)
            # Prefer large visible frames, then frames served from the actual preview origin.
            score = area + (1e9 if "demone2" in urlparse(src).path else 0)
            candidates.append((score, src))
        except Exception:
            continue
    return max(candidates, default=(0, None))[1]


def dismiss_dialog_if_present(driver):
    try:
        btn = WebDriverWait(driver, 3).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, SEL_DIALOG_CLOSE_BUTTON))
        )
        btn.click()
        time.sleep(0.3)
    except Exception:
        pass


def scroll_full_page(driver):
    driver.execute_script(
        """
        return new Promise((resolve) => {
            let total = 0;
            const distance = 400;
            const timer = setInterval(() => {
                window.scrollBy(0, distance);
                total += distance;
                if (total >= document.body.scrollHeight) {
                    clearInterval(timer);
                    window.scrollTo(0, 0);
                    resolve();
                }
            }, 150);
        });
        """
    )
    time.sleep(0.8)


def _guess_mime_type(url, content_type=""):
    """Return a MIME type suitable for a data URL."""
    if content_type:
        content_type = content_type.split(";")[0].strip().lower()
        if content_type:
            return content_type

    path = urlparse(url).path.lower()
    ext_map = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".avif": "image/avif",
        ".svg": "image/svg+xml",
        ".ico": "image/x-icon",
        ".woff": "font/woff",
        ".woff2": "font/woff2",
        ".ttf": "font/ttf",
        ".otf": "font/otf",
        ".eot": "application/vnd.ms-fontobject",
        ".css": "text/css",
        ".js": "application/javascript",
    }
    for ext, mime in ext_map.items():
        if path.endswith(ext):
            return mime
    return "application/octet-stream"


def _to_data_url(raw_bytes, mime_type):
    encoded = base64.b64encode(raw_bytes).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _normalise_asset_url(raw_url, page_url):
    """Turn an asset reference into an absolute HTTP(S) URL; never fetch script/data schemes."""
    if not raw_url:
        return None

    raw_url = raw_url.strip().strip("'\"")
    if raw_url.lower().startswith(("data:", "blob:", "#", "javascript:", "mailto:", "tel:", "about:")):
        return None
    if raw_url.startswith("//"):
        raw_url = "https:" + raw_url
    else:
        raw_url = urljoin(page_url, raw_url)
    parsed = urlparse(raw_url)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        return None
    return raw_url


def _build_authenticated_session(driver):
    """Create a requests session carrying the cookies from the live Chrome page."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        ),
        "Accept": "*/*",
    })

    try:
        for cookie in driver.get_cookies():
            session.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain"),
                path=cookie.get("path", "/"),
            )
    except Exception as e:
        print(f"    cookie transfer warning: {e}")

    return session


def _browser_resource_map(driver):
    """
    Read Chrome performance logs and return a map of resources Chrome loaded:

        normalized URL -> {request_id, mime_type, status}

    Multiple requests to the same URL are possible; the last response is
    generally the most useful one.
    """
    resource_map = {}

    try:
        logs = driver.get_log("performance")
    except Exception:
        return resource_map

    for entry in logs:
        try:
            message = json.loads(entry["message"])["message"]
            if message.get("method") != "Network.responseReceived":
                continue

            params = message.get("params", {})
            response = params.get("response", {})
            url = response.get("url", "")
            request_id = params.get("requestId")
            if not url or not request_id:
                continue

            resource_map[url.split("#", 1)[0]] = {
                "request_id": request_id,
                "mime_type": response.get("mimeType", ""),
                "status": response.get("status"),
                "resource_type": params.get("type", ""),
            }
        except Exception:
            continue

    return resource_map


def _clear_browser_logs(driver):
    """Discard old performance events before rendering a new template."""
    try:
        driver.get_log("performance")
    except Exception:
        pass


def _get_browser_body(driver, resource_info):
    """Recover exact bytes from a Chrome network response."""
    if not resource_info:
        return None, ""

    try:
        result = driver.execute_cdp_cmd(
            "Network.getResponseBody",
            {"requestId": resource_info["request_id"]},
        )
        body = result.get("body", "")
        if result.get("base64Encoded"):
            raw = base64.b64decode(body)
        else:
            raw = body.encode("utf-8")
        return raw, resource_info.get("mime_type", "")
    except Exception:
        return None, ""


def _download_asset(session, asset_url, cache, referer, driver=None,
                    browser_resources=None):
    """
    Download one asset and convert it to a data URL.

    Priority:
      1. Chrome's already-loaded response body (exact browser bytes).
      2. Authenticated requests.Session() fallback.

    Chrome is preferred because the rendered Wix page is the source of truth.
    """
    if not asset_url or asset_url.startswith("data:"):
        return asset_url

    key = asset_url.split("#", 1)[0]
    if key in cache:
        return cache[key]

    # First try the browser response. This avoids a common Wix failure mode:
    # Chrome can render the image while a second requests call gets 403/401.
    if driver is not None and browser_resources:
        resource_info = browser_resources.get(key)
        raw_bytes, browser_mime = _get_browser_body(driver, resource_info)
        if raw_bytes is not None:
            mime = _guess_mime_type(key, browser_mime)
            data_url = _to_data_url(raw_bytes, mime)
            cache[key] = data_url
            return data_url

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        ),
        "Referer": referer,
        "Accept": "*/*",
    }

    last_error = None
    for attempt in range(ASSET_RETRY_COUNT + 1):
        try:
            response = session.get(key, headers=headers, timeout=30)
            response.raise_for_status()

            mime = _guess_mime_type(
                key,
                response.headers.get("Content-Type", "")
            )
            data_url = _to_data_url(response.content, mime)
            cache[key] = data_url
            return data_url
        except Exception as e:
            last_error = e
            if attempt < ASSET_RETRY_COUNT:
                time.sleep(0.5 * (attempt + 1))

    print(f"    asset failed: {key} -> {last_error}")
    cache[key] = None
    return None


def _extract_css_urls(css_text):
    """
    Find url(...) references in CSS.
    """
    pattern = re.compile(
        r"url\(\s*([\"']?)(.*?)\1\s*\)",
        re.IGNORECASE | re.DOTALL
    )
    return [m.group(2).strip() for m in pattern.finditer(css_text)]


def _embed_css_assets(css_text, css_base_url, session, cache, driver=None, browser_resources=None):
    """
    Replace image/font/etc. url(...) references in CSS with data URLs.
    """
    pattern = re.compile(
        r"url\(\s*([\"']?)(.*?)\1\s*\)",
        re.IGNORECASE | re.DOTALL
    )

    def replace(match):
        quote = match.group(1)
        raw_url = match.group(2).strip()

        absolute_url = _normalise_asset_url(raw_url, css_base_url)
        if not absolute_url:
            return match.group(0)

        data_url = _download_asset(
            session,
            absolute_url,
            cache,
            css_base_url,
            driver=driver,
            browser_resources=browser_resources,
        )

        if data_url:
            return f"url({quote}{data_url}{quote})"

        return match.group(0)

    return pattern.sub(replace, css_text)


def _make_html_fully_self_contained(driver, page_url):
    """
    Package the rendered Wix template into ONE portable HTML file.

    No later browser opening is required. All resources that are needed for
    the rendered page are captured during the crawl and embedded as data URLs.
    """
    scroll_full_page(driver)
    time.sleep(2.0)

    html = driver.execute_script(
        "return document.documentElement.outerHTML;"
    )
    actual_page_url = driver.current_url or page_url

    session = _build_authenticated_session(driver)
    browser_resources = _browser_resource_map(driver)
    asset_cache = {}
    soup = BeautifulSoup(html, "html.parser")

    # Resources that Chrome loaded but which are not necessarily represented
    # as a normal src attribute. They are useful for diagnosing missed assets.
    browser_resource_urls = set(browser_resources.keys())

    def embed_url(raw_url, base_url=actual_page_url):
        absolute = _normalise_asset_url(raw_url, base_url)
        if not absolute:
            return None
        return _download_asset(
            session,
            absolute,
            asset_cache,
            base_url,
            driver=driver,
            browser_resources=browser_resources,
        )

    def replace_srcset(value, base_url=actual_page_url):
        if not value:
            return value
        output = []
        for raw_url, descriptor in _v4_split_srcset(value):
            data_url = embed_url(raw_url, base_url)
            if data_url:
                raw_url = data_url
            output.append((raw_url + (" " + descriptor if descriptor else "")).strip())
        return ", ".join(output)

    # ---------------------------------------------------------
    # 1. Images, lazy images, responsive sources, SVG and video poster.
    # ---------------------------------------------------------
    asset_attrs = [
        "src", "data-src", "data-lazy-src", "data-original",
        "data-image-url", "poster", "href", "xlink:href",
    ]

    for tag in soup.find_all(["img", "source", "video", "image"]):
        for attr in asset_attrs:
            if not tag.has_attr(attr):
                continue
            raw = tag.get(attr)
            if not raw:
                continue
            if attr in {"href", "xlink:href"} and tag.name != "image":
                continue
            data_url = embed_url(raw)
            if data_url:
                tag[attr] = data_url

        for attr in ["srcset", "data-srcset"]:
            if tag.has_attr(attr):
                tag[attr] = replace_srcset(tag.get(attr))

    # ---------------------------------------------------------
    # 2. Inline styles and CSS url(...) references.
    # ---------------------------------------------------------
    for tag in soup.find_all(style=True):
        tag["style"] = _embed_css_assets(
            tag.get("style") or "",
            actual_page_url,
            session,
            asset_cache,
            driver=driver,
            browser_resources=browser_resources,
        )

    # ---------------------------------------------------------
    # 3. External stylesheets -> inline CSS.
    # ---------------------------------------------------------
    for link in list(soup.find_all("link")):
        rel = [x.lower() for x in (link.get("rel") or [])]
        href = link.get("href")
        if "stylesheet" not in rel or not href:
            continue

        absolute_css = _normalise_asset_url(href, actual_page_url)
        if not absolute_css:
            continue

        css_text = None
        try:
            response = session.get(
                absolute_css,
                headers={"Referer": actual_page_url},
                timeout=30,
            )
            response.raise_for_status()
            css_text = response.text
        except Exception:
            raw_bytes, _ = _get_browser_body(
                driver, browser_resources.get(absolute_css)
            )
            if raw_bytes is not None:
                css_text = raw_bytes.decode("utf-8", errors="replace")

        if css_text is None:
            print(f"    stylesheet failed: {absolute_css}")
            continue

        css_text = _embed_css_assets(
            css_text,
            absolute_css,
            session,
            asset_cache,
            driver=driver,
            browser_resources=browser_resources,
        )
        style_tag = soup.new_tag("style")
        style_tag["data-embedded-from"] = absolute_css
        style_tag.string = css_text
        link.replace_with(style_tag)

    # ---------------------------------------------------------
    # 4. Remaining inline <style> blocks.
    # ---------------------------------------------------------
    for style_tag in soup.find_all("style"):
        css_text = style_tag.string or style_tag.get_text() or ""
        embedded_css = _embed_css_assets(
            css_text,
            actual_page_url,
            session,
            asset_cache,
            driver=driver,
            browser_resources=browser_resources,
        )
        style_tag.clear()
        style_tag.append(embedded_css)

    # ---------------------------------------------------------
    # 5. External scripts -> inline JavaScript where possible.
    #    This prevents a visual page from trying to download remote JS after
    #    the developer receives the HTML. Scripts that cannot be captured are
    #    removed only when they are explicitly marked as third-party remote
    #    resources; the original src is otherwise retained and validation
    #    reports it.
    # ---------------------------------------------------------
    for script in list(soup.find_all("script", src=True)):
        raw_src = script.get("src")
        absolute_js = _normalise_asset_url(raw_src, actual_page_url)
        if not absolute_js:
            continue

        js_text = None
        try:
            response = session.get(
                absolute_js,
                headers={"Referer": actual_page_url},
                timeout=30,
            )
            response.raise_for_status()
            js_text = response.text
        except Exception:
            raw_bytes, _ = _get_browser_body(
                driver, browser_resources.get(absolute_js)
            )
            if raw_bytes is not None:
                js_text = raw_bytes.decode("utf-8", errors="replace")

        if js_text is not None:
            # A literal </script> would terminate an inline script element.
            js_text = js_text.replace("</script>", "<\\/script>")
            script.attrs.pop("src", None)
            script.string = js_text
        else:
            print(f"    script failed to embed: {absolute_js}")

    # ---------------------------------------------------------
    # 6. External preload/modulepreload/font/icon links.
    #    Preload hints are unnecessary once the actual resource is embedded.
    #    Remote favicons/icons are removed because they are not needed for the
    #    template rendering and would otherwise be external dependencies.
    # ---------------------------------------------------------
    for link in list(soup.find_all("link")):
        rel = [x.lower() for x in (link.get("rel") or [])]
        href = link.get("href")
        if not href:
            continue

        if any(x in rel for x in ["preload", "prefetch", "dns-prefetch", "preconnect", "modulepreload"]):
            link.decompose()
            continue

        if any(x in rel for x in ["icon", "shortcut icon", "apple-touch-icon"]):
            absolute = _normalise_asset_url(href, actual_page_url)
            data_url = embed_url(href) if absolute else None
            if data_url:
                link["href"] = data_url
            else:
                link.decompose()

    # ---------------------------------------------------------
    # 7. Remove base URL so local file loading cannot redirect relative
    #    references back to Wix.
    # ---------------------------------------------------------
    for base in soup.find_all("base"):
        base.decompose()

    # Add local metadata.
    head = soup.head
    if head is None:
        head = soup.new_tag("head")
        if soup.html:
            soup.html.insert(0, head)
        else:
            html_tag = soup.new_tag("html")
            for child in list(soup.contents):
                html_tag.append(child.extract())
            html_tag.insert(0, head)
            soup = html_tag

    meta_charset = soup.new_tag("meta")
    meta_charset["charset"] = "utf-8"
    head.insert(0, meta_charset)

    meta_viewport = soup.new_tag("meta")
    meta_viewport["name"] = "viewport"
    meta_viewport["content"] = "width=device-width, initial-scale=1"
    head.insert(1, meta_viewport)

    meta_generator = soup.new_tag("meta")
    meta_generator["name"] = "generator"
    meta_generator["content"] = "Wix template offline crawler v3"
    head.insert(2, meta_generator)

    return str(soup)


def validate_offline_html(html_path):
    """
    Open the generated HTML in a fresh Chrome profile with network disabled.

    Returns a dict with:
      - pass/fail
      - broken images
      - external resources observed in the DOM/performance entries
      - page title and basic dimensions

    This is intentionally a real browser test, not just a regex check.
    """
    result = {
        "offline_status": "SKIPPED",
        "broken_images": [],
        "external_resources": [],
        "external_dom_urls": [],
        "title": "",
        "width": 0,
        "height": 0,
        "error": "",
    }

    if not OFFLINE_VALIDATION:
        return result

    validator = None
    try:
        options = Options()
        options.add_argument("--headless=new")
        options.add_argument(f"--window-size={WINDOW_SIZE[0]},{WINDOW_SIZE[1]}")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-background-networking")
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-sync")
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

        service = Service(ChromeDriverManager().install())
        validator = webdriver.Chrome(service=service, options=options)
        validator.set_page_load_timeout(OFFLINE_VALIDATION_TIMEOUT)

        validator.execute_cdp_cmd("Network.enable", {})
        # Block only web protocols. file:// must remain loadable.
        validator.execute_cdp_cmd("Network.setBlockedURLs", {
            "urls": ["http://*/*", "https://*/*", "ws://*/*", "wss://*/*"]
        })

        file_url = Path(html_path).resolve().as_uri()
        try:
            validator.get(file_url)
        except TimeoutException:
            # A page can continue rendering even if some script never settles.
            pass

        time.sleep(2.0)

        # Allow lazy image observers to settle once the local document is up.
        try:
            validator.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(1.0)
            validator.execute_script("window.scrollTo(0, 0);")
            time.sleep(0.5)
        except Exception:
            pass

        # Image integrity: every image should have loaded bytes.
        broken = validator.execute_script("""
            return Array.from(document.images).map((img, i) => ({
                index: i,
                src: img.currentSrc || img.src || '',
                complete: img.complete,
                naturalWidth: img.naturalWidth,
                naturalHeight: img.naturalHeight
            })).filter(x => !x.complete || x.naturalWidth === 0);
        """)
        result["broken_images"] = broken or []

        # Find web URLs left in actual resource-bearing DOM attributes.
        external_dom = validator.execute_script("""
            const attrs = ['src','href','poster','data-src','data-lazy-src',
                           'data-original','data-image-url','srcset','data-srcset'];
            const out = new Set();
            for (const el of document.querySelectorAll('*')) {
                for (const a of attrs) {
                    if (!el.hasAttribute(a)) continue;
                    const value = el.getAttribute(a) || '';
                    if (/^https?:\\/\\//i.test(value) || /^\\/\\//.test(value)) {
                        out.add(value);
                    }
                    if (a.includes('srcset')) {
                        for (const part of value.split(',')) {
                            const u = (part.trim().split(/\\s+/)[0] || '');
                            if (/^https?:\\/\\//i.test(u) || /^\\/\\//.test(u)) out.add(u);
                        }
                    }
                }
            }
            for (const s of document.querySelectorAll('style')) {
                const css = s.textContent || '';
                const matches = css.match(/url\\(\\s*["']?(https?:\\/\\/|\\/\\/)[^)]*/gi) || [];
                for (const m of matches) out.add(m);
            }
            return Array.from(out);
        """)
        result["external_dom_urls"] = external_dom or []

        # Network log catches resources attempted by JS/CSS that aren't obvious
        # in DOM attributes. With web protocols blocked, these are useful proof
        # that the file is not fully self-contained.
        external_resources = []
        try:
            logs = validator.get_log("performance")
            for entry in logs:
                try:
                    msg = json.loads(entry["message"])["message"]
                    if msg.get("method") != "Network.requestWillBeSent":
                        continue
                    url = msg.get("params", {}).get("request", {}).get("url", "")
                    if url.startswith(("http://", "https://", "ws://", "wss://")):
                        external_resources.append(url)
                except Exception:
                    pass
        except Exception:
            pass
        result["external_resources"] = sorted(set(external_resources))

        result["title"] = validator.title
        result["width"], result["height"] = validator.execute_script(
            "return [document.documentElement.scrollWidth, document.documentElement.scrollHeight];"
        )

        result["offline_status"] = (
            "PASS"
            if not result["broken_images"]
            and not result["external_dom_urls"]
            and not result["external_resources"]
            else "FAIL"
        )
        return result

    except Exception as e:
        result["offline_status"] = "ERROR"
        result["error"] = str(e)
        return result
    finally:
        if validator is not None:
            try:
                validator.quit()
            except Exception:
                pass


def save_self_contained_html(driver, out_path, page_url):
    """Save one self-contained HTML file and verify it offline immediately."""
    html = _make_html_fully_self_contained(driver, page_url)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("<!DOCTYPE html>\n")
        f.write(html)

    validation = validate_offline_html(out_path)
    return out_path, validation


def screenshot_card(driver, listing_slug, card):
    """
    Kept under the old function name so the crawler flow remains familiar,
    but now produces ONE self-contained HTML file instead of PNG.
    """
    view_url = card["view_url"]
    wh_id = card["wh_id"]
    title = card.get("title", "")

    name_part = slugify(title) if title else wh_id
    out_dir = os.path.join(OUTPUT_DIR, listing_slug)
    ensure_dir(out_dir)

    out_file = os.path.join(
        out_dir,
        f"{wh_id}__{name_part}.html"
    )

    live_url = get_live_template_url(driver, view_url)

    if live_url:
        driver.get(live_url)
        time.sleep(1.5)
        dismiss_dialog_if_present(driver)
    else:
        dismiss_dialog_if_present(driver)

    out_file, validation = save_self_contained_html(
        driver,
        out_file,
        live_url or view_url
    )

    return out_file, live_url or "", validation


def run():
    ensure_dir(OUTPUT_DIR)
    done = load_done_set()
    driver = make_driver()

    try:
        for listing_url in LISTING_URLS:
            listing_slug = slugify(listing_url)
            print(f"\n=== Listing: {listing_url} ===")

            driver.get(listing_url)
            time.sleep(1.5)
            last_page = get_last_page_number(driver)
            print(f"  detected {last_page} page(s)")

            all_cards = {}
            for page_num in range(1, last_page + 1):
                page_url = listing_url if page_num == 1 else f"{listing_url.rstrip('/')}/{page_num}"
                print(f"  scanning page {page_num}: {page_url}")
                try:
                    driver.get(page_url)
                    time.sleep(1.5)
                except TimeoutException:
                    print("  timed out, skipping page")
                    continue

                cards = collect_cards(driver)
                cards = enrich_titles(driver, cards)
                for c in cards:
                    all_cards[c["wh_id"]] = c

            print(f"  found {len(all_cards)} unique templates for this listing")

            for card in all_cards.values():
                view_url = card["view_url"]
                if view_url in done:
                    print(f"  skip (already done): {view_url}")
                    continue
                try:
                    out_file, live_url, validation = screenshot_card(driver, listing_slug, card)
                    print(f"  saved: {out_file}")
                    print(
                        f"  offline test: {validation['offline_status']} | "
                        f"broken images: {len(validation['broken_images'])} | "
                        f"external resources: {len(set(validation['external_resources']))}"
                    )

                    status = "OK" if validation["offline_status"] == "PASS" else "OFFLINE_FAILED"
                    append_log(
                        listing_url, card["wh_id"], card.get("title", ""),
                        view_url, live_url, status, out_file,
                    )
                    if status != "OK":
                        print("  WARNING: template was saved but did not pass offline validation.")
                        if validation["broken_images"]:
                            print(f"    broken images: {validation['broken_images'][:5]}")
                        if validation["external_dom_urls"]:
                            print(f"    external DOM URLs: {validation['external_dom_urls'][:5]}")
                        if validation["external_resources"]:
                            print(f"    external network URLs: {list(sorted(set(validation['external_resources'])))[:5]}")
                except Exception as e:
                    print(f"  FAILED: {view_url} -> {e}")
                    append_log(
                        listing_url, card["wh_id"], card.get("title", ""),
                        view_url, "", "FAILED",
                    )
                time.sleep(DELAY_BETWEEN_SHOTS_SEC)
    finally:
        driver.quit()

    print("\nDone. See screenshots/ (one self-contained HTML file per template) and screenshots/log.csv")



# ================================================================
# V4 OUTPUT MODE: normal HTML + local assets folders
# ================================================================
import hashlib
from pathlib import Path
from urllib.parse import unquote

V4_TRACKING = (
    "google-analytics", "googletagmanager", "doubleclick",
    "facebook.com/tr", "hotjar", "clarity.ms", "segment.io",
    "mixpanel", "sentry.io"
)
V4_MIME_EXT = {
    "text/css": ".css", "text/javascript": ".js", "application/javascript": ".js",
    "application/x-javascript": ".js", "image/jpeg": ".jpg", "image/png": ".png",
    "image/gif": ".gif", "image/webp": ".webp", "image/avif": ".avif",
    "image/svg+xml": ".svg", "image/x-icon": ".ico", "font/woff": ".woff",
    "font/woff2": ".woff2", "font/ttf": ".ttf", "font/otf": ".otf",
    "application/vnd.ms-fontobject": ".eot"
}

def _v4_split_srcset(value):
    """Split responsive-image candidates while preserving commas inside CDN URLs."""
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


def _v4_wix_url_candidates(url):
    """Return safe fallbacks for Wix transformed or split-image URLs.

    Wix transformed images commonly use `/media/file.jpg/v1/fill/w_...`. If the
    transformed URL is blocked, the untransformed media file may still be public.
    Some malformed srcset entries also resolve to `/demone2/quality_auto/file.jpg`;
    in that narrow case try the matching Wix media URL rather than inventing a page URL.
    """
    candidates = []
    try:
        parsed = urlparse(url)
        path = unquote(parsed.path or "")
        lower_host = (parsed.hostname or "").lower()
        image_pattern = re.compile(r"([^/]+\.(?:jpe?g|png|gif|webp|avif|svg|ico))(?:/|$)", re.I)
        matches = list(image_pattern.finditer(path))
        if lower_host.endswith("wixstatic.com") and "/media/" in path and matches:
            match = matches[-1]
            original_path = path[:match.start(1) + len(match.group(1))]
            # Strip only the Wix transformation suffix after the real filename.
            original = urlunparse((parsed.scheme or "https", parsed.netloc, original_path,
                                   "", parsed.query, ""))
            if original != url:
                candidates.append(original)
        elif lower_host == "www.wix.com" and path.lower().startswith("/demone2/") and matches:
            filename = matches[-1].group(1)
            if "/" not in filename and filename:
                candidates.append("https://static.wixstatic.com/media/" + filename)
    except Exception:
        return [url]
    out = [url]
    for candidate in candidates:
        if candidate not in out:
            out.append(candidate)
    return out


def _v4_is_orphaned_wix_transform(url):
    """Recognize transform tokens accidentally promoted to URLs by a bad split."""
    try:
        parsed = urlparse(url)
        if (parsed.hostname or "").lower() != "www.wix.com":
            return False
        parts = [p for p in (parsed.path or "").split("/") if p]
        if not parts or parts[0].lower() != "demone2":
            return False
        if any(re.search(r"\.(?:jpe?g|png|gif|webp|avif|svg|ico)$", p, re.I) for p in parts):
            return False
        transform = re.compile(r"^(?:h_\d+|w_\d+|q_\d+|fp_[\d.]+|al_c|enc_avif|quality_auto|usm_[\d.]+(?:_[\d.]+)*|fill|crop|fit)$", re.I)
        return len(parts) > 1 and all(transform.match(p) for p in parts[1:])
    except Exception:
        return False


def v4_category(url, mime=""):
    low=url.lower(); path=urlparse(url).path.lower(); mime=(mime or "").split(";",1)[0].lower()
    if any(x in low for x in V4_TRACKING): return "skip"
    if mime == "text/css" or path.endswith(".css"): return "css"
    if mime in ("text/javascript","application/javascript","application/x-javascript") or path.endswith((".js",".mjs")): return "js"
    if mime.startswith("image/") or path.endswith((".jpg",".jpeg",".png",".gif",".webp",".avif",".svg",".ico")): return "images"
    if mime.startswith("font/") or path.endswith((".woff",".woff2",".ttf",".otf",".eot")): return "fonts"
    return "other"

def v4_name(url,mime,category):
    raw=unquote(Path(urlparse(url).path).name)
    raw=re.sub(r"[^A-Za-z0-9._-]+","_",raw).strip("._") or category.rstrip("s") or "asset"
    ext=Path(raw).suffix.lower() or V4_MIME_EXT.get((mime or "").split(";",1)[0].lower(),"")
    h=hashlib.sha1(url.encode()).hexdigest()[:10]
    return f"{Path(raw).stem[:70]}_{h}{ext}"

def _v4_find_browser_resource(url, resources):
    """Resolve stale Wix page-origin URLs to the successful CDN response Chrome loaded."""
    key = url.split("#", 1)[0]

    def good(info):
        try:
            status = float(info.get("status"))
            return 200 <= status < 300 and bool(info.get("request_id"))
        except (TypeError, ValueError, AttributeError):
            return bool(info and info.get("request_id"))

    exact = resources.get(key)
    if exact and good(exact):
        return key, exact
    try:
        target = urlparse(key)
        path = target.path or "/"
        query = target.query
        embedded = re.findall(
            r"([^/]+\.(?:css|mjs|js|jpe?g|png|gif|webp|avif|svg|ico|woff2?|ttf|otf|eot|mp4|webm|mp3|ogg|mov))(?:/|$)",
            unquote(path), re.I)
        basename = (embedded[-1] if embedded else Path(path).name).lower()
        extension = Path(basename).suffix.lower()
    except Exception:
        return key, None

    valid = [(candidate, info) for candidate, info in resources.items() if good(info)]
    matches = []
    for candidate, info in valid:
        try:
            parsed = urlparse(candidate.split("#", 1)[0])
            if (parsed.path or "/") == path and parsed.query == query:
                matches.append((candidate, info))
        except Exception:
            continue
    if len(matches) == 1:
        return matches[0]

    if len(path) >= 18:
        matches = []
        for candidate, info in valid:
            try:
                if (urlparse(candidate.split("#", 1)[0]).path or "/").endswith(path):
                    matches.append((candidate, info))
            except Exception:
                continue
        if len(matches) == 1:
            return matches[0]

    # Some Wix builds move the file below a service/version directory, changing
    # everything except the content-hashed filename.
    matches = []
    for candidate, info in valid:
        try:
            parsed = urlparse(candidate.split("#", 1)[0])
            candidate_path = unquote(parsed.path or "")
            candidate_embedded = re.findall(
                r"([^/]+\.(?:css|mjs|js|jpe?g|png|gif|webp|avif|svg|ico|woff2?|ttf|otf|eot|mp4|webm|mp3|ogg|mov))(?:/|$)",
                candidate_path, re.I)
            candidate_name = (candidate_embedded[-1] if candidate_embedded else Path(candidate_path).name).lower()
            if candidate_name != basename:
                continue
            mime = (info.get("mime_type") or "").lower()
            if extension in (".js", ".mjs") and mime and "javascript" not in mime:
                continue
            if extension == ".css" and mime and "css" not in mime:
                continue
            matches.append((candidate, info))
        except Exception:
            continue
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1 and extension in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"):
        wanted_width_match = re.search(r"(?:^|[/,_-])(?:w_|width[=/])([0-9]{2,5})", path, re.I)
        if wanted_width_match:
            wanted_width = int(wanted_width_match.group(1))
            scored = []
            for candidate, info in matches:
                candidate_path = urlparse(candidate).path
                cm = re.search(r"(?:^|[/,_-])(?:w_|width[=/])([0-9]{2,5})", candidate_path, re.I)
                if cm:
                    scored.append((abs(int(cm.group(1)) - wanted_width), candidate, info))
            if scored:
                best_score = min(x[0] for x in scored)
                best = [x for x in scored if x[0] == best_score]
                if len(best) == 1:
                    return best[0][1], best[0][2]
    return key, None

def v4_download(driver, session, url, cache, referer, resources):
    """Download one asset, preferring browser bytes and trying safe Wix originals."""
    key = url.split("#", 1)[0]
    if key in cache:
        return cache[key]
    if _v4_is_orphaned_wix_transform(key):
        cache[key] = None
        return None

    candidates = _v4_wix_url_candidates(key)

    # First search successful Chrome responses for both the requested transform and
    # its untransformed original. Chrome can receive image bytes even when a second
    # requests call is blocked by CDN hotlink protection.
    for candidate in candidates:
        browser_key, info = _v4_find_browser_resource(candidate, resources)
        if not info:
            continue
        raw, mime = _get_browser_body(driver, info)
        if raw is None:
            continue
        candidate_mime = mime or _guess_mime_type(browser_key)
        head = (raw or b"")[:2048].lstrip().lower()
        is_html = (head.startswith((b"<!doctype html", b"<html", b"<head", b"<body", b"<title"))
                   or candidate_mime.split(";", 1)[0].strip().lower() == "text/html")
        if is_html:
            continue
        if raw:
            cache[key] = (raw, candidate_mime)
            return cache[key]

    last = None
    for index, candidate in enumerate(candidates):
        # Don't spend the full retry budget on each fallback URL. A transformed
        # URL gets one retry; an original-image fallback is tried once promptly.
        attempts = 2 if index == 0 else 1
        for attempt in range(attempts):
            try:
                r = session.get(candidate, headers={"Referer": referer, "Accept": "*/*"}, timeout=(8, 20))
                r.raise_for_status()
                body = r.content
                ct = r.headers.get("Content-Type", "")
                head = (body or b"")[:2048].lstrip().lower()
                is_html = (head.startswith((b"<!doctype html", b"<html", b"<head", b"<body", b"<title"))
                           or ct.split(";", 1)[0].strip().lower() == "text/html")
                if is_html:
                    raise ValueError("HTML/challenge page returned for a static asset")
                cache[key] = (body, _guess_mime_type(candidate, ct))
                return cache[key]
            except Exception as e:
                last = e
                if attempt + 1 < attempts:
                    time.sleep(0.6)
    print(f"    asset failed: {key} -> {str(last)[:160]}")
    cache[key] = None
    return None

def v4_save(package,category,raw,mime,url,used):
    folder=Path(package)/"assets"/category; folder.mkdir(parents=True,exist_ok=True)
    name=v4_name(url,mime,category); stem=Path(name).stem; ext=Path(name).suffix
    candidate=name; n=2
    while candidate in used or (folder/candidate).exists():
        candidate=f"{stem}_{n}{ext}"; n+=1
    used.add(candidate); p=folder/candidate; p.write_bytes(raw); return p

def v4_rewrite_css(css,css_url,save,from_dir="assets/css"):
    """Rewrite CSS asset URLs. `from_dir` is the local folder containing this CSS.

    External stylesheet files live in assets/css; inline CSS lives in index.html and
    therefore must use paths relative to the package root. Mixing these bases was
    causing inline background images to point at a non-existent ../images path.
    """
    pat=re.compile(r"url\(\s*([\"']?)(.*?)\1\s*\)",re.I|re.S)
    def local_path(raw):
        absolute=_normalise_asset_url(raw,css_url)
        local=save(absolute,"css") if absolute else None
        if not local: return None
        return os.path.relpath(local,start=from_dir or ".").replace("\\","/")
    def repl(m):
        raw=m.group(2).strip()
        if not raw or raw.startswith(("data:","blob:","#")): return m.group(0)
        rel=local_path(raw)
        if not rel: return m.group(0)
        return f"url({m.group(1)}{rel}{m.group(1)})"
    css=pat.sub(repl,css)
    # CSS also permits @import "file.css" without wrapping the URL in url(...).
    imp=re.compile(r"@import\s+([\"'])([^\"']+)\1",re.I)
    def import_repl(m):
        raw=m.group(2).strip()
        rel=local_path(raw)
        return f"@import {m.group(1)}{rel}{m.group(1)}" if rel else m.group(0)
    return imp.sub(import_repl,css)

def v4_rewrite_js_imports(js_text, js_url, js_local, package_dir, save_asset):
    # Follow only real import/export module specifiers, not arbitrary URL strings
    # (API endpoints, navigation links, analytics payloads). The save callback is
    # save_asset(absolute_url, context, base_url).
    module_extensions = (".js", ".mjs", ".css")
    patterns = [
        # import("./lazy-chunk.js")
        re.compile(r"(?P<prefix>\bimport\s*\(\s*)(?P<quote>['\"])(?P<url>[^'\"]+)(?P=quote)(?P<suffix>\s*\))"),
        # import "./side-effect.js"
        re.compile(r"(?P<prefix>\bimport\s*)(?P<quote>['\"])(?P<url>[^'\"]+)(?P=quote)"),
        # import { value } from "./module.js" / export { value } from "./module.js"
        re.compile(r"(?P<prefix>\b(?:import|export)\b[^;\n]*?\bfrom\s*)(?P<quote>['\"])(?P<url>[^'\"]+)(?P=quote)"),
    ]
    package = Path(package_dir)
    source_path = package / Path(js_local)

    def replace(match):
        raw = match.group("url").strip()
        # Bare npm/browser specifiers (e.g. "react") are not downloadable URLs.
        if not raw.startswith(("./", "../", "/", "//", "http://", "https://")):
            return match.group(0)
        absolute = _normalise_asset_url(raw, js_url)
        if not absolute:
            return match.group(0)
        try:
            ext = Path(urlparse(absolute).path).suffix.lower()
        except Exception:
            return match.group(0)
        if ext not in module_extensions:
            return match.group(0)
        local = save_asset(absolute, "JavaScript module import", js_url)
        if not local:
            return match.group(0)
        target_path = package / Path(local)
        try:
            target_path.resolve().relative_to(package.resolve())
        except (OSError, ValueError):
            return match.group(0)
        relative = os.path.relpath(target_path, start=source_path.parent).replace("\\", "/")
        if not relative.startswith(("./", "../", "/")):
            relative = "./" + relative
        return (match.group("prefix") + match.group("quote") + relative +
                match.group("quote") + match.groupdict().get("suffix", ""))

    rewritten = js_text or ""
    for pattern in patterns:
        rewritten = pattern.sub(replace, rewritten)
    return rewritten


def _v4_build_package(driver, page_url, package_dir):
    # Save the rendered page and its static dependencies as a local asset package.
    scroll_full_page(driver)
    time.sleep(1.2)
    html = driver.execute_script("return document.documentElement.outerHTML;")
    actual = driver.current_url or page_url
    soup = BeautifulSoup(html, "html.parser")
    base_tag = soup.find("base", href=True)
    asset_base = urljoin(actual, base_tag["href"]) if base_tag else actual
    session = _build_authenticated_session(driver)
    resources = _browser_resource_map(driver)
    cache, saved_urls, used, manifest, failed_assets = {}, {}, set(), [], []
    package = Path(package_dir)
    package.mkdir(parents=True, exist_ok=True)
    for category in ("css", "js", "images", "fonts", "other", "media"):
        (package / "assets" / category).mkdir(parents=True, exist_ok=True)

    def save(raw_url, context="", base=asset_base):
        absolute = _normalise_asset_url(raw_url, base)
        if not absolute:
            return None
        absolute = absolute.split("#", 1)[0]
        # These are stray Wix image-transform tokens accidentally emitted as URLs,
        # not retrievable files. Skip them rather than recording misleading failures.
        if _v4_is_orphaned_wix_transform(absolute):
            return None
        if absolute in saved_urls:
            return saved_urls[absolute]
        category = v4_category(absolute, resources.get(absolute, {}).get("mime_type", ""))
        if category == "skip":
            return None
        result = v4_download(driver, session, absolute, cache, base, resources)
        if not result:
            if not any(x["url"] == absolute for x in failed_assets):
                failed_assets.append({"url": absolute, "context": context or "asset", "reason": "not retrievable from browser cache or HTTP"})
            return None
        raw, mime = result
        category = v4_category(absolute, mime)
        if category == "skip":
            return None
        path = v4_save(package, category, raw, mime, absolute, used)
        local = os.path.relpath(path, start=package).replace("\\", "/")
        # Register before walking CSS to prevent @import loops.
        saved_urls[absolute] = local
        if category == "css":
            css = raw.decode("utf-8", errors="replace")
            css = v4_rewrite_css(css, absolute, lambda u, c="CSS dependency": save(u, c, absolute))
            path.write_text(css, encoding="utf-8")
        elif category == "js":
            js_text = raw.decode("utf-8", errors="replace")
            js_text = v4_rewrite_js_imports(
                js_text, absolute, local, package,
                lambda u, c="JavaScript module import", b=absolute: save(u, c, b),
            )
            path.write_text(js_text, encoding="utf-8")
        manifest.append({"url": absolute, "local": local, "category": category,
                         "mime": mime or _guess_mime_type(absolute), "bytes": path.stat().st_size})
        return local

    # Remove analytics/pixel elements before asset rewriting. Keep functional external
    # embeds (maps/video/forms) visible in the audit rather than deleting page content.
    removed_trackers = []
    for tag in list(soup.find_all(["script", "img", "iframe", "link", "source", "video", "audio", "noscript"])):
        refs = [tag.get(a) for a in ("src", "href", "poster", "data-src", "data-original") if tag.get(a)]
        if any(any(marker in urljoin(asset_base, ref).lower() for marker in V4_TRACKING) for ref in refs):
            removed_trackers.extend(urljoin(asset_base, ref) for ref in refs
                                    if any(marker in urljoin(asset_base, ref).lower() for marker in V4_TRACKING))
            tag.decompose()

    # External stylesheets and icons: use the same save path as assets discovered from CSS.
    for link in list(soup.find_all("link")):
        rels = [x.lower() for x in (link.get("rel") or [])]
        href = link.get("href")
        if not href:
            continue
        if any(x in rels for x in ("preload", "prefetch", "dns-prefetch", "preconnect", "modulepreload", "prerender")):
            link.decompose()
            continue
        if "stylesheet" in rels or any(x in rels for x in ("icon", "apple-touch-icon")):
            local = save(href, "stylesheet" if "stylesheet" in rels else "icon", asset_base)
            if local:
                link["href"] = local
                link.attrs.pop("integrity", None)
                link.attrs.pop("crossorigin", None)

    # HTML, lazy images, responsive srcsets, video posters and SVG-linked images.
    for tag in soup.find_all(True):
        name = tag.name.lower()
        if name in ("script", "link", "base", "iframe"):
            continue
        for attr in ("src", "data-src", "data-lazy-src", "data-original", "data-original-src",
                     "data-image-url", "data-background-image", "data-bg-image", "data-fallback-src",
                     "data-bg", "data-background", "data-bg-src", "data-lazy-background",
                     "data-poster", "poster", "data-poster-url", "data-lazyload"):
            if not tag.has_attr(attr):
                continue
            raw_url = tag.get(attr)
            local = save(raw_url, f"{name}:{attr}", asset_base)
            if local:
                tag[attr] = local
        if name in ("image", "use"):
            for attr in ("href", "xlink:href"):
                raw_url = tag.get(attr)
                if raw_url and not raw_url.startswith("#"):
                    local = save(raw_url, f"{name}:{attr}", asset_base)
                    if local:
                        tag[attr] = local
        for attr in ("srcset", "data-srcset", "data-lazy-srcset", "imagesrcset", "data-imagesrcset"):
            if not tag.has_attr(attr):
                continue
            vals = []
            for raw_url, descriptor in _v4_split_srcset(tag.get(attr) or ""):
                local = save(raw_url, f"{name}:{attr}", asset_base)
                if local:
                    raw_url = local
                vals.append((raw_url + (" " + descriptor if descriptor else "")).strip())
            tag[attr] = ", ".join(vals)
        if tag.has_attr("style"):
            tag["style"] = v4_rewrite_css(tag.get("style") or "", asset_base,
                lambda u, c="css": save(u, f"{name}:style", asset_base), from_dir=".")

    removed_inline_css_locators = []
    for style in list(soup.find_all("style")):
        css = style.get_text() or ""
        source_locator = style.get("data-href") or style.get("data-url")
        # Relative url(...) values inside inline CSS can still be relative to the original
        # stylesheet URL when Wix copied that stylesheet's content into the DOM.
        css_base_url = source_locator or asset_base
        if source_locator and css.strip():
            # Wix often leaves the original stylesheet URL as inert metadata after its
            # contents have already been injected into this style tag. Do not download a
            # duplicate stylesheet or turn it into a second active CSS import.
            removed_inline_css_locators.append({
                "url": source_locator,
                "reason": "stylesheet content is already embedded in this style element",
            })
            style.attrs.pop("data-href", None)
            style.attrs.pop("data-url", None)
        elif source_locator and not css.strip():
            # If only the external locator remains, fetch it and replace the empty style
            # element with a real local stylesheet link.
            local_css = save(source_locator, "empty style data-href stylesheet", asset_base)
            if local_css:
                link_tag = soup.new_tag("link", rel="stylesheet", href=local_css)
                style.replace_with(link_tag)
                continue
        css = v4_rewrite_css(css, css_base_url, lambda u, c="inline CSS dependency": save(u, c, css_base_url), from_dir=".")
        style.clear()
        style.append(css)

    # Scripts are only retained locally when their actual bytes can be recovered.
    # The rendered DOM has already been captured; a dead remote bootstrap script must
    # not be left in the saved file to overwrite that DOM or trigger a network fetch.
    for script in list(soup.find_all("script", src=True)):
        src = script.get("src")
        local = save(src, "script", asset_base)
        if local:
            script["src"] = local
            script.attrs.pop("integrity", None)
            script.attrs.pop("crossorigin", None)
        else:
            script.decompose()

    # Capture static assets Chrome actually loaded, including runtime fonts/images and
    # dynamically inserted CSS/JS that no longer has a direct DOM reference.
    for resource_url, info in resources.items():
        try:
            status = float(info.get("status"))
            if not (200 <= status < 300):
                continue
        except (TypeError, ValueError):
            pass
        mime = (info.get("mime_type") or "").lower().split(";", 1)[0].strip()
        path = urlparse(resource_url).path.lower()
        static_mime = (mime.startswith(("image/", "font/", "audio/", "video/")) or
                       mime in ("text/css", "text/javascript", "application/javascript", "application/x-javascript"))
        static_ext = path.endswith((".css", ".js", ".mjs", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".svg", ".ico",
                                    ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp4", ".webm", ".mp3", ".ogg", ".mov"))
        if static_mime or static_ext:
            save(resource_url, "runtime resource", actual)

    for base_tag in soup.find_all("base"):
        base_tag.decompose()
    head = soup.head
    if head is None:
        if soup.html:
            head = soup.new_tag("head")
            soup.html.insert(0, head)
        else:
            html_tag = soup.new_tag("html")
            head = soup.new_tag("head")
            html_tag.append(head)
            for child in list(soup.contents):
                html_tag.append(child.extract())
            soup = html_tag
    if head:
        if not head.find("meta", attrs={"charset": True}):
            meta = soup.new_tag("meta")
            meta["charset"] = "utf-8"
            head.insert(0, meta)
        if not head.find("meta", attrs={"name": "viewport"}):
            meta = soup.new_tag("meta")
            meta["name"] = "viewport"
            meta["content"] = "width=device-width, initial-scale=1"
            head.append(meta)

    index = package / "index.html"
    index.write_text("<!DOCTYPE html>\n" + str(soup), encoding="utf-8")
    (package / "asset-manifest.json").write_text(json.dumps({
        "source_url": actual, "asset_count": len(manifest), "assets": manifest,
        "failed": failed_assets, "removed_trackers": sorted(set(removed_trackers)),
        "removed_inline_css_locators": removed_inline_css_locators,
        "dependency_localization": {
            "css_imports_and_urls_followed": True,
            "javascript_static_and_dynamic_imports_followed": True,
            "ordinary_navigation_urls_downloaded_as_assets": False,
        },
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(index), manifest

def _v4_validate(package_dir):
    """Validate active local dependencies in HTML, inline CSS, linked CSS and embeds."""
    package = Path(package_dir)
    index = package / "index.html"
    errors = []
    if not index.exists():
        return False, ["index.html missing"]
    soup = BeautifulSoup(index.read_text(encoding="utf-8", errors="replace"), "html.parser")

    def check_ref(where, value, base_dir):
        value = (value or "").strip().strip("\"'")
        if not value or value.startswith(("data:", "blob:", "#", "javascript:", "mailto:", "tel:")):
            return
        if value.startswith(("http:", "https:", "//")):
            errors.append(f"external {where}: {value}")
            return
        path = value.split("#", 1)[0].split("?", 1)[0]
        if path and not (base_dir / unquote(path)).exists():
            errors.append(f"missing {where}: {value}")

    css_url_pattern = re.compile(r"url\(\s*([\"']?)(.*?)\1\s*\)", re.I | re.S)
    css_import_pattern = re.compile(r"@import\s+(?:url\()?\s*([\"'])(.*?)\1\s*\)?", re.I | re.S)

    def check_css(css_text, base_dir, where):
        for match in css_url_pattern.finditer(css_text or ""):
            check_ref(f"CSS asset ({where})", match.group(2), base_dir)
        for match in css_import_pattern.finditer(css_text or ""):
            check_ref(f"CSS import ({where})", match.group(2), base_dir)

    resource_attrs = (
        "src", "poster", "data-src", "data-lazy-src", "data-original", "data-original-src",
        "data-image-url", "data-background-image", "data-bg-image", "data-fallback-src",
        "data-bg", "data-background", "data-bg-src", "data-lazy-background", "data-poster",
        "data-poster-url", "data-lazyload",
    )
    srcset_attrs = ("srcset", "data-srcset", "data-lazy-srcset", "imagesrcset", "data-imagesrcset")

    for tag in soup.find_all(True):
        if tag.name == "iframe":
            iframe_src = tag.get("src", "")
            if iframe_src.startswith(("http:", "https:", "//")):
                errors.append(f"external iframe dependency (review; may be map/video/form content): {iframe_src}")
        for attr in resource_attrs:
            if tag.has_attr(attr) and not (tag.name == "iframe" and attr == "src"):
                check_ref(f"{tag.name}[{attr}]", tag.get(attr), package)
        for attr in srcset_attrs:
            for url, _descriptor in _v4_split_srcset(tag.get(attr) or ""):
                check_ref(f"{tag.name}[{attr}]", url, package)
        if tag.name in ("image", "use"):
            for attr in ("href", "xlink:href"):
                value = tag.get(attr)
                if value and not value.startswith("#"):
                    check_ref(f"{tag.name}[{attr}]", value, package)
        if tag.name == "link":
            rels = [x.lower() for x in (tag.get("rel") or [])]
            if any(x in rels for x in ("stylesheet", "icon", "apple-touch-icon")):
                check_ref("link resource", tag.get("href"), package)
        if tag.has_attr("style"):
            check_css(tag.get("style") or "", package, "inline style")

    for style in soup.find_all("style"):
        check_css(style.get_text() or "", package, "style block")
    for css in (package / "assets" / "css").glob("*.css"):
        check_css(css.read_text(encoding="utf-8", errors="replace"), css.parent, css.name)
    return not errors, errors

def save_template_package(driver,out_dir,page_url):
    return _v4_build_package(driver,page_url,out_dir)

def screenshot_card(driver, listing_slug, card):
    view_url = card["view_url"]
    wh_id = card["wh_id"]
    title = card.get("title", "")
    name_part = slugify(title) if title else wh_id
    # Keep package folders directly under outputs/wix so the Studio inventory can see them.
    out_dir = os.path.join(OUTPUT_DIR, f"{wh_id}__{name_part}")
    ensure_dir(out_dir)
    live_url = get_live_template_url(driver, view_url)
    if live_url:
        _clear_browser_logs(driver)
        driver.get(live_url)
        try:
            WebDriverWait(driver, 15).until(lambda d: d.execute_script("return document.readyState") in ("interactive", "complete"))
        except Exception:
            pass
        time.sleep(1.0)
        dismiss_dialog_if_present(driver)
    else:
        dismiss_dialog_if_present(driver)
    index_path, manifest = save_template_package(driver, out_dir, live_url or view_url)
    passed, errors = _v4_validate(out_dir)
    Path(out_dir, "offline-validation.txt").write_text(("PASS\n" if passed else "PARTIAL\n") + "\n".join(errors), encoding="utf-8")
    # Keep the crawl-time cleanup auditable and provide a compact, structured input
    # for an optional LLM review instead of sending the whole website for rewriting.
    manifest_path = Path(out_dir, "asset-manifest.json")
    try:
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        manifest_data = {"assets": manifest}
    manifest_data["validation_errors"] = errors
    manifest_data["unresolved_external_dependencies"] = [e for e in errors if e.startswith("external ")]
    js_hosts = set()
    for js_path in (Path(out_dir) / "assets" / "js").glob("*"):
        if not js_path.is_file() or js_path.stat().st_size > 5 * 1024 * 1024:
            continue
        try:
            text = js_path.read_text(encoding="utf-8", errors="ignore")
            for host in re.findall(r"https?://([^/\s\"'<>`]+)", text, re.I):
                host = host.split(":", 1)[0].lower().strip(". ")
                if host and not host.endswith(("w3.org", "schema.org", "example.com")):
                    js_hosts.add(host)
        except Exception:
            pass
    manifest_data["remote_hosts_mentioned_in_local_js_for_review"] = sorted(js_hosts)
    manifest_path.write_text(json.dumps(manifest_data, indent=2, ensure_ascii=False), encoding="utf-8")
    status = "completed" if passed else "partial"
    warnings = errors[:30]
    Path(out_dir, "metadata.json").write_text(json.dumps({
        "provider": "Wix", "template_id": wh_id, "status": status,
        "title": title or wh_id, "template_url": view_url,
        "preview_url": live_url or view_url, "final_url": live_url or view_url,
        "captured_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(timespec="seconds"),
        "asset_count": len(manifest), "warnings": warnings,
        "unresolved_external_dependency_count": len(manifest_data.get("unresolved_external_dependencies", [])),
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    return out_dir, live_url or "", passed, len(manifest)

def recover_driver(driver):
    """After a renderer timeout the Chrome tab can be stuck. Try to reset it; if that fails, restart Chrome."""
    try:
        driver.switch_to.window(driver.window_handles[0])
        driver.set_page_load_timeout(10)
        driver.get("about:blank")
        driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
        return driver
    except Exception:
        print("  [studio] browser tab was stuck - restarting Chrome", flush=True)
        try: driver.quit()
        except Exception: pass
        return make_driver()

# Override run() so V4 status is logged as OK only when the package validates.
def run():
    ensure_dir(OUTPUT_DIR)
    done = load_done_set()
    force_refresh = os.environ.get("STUDIO_FORCE_REFRESH", "0") == "1"
    if force_refresh:
        done.clear()
        print("  refresh mode: completed Wix templates will be re-crawled", flush=True)
    driver = make_driver()
    try:
        for input_url in LISTING_URLS:
            listing_slug = slugify(input_url)[:80]
            print(f"\n=== Listing: {input_url} ===", flush=True)
            is_direct_template = bool(re.search(r"/(?:website-template/)?view/html/[^/?#]+", urlparse(input_url).path, re.I))
            if is_direct_template:
                cards = [{"view_url": input_url, "wh_id": wh_id_from_url(input_url), "title": ""}]
                all_cards = {cards[0]["wh_id"]: cards[0]}
                print("  direct Wix template URL detected; skipping listing discovery", flush=True)
            else:
                for attempt in (1, 2, 3):
                    try:
                        driver.get(input_url)
                        try:
                            WebDriverWait(driver, 15).until(lambda d: d.execute_script(
                                "return !!document.querySelector('a[data-hook=\"hover-panel__previewLink\"],a[href*=\"/view/html/\"]')"))
                        except Exception:
                            pass
                        break
                    except Exception as e:
                        print(f"  listing open issue (try {attempt}/3): {(str(e).strip().splitlines() or [''])[0][:140]}", flush=True)
                        if attempt == 3:
                            raise
                targets = get_pagination_targets(driver, input_url)
                last_page = max(get_last_page_number(driver), max(targets) if targets else 1)
                print(f"  detected {last_page} page(s)", flush=True)
                all_cards = {}
                for page_num in range(1, last_page + 1):
                    page_url = targets.get(page_num) or _fallback_page_url(input_url, page_num, targets)
                    print(f"  scanning page {page_num}: {page_url}", flush=True)
                    got = None
                    for attempt in (1, 2, 3):
                        try:
                            driver.get(page_url)
                            try:
                                WebDriverWait(driver, 12).until(lambda d: d.execute_script(
                                    "return !!document.querySelector('a[data-hook=\"hover-panel__previewLink\"],a[href*=\"/view/html/\"]')"))
                            except Exception:
                                pass
                            time.sleep(0.5)
                            got = enrich_titles(driver, collect_cards(driver))
                            _clear_browser_logs(driver)
                            break
                        except Exception as e:
                            print(f"  page {page_num} issue (try {attempt}/3): {(str(e).strip().splitlines() or [''])[0][:140]}", flush=True)
                    if got is None:
                        print(f"  skipping page {page_num} after 3 attempts", flush=True)
                        continue
                    if not got and page_num == 1:
                        print("  no template cards found with known selectors; the provider markup may have changed", flush=True)
                    for card in got:
                        all_cards[card["wh_id"]] = card
                print(f"  found {len(all_cards)} unique templates for this listing", flush=True)

            for card in all_cards.values():
                view_url = card["view_url"]
                if view_url in done:
                    print(f"  skip (already done): {view_url}", flush=True)
                    continue
                completed = False
                for attempt in (1, 2):
                    try:
                        out_dir, live_url, ok, count = screenshot_card(driver, listing_slug, card)
                        status = "OK" if ok else "OFFLINE_FAILED"
                        print(f"  saved: {out_dir} | assets: {count} | validation: {'OK' if ok else 'PARTIAL'}", flush=True)
                        append_log(input_url, card["wh_id"], card.get("title", ""), view_url, live_url, status, out_dir)
                        if not ok:
                            print("  WARNING: inspect offline-validation.txt and asset-manifest.json for misses", flush=True)
                        completed = True
                        break
                    except Exception as e:
                        first = (str(e).strip().splitlines() or [repr(e)])[0][:200]
                        try:
                            driver = recover_driver(driver)
                        except Exception:
                            pass
                        if attempt == 1:
                            print(f"  retrying template after error: {first}", flush=True)
                            continue
                        print(f"  FAILED: {view_url} -> {first}", flush=True)
                        append_log(input_url, card["wh_id"], card.get("title", ""), view_url, "", "FAILED")
                if completed:
                    done.add(view_url)
                time.sleep(DELAY_BETWEEN_SHOTS_SEC)
    finally:
        driver.quit()
    print("\nDone. Each Wix template is saved under outputs/wix/<template>/ with index.html, assets/, and metadata.", flush=True)

if __name__ == "__main__":
    run()
