"""
Free Website Templates crawler  (provider crawler for Website Crawler Studio)

Site:     https://freewebsitetemplates.com/
Default:  https://freewebsitetemplates.com/            (used when the URL box is empty)

How the site works (checked against the live pages)
---------------------------------------------------
  * Listing pages:  /  (page 1)  and  /templates/page-2 ... /templates/page-N   (about 12 cards per page)
  * Each card links to  /preview/<slug>/   - that address IS the live template site (plain HTML/CSS),
    so the "preview" and the capture are the same page. No second hop is needed.
  * Some cards are "Get it on Wix for free"  (/website/<slug>/). Those are Wix sites, not downloadable
    templates, and are skipped.

What it does
------------
  * Empty URL / the home page / /templates/page-N : reads every listing page it can find
    (all pages when given the home page; just that page when given a /templates/page-N address).
  * /preview/<slug>/ (or /download/<slug>/, /template/<slug>/) : crawls that one template.
  * Each template's HOME page (index.html) is saved as an offline package. These templates have several
    pages (about.html, contact.html ...); only the home page is captured, like the other providers.

Run alone:  python freewebsitetemplates_crawler.py [<url> ...] [--out DIR]
Entry point used by runner.py:  crawl(urls, output_dir)
"""
import os
import re
import sys
import time
from urllib.parse import urljoin

from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import crawler_core as core                     # noqa: E402
from crawler_core import FetchError, NoPreview, safe_slug, step   # noqa: E402

PROVIDER = "freewebsitetemplates"
LABEL = "Free Website Templates"
DEFAULT_URL = "https://freewebsitetemplates.com/"      # hard-coded default

MAX_LISTING_PAGES = 0          # 0 = every page the site lists; set e.g. 3 to crawl only the first 3 pages
MAX_TEMPLATES = 0              # 0 = no limit; set a number to cap one run
DELAY_BETWEEN_PAGES_SEC = 1.0  # politeness delay between listing-page requests

BASE = "https://freewebsitetemplates.com"
_HOST = r"(?:www\.)?freewebsitetemplates\.com"
TEMPLATE_PATH_RE = re.compile(
    rf"^https?://{_HOST}/(?:preview|template|download|discuss|use)/([a-z0-9][a-z0-9_\-]*)(?:[/?#].*)?$", re.I)
PREVIEW_PATH_RE = re.compile(rf"^https?://{_HOST}/preview/([a-z0-9][a-z0-9_\-]*)(?:[/?#].*)?$", re.I)
LISTING_RE = re.compile(rf"^https?://{_HOST}(?:/(?:templates(?:/page-(\d+))?)?)?/?(?:[?#].*)?$", re.I)
PAGE_LINK_RE = re.compile(r"/templates/page-(\d+)", re.I)


# ----------------------------------------------------------------- URL logic
def classify_url(url):
    """Classify known routes first, then tolerate renamed preview/detail paths on this host."""
    u = url.strip()
    p = __import__("urllib.parse", fromlist=["urlparse"]).urlparse(u)
    if p.scheme not in ("http", "https") or not re.fullmatch(_HOST, p.hostname or "", re.I):
        return "unsupported", None
    m = TEMPLATE_PATH_RE.match(u)
    if m:
        return "template", m.group(1).lower()
    m = LISTING_RE.match(u)
    if m:
        return "listing", int(m.group(1)) if m.group(1) else None
    path = p.path.strip("/")
    if not path or path.lower() in {"templates", "category", "categories"}:
        return "listing", None
    # URL slug fall-back when the provider renames /preview/ or /template/.
    slug = path.split("/")[-1].strip()
    if slug and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", slug):
        return "template", slug.lower()
    return "unsupported", None


def listing_page_url(n):
    return BASE + "/" if n == 1 else f"{BASE}/templates/page-{n}"


def extract_cards(html, base=BASE + "/"):
    """Cards on one listing page -> [{slug, title}] in page order. Wix-only cards are not included."""
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], {}
    for a in soup.find_all("a", href=True):
        m = PREVIEW_PATH_RE.match(urljoin(base, a["href"]))
        if not m:
            continue
        slug = m.group(1).lower()
        title = (a.get("title") or "").strip()
        img = a.find("img")
        if not title and img is not None:
            title = (img.get("alt") or "").strip()
        if not title:
            title = a.get_text(" ", strip=True)
        if slug not in seen:
            seen[slug] = {"slug": slug, "title": "" if title.lower() in ("view", "download", "discuss", "edit") else title}
            out.append(seen[slug])
        elif title and not seen[slug]["title"] and title.lower() not in ("view", "download", "discuss", "edit"):
            seen[slug]["title"] = title
    return out


def last_page_number(html):
    nums = [int(n) for n in PAGE_LINK_RE.findall(html)]
    return max(nums) if nums else 1


def _has_cards(html):
    return bool(extract_cards(html))


