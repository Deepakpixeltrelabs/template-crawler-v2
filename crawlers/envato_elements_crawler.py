"""
Envato Elements web-templates crawler  (provider crawler for Website Crawler Studio)

Site:     https://elements.envato.com/web-templates
Default:  https://elements.envato.com/web-templates       (used when the URL box is empty)

How the site works (checked against the live pages)
---------------------------------------------------
  * Listing pages:  /web-templates  (also /web-templates/site-templates, /web-templates/landing-page-templates,
    /web-templates/portfolio, /wordpress/themes ...).  Further pages are  ?page=2, ?page=3 ...
  * Each card links to an item page  https://elements.envato.com/<name>-<ID>   (ID = 6-8 capital letters/digits).
  * The item page has a "Live preview" button. It does NOT go to an Envato address: each author hosts the
    demo on their own domain (for example https://thememxpro.com/php-template/baosh/index.php?storefront=envato-elements).
    So, unlike the other providers, the preview host is different for every template.
  * Some items have no live preview (Figma / PSD kits, some email templates). They are logged as `no_preview`
    and skipped on later runs.

What it does
------------
  * Empty URL or any elements.envato.com listing page: reads listing pages one after another
    (?page=1, 2, 3 ...) until a page has nothing new, a page limit is reached, or the site stops answering.
  * An item page (https://elements.envato.com/<name>-<ID>) : crawls that one template.
  * For every item: reads the item page, takes the "Live preview" address, opens it in Chrome and saves
    that page as an offline package (the author's demo - what you see in the preview).

Notes
-----
  * Envato Elements is a very large catalogue (tens of thousands of web templates). MAX_LISTING_PAGES and
    MAX_TEMPLATES below let you crawl it in slices; finished templates are skipped on the next run.
  * If Envato answers plain HTTP requests with a block page, the crawler automatically reads the pages in
    a real Chrome instead (slower). If Chrome is blocked too, the log says so.
  * Please respect Envato's terms of use for the material you save; the crawler pauses between requests.

Run alone:  python envato_elements_crawler.py [<url> ...] [--out DIR]
Entry point used by runner.py:  crawl(urls, output_dir)
"""
import os
import re
import sys
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import crawler_core as core                     # noqa: E402
from crawler_core import FetchError, NoPreview, safe_slug, step   # noqa: E402

PROVIDER = "envato"
LABEL = "Envato Elements"
DEFAULT_URL = "https://elements.envato.com/web-templates"      # hard-coded default

MAX_LISTING_PAGES = 0          # 0 = keep going until a page has nothing new; set e.g. 5 to read only 5 pages
MAX_TEMPLATES = 0              # 0 = no limit; set a number to cap one run
DELAY_BETWEEN_PAGES_SEC = 1.5  # politeness delay between listing-page requests
EMPTY_PAGES_TO_STOP = 1        # stop after this many pages in a row without a new template

ITEM_PATH_RE = re.compile(r"^/([^/?#]+?)-([A-Z0-9]{6,8})/?$")
LIVE_TEXT_RE = re.compile(r"live\s*preview", re.I)


# ----------------------------------------------------------------- URL logic
def _is_envato_host(host):
    host = (host or "").lower()
    return host == "elements.envato.com"


def parse_item_url(url):
    """-> (name_slug, ID) for a recognised Envato product URL; listing links are excluded."""
    p = urlparse(url.strip())
    if not _is_envato_host(p.hostname):
        return None
    m = ITEM_PATH_RE.match(p.path)
    if m:
        return m.group(1), m.group(2)
    # A possible alternate route /item/<slug>/<ID> is safe to recognise only
    # when its terminal segment looks like a stable Envato item identifier.
    segments = [x for x in p.path.strip("/").split("/") if x]
    if len(segments) >= 2 and segments[-2].lower() == "item" and re.fullmatch(r"[A-Za-z0-9]{6,14}", segments[-1]):
        return safe_slug(segments[-3] if len(segments) >= 3 else "item"), segments[-1].upper()
    return None


def classify_url(url):
    """Classify listing vs item by host/path semantics with a conservative detail-page fallback."""
    p = urlparse(url.strip())
    if not _is_envato_host(p.hostname) or p.scheme not in ("http", "https"):
        return "unsupported", None
    path = (p.path or "/").lower().rstrip("/") or "/"
    listing = (path == "/" or path.startswith("/web-templates") or
               path.startswith("/collections") or path.startswith("/search") or
               path.startswith("/user/"))
    if not listing:
        item = parse_item_url(url)
        if item:
            return "template", item
        # If Envato changes its item slug/identifier format, classify a direct
        # non-listing URL as a template; the canonical/title/live-preview data
        # will be read from the actual page. Do not apply this fallback to cards.
        segments = [x for x in p.path.strip("/").split("/") if x]
        if segments:
            name = safe_slug(segments[-1])
            item_id = __import__("hashlib").sha1(p.path.encode()).hexdigest()[:8].upper()
            return "template", (name, item_id)
    q = parse_qs(p.query)
    page = None
    if q.get("page") and q["page"][0].isdigit():
        page = int(q["page"][0])
    q.pop("page", None)
    base = urlunparse(("https", "elements.envato.com", p.path or "/", "", urlencode(q, doseq=True), ""))
    return "listing", (base, page)

