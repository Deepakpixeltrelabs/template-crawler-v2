"""
WP Astra (Starter Templates) crawler  (provider crawler for Website Crawler Studio)

Site:     https://wpastra.com/website-templates/
Default:  https://wpastra.com/website-templates/business/      (used when the URL box is empty)

How the site works (checked against the live pages)
---------------------------------------------------
  * Category listing:  /website-templates/<category>/   then   /website-templates/<category>/page/2/ ... /page/N/
    12 cards per page ("Showing 1-12 of 184 templates"). Free and premium templates are both listed.
  * Each card has  Details -> https://wpastra.com/templates/<slug>/
                   Live Preview -> https://websitedemos.net/<slug>?customize=template
    The preview is an ordinary WordPress demo site at  https://websitedemos.net/<slug>/ .
    (The "?customize=template" part is only a flag; the crawler opens the clean address.)

What it does
------------
  * Empty URL, or any wpastra.com/website-templates/... category page: reads every listing page of that
    category (all pages when given the first page; just that page when given a /page/N/ address).
  * https://wpastra.com/templates/<slug>/   or   https://websitedemos.net/<slug>/ : crawls that one template.
  * Saves the demo site's home page as an offline package.

Run alone:  python wpastra_crawler.py [<url> ...] [--out DIR]
Entry point used by runner.py:  crawl(urls, output_dir)
"""
import os
import re
import sys
import time
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import crawler_core as core                     # noqa: E402
from crawler_core import FetchError, NoPreview, safe_slug, step   # noqa: E402

PROVIDER = "wpastra"
LABEL = "WP Astra"
DEFAULT_URL = "https://wpastra.com/website-templates/business/"      # hard-coded default

MAX_LISTING_PAGES = 0          # 0 = every page of the category; set e.g. 2 to crawl only the first 2 pages
MAX_TEMPLATES = 0              # 0 = no limit; set a number to cap one run
DELAY_BETWEEN_PAGES_SEC = 1.0  # politeness delay between listing-page requests

DEMO_RE = re.compile(r"^https?://(?:www\.)?websitedemos\.net/([a-z0-9][a-z0-9\-_]*)(?:[/?#].*)?$", re.I)
DETAILS_RE = re.compile(r"^https?://(?:www\.)?wpastra\.com/templates/([a-z0-9][a-z0-9\-_]*)/?(?:[?#].*)?$", re.I)
LISTING_RE = re.compile(r"^https?://(?:www\.)?wpastra\.com/website-templates(/[^?#]*)?(?:[?#].*)?$", re.I)
PAGE_PATH_RE = re.compile(r"/page/(\d+)/?$", re.I)
PAGE_LINK_RE = re.compile(r"/website-templates/[^\"'\s]*?/page/(\d+)/", re.I)
LABELS = {"live preview", "details", "preview", "view"}


# ----------------------------------------------------------------- URL logic
def classify_url(url):
    """Classify known category/detail routes, with a same-host fallback for route changes."""
    u = url.strip()
    from urllib.parse import urlparse
    parsed = urlparse(u)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https"):
        return "unsupported", None
    m = DETAILS_RE.match(u)
    if m:
        return "template", m.group(1).lower()
    m = DEMO_RE.match(u)
    if m:
        return "template", m.group(1).lower()
    m = LISTING_RE.match(u)
    if m:
        path = m.group(1) or "/"
        pm = PAGE_PATH_RE.search(path)
        page = int(pm.group(1)) if pm else None
        if pm:
            path = path[:pm.start()]
        sub = path.strip("/")
        base = "https://wpastra.com/website-templates/" + (sub + "/" if sub else "")
        return "listing", (base, page)
    if host in ("wpastra.com", "www.wpastra.com"):
        path = parsed.path.strip("/")
        if not path or path.lower() in {"templates", "website-templates"}:
            return "listing", ("https://wpastra.com/website-templates/", None)
        slug = path.split("/")[-1].lower()
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]*", slug):
            return "template", slug
    return "unsupported", None


def listing_page_url(base, n):
    return base if n == 1 else f"{base}page/{n}/"