# ----------------------------------------------------------------- listing
def collect_jobs(urls, fetcher):
    jobs, seen, bad = [], set(), []

    def add(slug, title, listing_url):
        if slug in seen or (MAX_TEMPLATES and len(jobs) >= MAX_TEMPLATES):
            return
        seen.add(slug)
        jobs.append({"slug": safe_slug(slug), "template_url": f"{BASE}/preview/{slug}/", "title": title,
                     "listing_url": listing_url, "preview_url": f"{BASE}/preview/{slug}/"})

    for entered_url in urls:
        url = core.normalize_http_url(entered_url)
        if not url:
            bad.append(entered_url)
            continue
        kind, ident = classify_url(url)
        first_html = None

        # Route names are only hints. If the provider changes a listing route, inspect
        # the page content before treating the terminal path segment as a template slug.
        known_template = bool(TEMPLATE_PATH_RE.match(url))
        if kind == "template" and not known_template:
            try:
                first_html = fetcher.get(url)
                cards_guess = extract_cards(first_html, url)
                pager_guess = core.discover_pagination_targets(first_html, url)
                title_guess = BeautifulSoup(first_html, "html.parser").title
                title_text = title_guess.get_text(" ", strip=True).lower() if title_guess else ""
                if (len(pager_guess) > 1 or len(cards_guess) >= 3 or
                        (len(cards_guess) >= 2 and any(word in title_text for word in ("template", "website", "theme", "collection")))):
                    kind, ident = "listing", None
            except FetchError:
                first_html = None

        if kind == "unsupported":
            bad.append(entered_url)
            continue
        if kind == "template":
            add(ident, "", "")
            continue

        step(f"Reading listing: {url}")
        if first_html is None:
            # Always start from the exact user-entered URL. Do not silently replace a
            # custom category/search URL with the provider's hard-coded home page.
            first_html = fetcher.get(url, ok=_has_cards)

        if ident:  # known /templates/page-N URL: just that requested page
            page_targets = {ident: url}
        else:
            page_targets = core.discover_pagination_targets(first_html, url)
            # Some older markup only mentions the final page in an unstructured URL.
            last = last_page_number(first_html)
            if last > max(page_targets, default=1):
                for n in range(2, last + 1):
                    page_targets.setdefault(n, core._build_page_url(url, n,
                        [u for k, u in page_targets.items() if k > 1]) or listing_page_url(n))
            page_targets.setdefault(1, url)

        pending = sorted(page_targets.items()) or [(1, url)]
        processed_urls = set()
        print(f"  pagination discovery found {len(pending)} initial page target(s)", flush=True)
        while pending:
            pending.sort(key=lambda item: item[0])
            page_n, purl = pending.pop(0)
            if purl in processed_urls:
                continue
            if MAX_LISTING_PAGES and len(processed_urls) >= MAX_LISTING_PAGES:
                print(f"  reached MAX_LISTING_PAGES ({MAX_LISTING_PAGES})", flush=True)
                break
            processed_urls.add(purl)
            if MAX_TEMPLATES and len(jobs) >= MAX_TEMPLATES:
                print(f"  reached MAX_TEMPLATES ({MAX_TEMPLATES})", flush=True)
                break
            try:
                html = first_html if purl == url else fetcher.get(purl, ok=_has_cards)
            except FetchError as e:
                print(f"  page {page_n}: {e}", flush=True)
                continue
            cards = extract_cards(html, purl)
            for c in cards:
                add(c["slug"], c["title"], purl)
            if not ident:  # page-N URL means the user explicitly asked for that page only
                fresh_targets = core.discover_pagination_targets(html, purl)
                known_urls = processed_urls | {u for _, u in pending}
                next_number = max([page_n] + [n for n, _ in pending]) + 1
                for discovered_n, discovered_url in fresh_targets.items():
                    if discovered_url == purl or discovered_url in known_urls:
                        continue
                    assigned_n = discovered_n if discovered_n > page_n and discovered_n not in {n for n, _ in pending} else next_number
                    pending.append((assigned_n, discovered_url))
                    known_urls.add(discovered_url)
                    next_number = max(next_number, assigned_n + 1)
            print(f"  page {page_n}: {len(cards)} template(s)  (total so far: {len(jobs)})", flush=True)
            time.sleep(DELAY_BETWEEN_PAGES_SEC)
    return jobs, bad


def resolve_preview(job, fetcher):
    """The /preview/<slug>/ address is the live site; nothing to look up."""
    if not job.get("preview_url"):
        raise NoPreview("no preview address")
    return {"preview_url": job["preview_url"], "title": job.get("title", ""), "categories": []}


def crawl(urls, output_dir):
    return core.run_crawl(provider=PROVIDER, label=LABEL, default_url=DEFAULT_URL, urls=urls, output_dir=output_dir,
                          collect_jobs=collect_jobs, resolve_preview=resolve_preview)


if __name__ == "__main__":
    core.standalone_main(crawl, "freewebsitetemplates_output")