def listing_page_url(base, n):
    if n <= 1:
        return base
    p = urlparse(base)
    q = parse_qs(p.query)
    q["page"] = [str(n)]
    return urlunparse((p.scheme, p.netloc, p.path, "", urlencode(q, doseq=True), ""))


def job_slug(name, item_id):
    """Folder name: shortened readable name + the unique ID (keeps Windows paths short)."""
    return f"{safe_slug(name, 48)}-{item_id.lower()}"


def extract_cards(html, base="https://elements.envato.com/"):
    """Item cards on one listing page -> [{slug, title, template_url}] in page order."""
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], {}
    for a in soup.find_all("a", href=True):
        href = urljoin(base, a["href"]).split("#", 1)[0].split("?", 1)[0]
        item = parse_item_url(href)
        if not item:
            continue
        name, item_id = item
        key = item_id
        title = (a.get("title") or "").strip()
        img = a.find("img")
        if not title and img is not None:
            title = re.sub(r"^\s*Preview:\s*", "", (img.get("alt") or "")).strip()
        if not title:
            title = a.get_text(" ", strip=True)
        if key not in seen:
            seen[key] = {"slug": job_slug(name, item_id), "title": title,
                         "template_url": f"https://elements.envato.com/{name}-{item_id}"}
            out.append(seen[key])
        elif title and not seen[key]["title"]:
            seen[key]["title"] = title
    return out


def last_page_number(html):
    nums = [int(n) for n in re.findall(r"[?&]page=(\d+)", html)]
    return max(nums) if nums else 0


def _has_cards(html):
    return bool(extract_cards(html))