def extract_cards(html, base="https://wpastra.com/"):
    """Cards on one listing page -> [{slug, title, template_url, preview_url}] in page order."""
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], {}
    for a in soup.find_all("a", href=True):
        href = urljoin(base, a["href"])
        m = DEMO_RE.match(href)
        if not m:
            continue
        slug = m.group(1).lower()
        text = a.get_text(" ", strip=True)
        title = "" if (not text or text.lower() in LABELS) else text
        if slug not in seen:
            seen[slug] = {"slug": slug, "title": title,
                          "template_url": f"https://wpastra.com/templates/{slug}/",
                          "preview_url": f"https://websitedemos.net/{slug}/"}
            out.append(seen[slug])
        elif title and not seen[slug]["title"]:
            seen[slug]["title"] = title
    return out


def last_page_number(html):
    nums = [int(n) for n in PAGE_LINK_RE.findall(html)]
    return max(nums) if nums else 1


def total_announced(html):
    m = re.search(r"of\s*(?:<[^>]+>\s*)*(\d[\d,]*)\s*(?:<[^>]+>\s*)*templates", html, re.I)
    return int(m.group(1).replace(",", "")) if m else 0


def _has_cards(html):
    return bool(extract_cards(html))


# ----------------------------------------------------------------- listing
def collect_jobs(urls, fetcher):
    jobs, seen, bad = [], set(), []

    def add(slug, title, template_url, preview_url, listing_url):
        if slug in seen or (MAX_TEMPLATES and len(jobs) >= MAX_TEMPLATES):
            return
        seen.add(slug)
        jobs.append({"slug": safe_slug(slug), "template_url": template_url, "preview_url": preview_url,
                     "title": title, "listing_url": listing_url})

    for entered_url in urls:
        url = core.normalize_http_url(entered_url)
        if not url:
            bad.append(entered_url)
            continue
        kind, ident = classify_url(url)
        first_html = None
        direct_known = bool(DETAILS_RE.match(url) or DEMO_RE.match(url))

        # New category routes often appear before selectors/classifiers are updated.
        # Probe ambiguous same-provider URLs and prefer real template-card evidence.
        if kind == "template" and not direct_known and (urlparse(url).hostname or "").lower().endswith("wpastra.com"):
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
            # Keep a provider's canonical detail/demo route when known. The live demo
            # hostname is independent of the user-entered template URL.
            slug = ident
            template_url = url if (urlparse(url).hostname or "").lower().endswith("wpastra.com") else f"https://wpastra.com/templates/{slug}/"
            preview_url = url if DEMO_RE.match(url) else f"https://websitedemos.net/{slug}/"
            add(slug, "", template_url, preview_url, "")
            continue

        base, only_page = ident
        step(f"Reading listing: {url}" + (f" (page {only_page} only)" if only_page else ""))
        # Always read the exact URL submitted by the user first, including its filters,
        # query parameters, and host spelling; base/default routes are fallback only.
        if first_html is None:
            first_html = fetcher.get(url, ok=_has_cards)

        if only_page:
            page_targets = {only_page: url}
        else:
            page_targets = core.discover_pagination_targets(first_html, url)
            last = last_page_number(first_html)
            if last > max(page_targets, default=1):
                examples = [u for n, u in page_targets.items() if n > 1]
                for n in range(2, last + 1):
                    generated = core._build_page_url(url, n, examples)
                    page_targets.setdefault(n, generated or listing_page_url(base, n))
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
                add(c["slug"], c["title"], c["template_url"], c["preview_url"], purl)
            if not only_page:  # a /page/N URL is intentionally a single-page request
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
    """The demo site address is already known from the listing (or derived from the template slug)."""
    if not job.get("preview_url"):
        raise NoPreview("no preview address")
    return {"preview_url": job["preview_url"], "title": job.get("title", ""), "categories": []}


def crawl(urls, output_dir):
    return core.run_crawl(provider=PROVIDER, label=LABEL, default_url=DEFAULT_URL, urls=urls, output_dir=output_dir,
                          collect_jobs=collect_jobs, resolve_preview=resolve_preview)


if __name__ == "__main__":
    core.standalone_main(crawl, "wpastra_output")