# ----------------------------------------------------------------- listing
def collect_jobs(urls, fetcher):
    jobs, seen, bad = [], set(), []

    def add(slug, title, template_url, listing_url):
        if slug in seen or (MAX_TEMPLATES and len(jobs) >= MAX_TEMPLATES):
            return False
        seen.add(slug)
        jobs.append({"slug": slug, "template_url": template_url, "title": title, "listing_url": listing_url})
        return True

    for entered_url in urls:
        url = core.normalize_http_url(entered_url)
        if not url:
            bad.append(entered_url)
            continue
        kind, ident = classify_url(url)
        first_html = None
        recognized_item = parse_item_url(url)

        # When an unfamiliar route appears on Envato, inspect the response rather than
        # assuming every non-category route is an item. Listing-card evidence wins.
        if kind == "template" and not recognized_item:
            try:
                first_html = fetcher.get(url)
                cards_guess = extract_cards(first_html, url)
                pager_guess = core.discover_pagination_targets(first_html, url)
                title_guess = BeautifulSoup(first_html, "html.parser").title
                title_text = title_guess.get_text(" ", strip=True).lower() if title_guess else ""
                if (len(pager_guess) > 1 or len(cards_guess) >= 3 or
                        (len(cards_guess) >= 2 and any(word in title_text for word in ("template", "website", "theme", "collection")))):
                    kind, ident = "listing", (url, None)
            except FetchError:
                first_html = None

        if kind == "unsupported":
            bad.append(entered_url)
            continue
        if kind == "template":
            name, item_id = ident
            # Keep the exact input route. Do not manufacture an old-style item URL if
            # Envato changes the item slug/ID route; preview resolution reads the page.
            slug = job_slug(name, item_id)
            add(slug, "", url, "")
            continue

        base, only_page = ident
        step(f"Reading listing: {url}" + (f" (page {only_page} only)" if only_page else ""))
        if first_html is None:
            # Always use the actual submitted URL, including filters and alternate route forms.
            try:
                first_html = fetcher.get(url, ok=_has_cards)
            except FetchError as e:
                print(f"  page 1: {e}", flush=True)
                continue

        page_targets = core.discover_pagination_targets(first_html, url)
        if only_page:
            page_targets = {only_page: url}
        else:
            # Some server-rendered pages expose only the final page in raw HTML.
            last_hint = last_page_number(first_html)
            examples = [u for n, u in page_targets.items() if n > 1]
            for n in range(2, last_hint + 1):
                generated = core._build_page_url(url, n, examples)
                if generated:
                    page_targets.setdefault(n, generated)
            page_targets.setdefault(1, url)

        n = only_page or 1
        empty_in_a_row = 0
        highest = max(page_targets, default=n)
        while True:
            if only_page and n != only_page:
                break
            purl = url if n == 1 or only_page else page_targets.get(n) or core._build_page_url(url, n, [u for k, u in page_targets.items() if k > 1]) or listing_page_url(base, n)
            try:
                html = first_html if purl == url else fetcher.get(purl, ok=_has_cards)
            except FetchError as e:
                print(f"  page {n}: {e}", flush=True)
                break
            cards = extract_cards(html, purl)
            new = sum(1 for c in cards if add(c["slug"], c["title"], c["template_url"], purl))
            # Refresh targets from each actual page so cursor-based or evolving
            # pagination can reveal its next URL without a provider-specific guess.
            fresh = core.discover_pagination_targets(html, purl)
            known_urls = set(page_targets.values())
            known_urls.update({url})
            for page_num, href in fresh.items():
                if href in known_urls or href == purl:
                    continue
                if page_num > n and page_num not in page_targets:
                    assigned_num = page_num
                else:
                    assigned_num = max([n] + list(page_targets.keys())) + 1
                page_targets.setdefault(assigned_num, href)
                known_urls.add(href)
            highest = max(highest, max(page_targets, default=n))
            print(f"  page {n}: {len(cards)} item(s), {new} new  (total so far: {len(jobs)})", flush=True)
            if not cards and n == (only_page or 1):
                print("  no templates on the first page - the site may be blocking the crawler "
                      "(try again later, or open the page in your browser to check it loads)", flush=True)
            if only_page:
                break
            empty_in_a_row = 0 if new else empty_in_a_row + 1
            if empty_in_a_row >= EMPTY_PAGES_TO_STOP and n >= highest:
                # Continue when an actual Next link is still available, but stop cleanly
                # after a non-new page once there are no known targets ahead.
                break
            if MAX_LISTING_PAGES and n >= MAX_LISTING_PAGES:
                print(f"  reached MAX_LISTING_PAGES ({MAX_LISTING_PAGES})", flush=True)
                break
            if MAX_TEMPLATES and len(jobs) >= MAX_TEMPLATES:
                print(f"  reached MAX_TEMPLATES ({MAX_TEMPLATES})", flush=True)
                break
            next_candidates = [num for num in page_targets if num > n]
            if next_candidates:
                n = min(next_candidates)
            elif empty_in_a_row < EMPTY_PAGES_TO_STOP:
                n += 1
            else:
                break
            time.sleep(DELAY_BETWEEN_PAGES_SEC)
    return jobs, bad


# ----------------------------------------------------------------- preview lookup
def extract_preview(html, page_url="https://elements.envato.com/"):
    """Item page HTML -> {preview_url, title, description, categories}; preview_url '' if none."""
    soup = BeautifulSoup(html, "html.parser")
    preview = ""
    for a in soup.find_all("a", href=True):
        if LIVE_TEXT_RE.search(a.get_text(" ", strip=True)):
            href = urljoin(page_url, a["href"].strip())
            if urlparse(href).scheme in ("http", "https") and not _is_envato_host(urlparse(href).hostname):
                preview = href
                break
    if not preview:                         # fallback: an outbound link carrying Envato's storefront flag
        for a in soup.find_all("a", href=True):
            href = urljoin(page_url, a["href"].strip())
            if "storefront=envato-elements" in href and not _is_envato_host(urlparse(href).hostname):
                preview = href
                break
    h1 = soup.find("h1")
    title = h1.get_text(" ", strip=True) if h1 else ""
    md = soup.find("meta", attrs={"property": "og:description"}) or soup.find("meta", attrs={"name": "description"})
    desc = (md.get("content") or "").strip() if md else ""
    cats = [a.get_text(" ", strip=True) for a in soup.select("a[href*='/web-templates/'][title]")][:12]
    return {"preview_url": preview, "title": title, "description": desc, "categories": cats}


def _has_preview(html):
    return bool(extract_preview(html)["preview_url"])


def resolve_preview(job, fetcher):
    # plain HTTP first; if that page has no live-preview link, look again in Chrome before giving up
    html = fetcher.get(job["template_url"], ok=_has_preview)
    info = extract_preview(html, job["template_url"])
    if not info["preview_url"]:
        raise NoPreview("this item has no 'Live preview' link (for example a Figma/PSD kit)")
    return info


def crawl(urls, output_dir):
    return core.run_crawl(provider=PROVIDER, label=LABEL, default_url=DEFAULT_URL, urls=urls, output_dir=output_dir,
                          collect_jobs=collect_jobs, resolve_preview=resolve_preview)


if __name__ == "__main__":
    core.standalone_main(crawl, "envato_output")
