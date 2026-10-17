"""No-quota local LLM cleanup for captured template packages.

Uses an OpenAI-compatible local inference API (llama.cpp or LM Studio). No hosted API, API key, or
cloud fallback is used. Original crawler outputs are never edited in-place.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

BASE_URL = os.environ.get("STUDIO_LLM_BASE_URL", "http://127.0.0.1:1234/v1").rstrip("/")
TIMEOUT = int(os.environ.get("STUDIO_LLM_TIMEOUT", "180"))
MAX_FILE_CHARS = int(os.environ.get("STUDIO_LLM_MAX_FILE_CHARS", "4500"))
MAX_FILES_PER_TEMPLATE = int(os.environ.get("STUDIO_LLM_MAX_FILES", "0"))  # 0 = review every eligible code file

CODE_EXTENSIONS = {".html", ".htm", ".css", ".js", ".mjs", ".cjs"}
TRACKER_HOSTS = (
    "google-analytics.com", "googletagmanager.com", "connect.facebook.net",
    "facebook.com/tr", "static.hotjar.com", "script.clarity.ms", "clarity.ms",
    "analytics.tiktok.com", "snap.licdn.com", "px.ads.linkedin.com",
    "bat.bing.com", "stats.g.doubleclick.net", "pixel.wp.com", "plausible.io",
)
DEPENDENCY_HINTS = re.compile(
    r"wix\.com|wixstatic\.com|parastorage\.com|manage\.wix\.com|"
    r"googletagmanager|google-analytics|hotjar|clarity\.ms|doubleclick|"
    r"facebook\.com/tr|connect\.facebook|gtag\s*\(|fbq\s*\(|"
    r"_paq\.push|analytics\.track|sourceMappingURL=", re.I
)
GENERAL_DEPENDENCY_HINTS = re.compile(
    DEPENDENCY_HINTS.pattern + r"|https?://|url\(\s*[\"']?https?://", re.I
)
MAX_LLM_SEGMENTS_PER_FILE = 8
WIX_RUNTIME_FILE_HINTS = re.compile(
    r"wix|thunderbolt|rb_wixui|builder|viewer|react(?:-dom)?\.production|"
    r"main(?:\.renderer)?\.|modulemetadata|siteTags|fedops|businessLogger|"
    r"browserZoom|accessibilityBrowser|interact(?:ions)?|usedPlatformApis|warmupData|"
    r"reporter-api|service\.|appMonitoring|htmlparser|lodash|browser-deprecation", re.I
)
URL_RE = re.compile(r"https?://[^\s\"'<>`)]+", re.I)

CLEANUP_PIPELINE_VERSION = "wix-static-extraction-v5-dependency-map"
PROMPT_PATH = Path(__file__).with_name("standalone_cleanup_prompt.txt")
_PACKAGE_MODE_ADAPTER = r"""
APPLICATION INPUT ADAPTER (the original user requirements above remain authoritative):
In this application, the crawler has already captured the rendered webpage into a package containing
index.html and local assets. The LLM may receive only a small excerpt from that package, not a raw
MHTML file. Treat extraction of the browser-rendered DOM/assets as the completed upstream step; do
not claim to have parsed an MHTML file. Apply the original prompt's remaining requirements to the
captured page: remove Wix viewer/runtime dependencies, remove every iframe from Wix output, keep the
rendered HTML/CSS/assets and single-page structure, and never regenerate the design. Deterministic
code in the application handles known Wix bootstrap removal and URL localization before the LLM pass.

When called, return ONLY JSON:
{"edits":[{"find":"EXACT literal substring copied from the supplied excerpt","replace":"replacement text","reason":"why this is safe"}],"notes":[],"dependencies_to_review":[]}
Use only exact, minimal edits. If there is no clearly safe additional edit, return empty edits and explain uncertainties. Never return a new file or a redesign.
"""
_DEFAULT_SYSTEM_PROMPT = "Use the original standalone-cleanup prompt in standalone_cleanup_prompt.txt. " + _PACKAGE_MODE_ADAPTER
try:
    _BASE_SYSTEM_PROMPT = PROMPT_PATH.read_text(encoding="utf-8").strip() if PROMPT_PATH.is_file() else _DEFAULT_SYSTEM_PROMPT
except OSError:
    _BASE_SYSTEM_PROMPT = _DEFAULT_SYSTEM_PROMPT
SYSTEM_PROMPT = _BASE_SYSTEM_PROMPT



def get_models(base_url: str = BASE_URL, timeout: float = 3.0) -> list[dict]:
    """Return models currently exposed by the local API; never contacts a cloud API."""
    response = requests.get(f"{base_url}/models", timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    return payload.get("data", []) if isinstance(payload, dict) else []


def local_status(base_url: str = BASE_URL) -> dict:
    try:
        models = get_models(base_url)
        return {"ok": bool(models), "base_url": base_url,
                "models": [str(m.get("id", "")) for m in models if m.get("id")],
                "message": (f"Local model ready: {models[0].get('id')}" if models
                            else "The local model server is reachable, but it has no loaded model.")}
    except Exception as exc:
        return {"ok": False, "base_url": base_url, "models": [],
                "message": "Cannot reach the local LLM API. Start the local model server and ensure it is listening on the configured address. "
                           f"Details: {type(exc).__name__}: {str(exc)[:180]}"}


def _extract_json(text: str) -> dict:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {}
    except ValueError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            return {}
        try:
            obj = json.loads(match.group(0))
            return obj if isinstance(obj, dict) else {}
        except ValueError:
            return {}


def _chat(messages: list[dict], base_url: str = BASE_URL, model: str | None = None) -> str:
    if model:
        model_id = model
    else:
        models = get_models(base_url)
        if not models:
            raise RuntimeError("No local model is loaded in the local LLM server.")
        model_id = str(models[0].get("id", ""))
    if not model_id:
        raise RuntimeError("The local LLM server did not report a loaded model ID.")
    response = requests.post(
        f"{base_url}/chat/completions",
        json={"model": model_id, "messages": messages, "temperature": 0.0,
              "max_tokens": max(128, int(os.environ.get("STUDIO_LLM_MAX_TOKENS", "1800"))), "stream": False,
              "response_format": {"type": "json_object"}},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    return str(payload["choices"][0]["message"].get("content", ""))


def _is_tracker_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if any(host == h or host.endswith("." + h) for h in TRACKER_HOSTS if "/" not in h):
        return True
    return host.endswith("facebook.com") and (parsed.path == "/tr" or parsed.path.startswith("/tr/"))


def _strip_known_tracking(html: str) -> tuple[str, list[str]]:
    """Remove only clear tracking tags while preserving every other byte of the HTML."""
    removed = []
    script_re = re.compile(r"<script\b[^>]*\bsrc\s*=\s*([\"'])(.*?)\1[^>]*>[\s\S]*?</script\s*>", re.I)
    link_re = re.compile(r"<link\b[^>]*>", re.I)
    img_re = re.compile(r"<img\b[^>]*>", re.I)

    def script_sub(match):
        url = match.group(2)
        if _is_tracker_url(url):
            removed.append(f"Removed known tracking script: {url}")
            return ""
        return match.group(0)

    html = script_re.sub(script_sub, html)

    def link_sub(match):
        tag = match.group(0)
        href = re.search(r"\bhref\s*=\s*([\"'])(.*?)\1", tag, re.I)
        rel = re.search(r"\brel\s*=\s*([\"'])(.*?)\1", tag, re.I)
        if href and rel and _is_tracker_url(href.group(2)) and re.search(r"\b(?:preconnect|dns-prefetch|prefetch)\b", rel.group(2), re.I):
            removed.append(f"Removed known tracking resource hint: {href.group(2)}")
            return ""
        return tag

    html = link_re.sub(link_sub, html)

    def img_sub(match):
        tag = match.group(0)
        src = re.search(r"\bsrc\s*=\s*([\"'])(.*?)\1", tag, re.I)
        if not src or not _is_tracker_url(src.group(2)):
            return tag
        url = src.group(2)
        dimensions = re.search(r"\b(?:width|height)\s*=\s*([\"']?)(1)\1(?:\s|/?>)", tag, re.I)
        if dimensions or any(bit in url.lower() for bit in ("collect", "pixel", "/tr?")):
            removed.append(f"Removed known tracking pixel: {url}")
            return ""
        return tag

    html = img_re.sub(img_sub, html)
    return html, removed


def _visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    return " ".join(soup.stripped_strings)


def _external_urls(content: str) -> list[str]:
    out = []
    seen = set()
    for raw in URL_RE.findall(content or ""):
        url = raw.rstrip(".,;:!?)]}\'")
        host = (urlparse(url).hostname or "").lower()
        if host and host not in ("localhost", "127.0.0.1") and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _all_code_files(package: Path) -> list[Path]:
    files = []
    for path in package.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in CODE_EXTENSIONS:
            continue
        relparts = {p.lower() for p in path.relative_to(package).parts}
        if relparts.intersection({"node_modules", ".git", "__pycache__", "vendor"}):
            continue
        files.append(path)
    return sorted(files, key=lambda p: (0 if p.name.lower() in {"index.html", "index.htm"} else 1,
                                        len(p.relative_to(package).parts), str(p).lower()))


def _is_wix_package(package: Path) -> bool:
    """Identify Wix-rendered captures using package metadata or unmistakable runtime markers."""
    metadata_path = package / "metadata.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if str(metadata.get("provider", "")).strip().lower() == "wix":
            return True
    except (OSError, ValueError):
        pass
    try:
        head = (package / "index.html").read_text(encoding="utf-8", errors="replace")[:160_000].lower()
        return any(marker in head for marker in (
            "wix-essential-viewer-model", "wix-viewer-model", "webpackjsonp__wix_thunderbolt_app",
            "static.parastorage.com/services/wix-thunderbolt", "static.wixstatic.com",
        ))
    except OSError:
        return False


def _code_files(package: Path) -> list[Path]:
    all_files = _all_code_files(package)
    if _is_wix_package(package):
        # Wix minified runtime bundles are handled as provider runtime, not sent to an LLM one file
        # at a time. The user explicitly wants one page preserved, not vendor JS regenerated.
        index = package / "index.html"
        return [index] if index.is_file() else []
    selected = []
    for path in all_files:
        try:
            sample = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if path.name.lower() in {"index.html", "index.htm"} or GENERAL_DEPENDENCY_HINTS.search(sample):
            selected.append(path)
    return selected[:MAX_FILES_PER_TEMPLATE] if MAX_FILES_PER_TEMPLATE > 0 else selected


def _review_segments(text: str, wix_mode: bool = False) -> list[str]:
    """Return a small, bounded set of dependency-focused excerpts for local review."""
    if len(text) <= MAX_FILE_CHARS:
        return [text]
    pattern = DEPENDENCY_HINTS if wix_mode else GENERAL_DEPENDENCY_HINTS
    matches = list(pattern.finditer(text))
    if not matches:
        # Never split a huge HTML/CSS/JS file into hundreds of arbitrary model calls.
        return []
    margin = min(1800, max(500, MAX_FILE_CHARS // 3))
    intervals = []
    for match in matches:
        start = max(0, match.start() - margin)
        end = min(len(text), max(match.end() + margin, start + min(MAX_FILE_CHARS, len(text))))
        if intervals and start <= intervals[-1][1] + 250:
            intervals[-1] = (intervals[-1][0], max(intervals[-1][1], end))
        else:
            intervals.append((start, end))
    # Prefer a few distinct early and late dependency contexts. Eight is enough for a review pass;
    # broad deterministic cleanup is not delegated to thousands of token-heavy model requests.
    if len(intervals) > MAX_LLM_SEGMENTS_PER_FILE:
        half = MAX_LLM_SEGMENTS_PER_FILE // 2
        intervals = intervals[:half] + intervals[-(MAX_LLM_SEGMENTS_PER_FILE-half):]
    segments = []
    for start, end in intervals:
        segment = text[start:min(end, start + MAX_FILE_CHARS)]
        if segment and segment not in segments:
            segments.append(segment)
    return segments


def _apply_edits(source: str, edits: list, is_html: bool) -> tuple[str, list[dict], list[dict]]:
    current = source
    applied, rejected = [], []
    original_text = _visible_text(source) if is_html else ""
    original_urls = set(_external_urls(source))
    for edit in edits[:30]:
        if not isinstance(edit, dict):
            continue
        find = edit.get("find")
        replace = edit.get("replace", "")
        reason = str(edit.get("reason", ""))[:400]
        if not isinstance(find, str) or not isinstance(replace, str) or len(find) < 18:
            rejected.append({"reason": "Missing/too-short exact match or invalid replacement."})
            continue
        count = current.count(find)
        if count != 1:
            rejected.append({"find_preview": find[:120], "reason": f"Exact match count was {count}; expected 1."})
            continue
        candidate = current.replace(find, replace, 1)
        new_urls = set(_external_urls(candidate)) - original_urls
        if new_urls:
            rejected.append({"find_preview": find[:120], "reason": "Would introduce new external URL(s): " + ", ".join(sorted(new_urls)[:5])})
            continue
        if is_html:
            new_visible = _visible_text(candidate)
            # Do not accept edits that visibly erase large parts of the captured site.
            if len(original_text) >= 30 and len(new_visible) < max(20, int(len(original_text) * 0.80)):
                rejected.append({"find_preview": find[:120], "reason": "Would remove too much visible page text; preserved original."})
                continue
            if "<html" in source.lower() and "<html" not in candidate.lower():
                rejected.append({"find_preview": find[:120], "reason": "Would remove the HTML document root."})
                continue
        current = candidate
        # Future validation compares against the original host set; intentionally don't permit novel hosts.
        applied.append({"reason": reason or "Minimal LLM-proposed edit", "find_preview": find[:120]})
    return current, applied, rejected


def _source_signature(package: Path) -> str:
    digest = hashlib.sha256()
    count = 0
    total = 0
    newest = 0
    for path in sorted(package.rglob("*")):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        rel = str(path.relative_to(package)).replace("\\", "/")
        digest.update(f"{rel}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode("utf-8", "replace"))
        count += 1
        total += stat.st_size
        newest = max(newest, stat.st_mtime_ns)
    digest.update(f"{count}:{total}:{newest}".encode())
    return digest.hexdigest()


def _pipeline_signature(model_id: str, base_url: str) -> str:
    """Invalidate cached cleaned copies when the prompt, model, endpoint, or cleaner changes."""
    digest = hashlib.sha256()
    digest.update(CLEANUP_PIPELINE_VERSION.encode("utf-8"))
    digest.update(SYSTEM_PROMPT.encode("utf-8", "replace"))
    digest.update(model_id.encode("utf-8", "replace"))
    digest.update(base_url.encode("utf-8", "replace"))
    return digest.hexdigest()


def _rewrite_assets_from_manifest(package: Path) -> list[dict]:
    """Rewrite exact remote asset URLs to downloaded local files listed in asset-manifest.json.

    This is deterministic: the mapping is supplied by the crawler, not invented by the LLM.
    URLs used only as ordinary navigation links are not rewritten unless the crawler identified
    that exact URL as a downloaded asset.
    """
    manifest_path = package / "asset-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    assets = manifest.get("assets", []) if isinstance(manifest, dict) else []
    mappings = []
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        remote = str(asset.get("url") or "").strip()
        local = str(asset.get("local") or "").strip().replace("\\", "/")
        if not remote.startswith(("http://", "https://")) or not local:
            continue
        local_path = package / Path(local)
        try:
            local_path.resolve().relative_to(package.resolve())
        except (OSError, ValueError):
            continue
        if local_path.is_file():
            mappings.append((remote, local))
    mappings.sort(key=lambda item: len(item[0]), reverse=True)
    # Wix serves the same image through many transformed URLs (different widths, quality and format).
    # If the crawler downloaded the same media ID, map all transformed variants to that local file.
    wix_media = {}
    for remote, local in mappings:
        parsed = urlparse(remote)
        if (parsed.hostname or "").lower() == "static.wixstatic.com" and parsed.path.startswith("/media/"):
            media_key = parsed.path[len("/media/"):].split("/", 1)[0].split("?", 1)[0].lower()
            if media_key and media_key not in wix_media:
                wix_media[media_key] = local
    wix_media_pattern = re.compile(r"https?://static\.wixstatic\.com/media/([^/\s\"'<>)]*)(?:/[^\s\"'<>)]*)?", re.I)
    changed = []
    for code_file in _all_code_files(package):
        try:
            source = code_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        current = source
        file_count = 0
        rel_parent = code_file.parent.relative_to(package).as_posix()
        for remote, local in mappings:
            target = Path(local)
            source_ref = os.path.relpath(package / target, code_file.parent).replace("\\", "/")
            if not source_ref.startswith((".", "/")):
                source_ref = "./" + source_ref if code_file.suffix.lower() in {".css", ".js", ".mjs", ".cjs"} and rel_parent != "." else source_ref
            variants = (remote, remote.replace("&", "&amp;"), remote.replace("https://", "//", 1) if remote.startswith("https://") else remote.replace("http://", "//", 1))
            for variant in dict.fromkeys(variants):
                n = current.count(variant)
                if n:
                    current = current.replace(variant, source_ref)
                    file_count += n
        if wix_media:
            def replace_wix_media(match):
                nonlocal file_count
                raw_url = match.group(0).rstrip(".,;:!?")
                media_key = match.group(1).lower()
                local = wix_media.get(media_key)
                if not local:
                    return match.group(0)
                source_ref = os.path.relpath(package / Path(local), code_file.parent).replace("\\", "/")
                if code_file.suffix.lower() in {".css", ".js", ".mjs", ".cjs"} and code_file.parent != package and not source_ref.startswith((".", "/")):
                    source_ref = "./" + source_ref
                file_count += 1
                return source_ref
            current = wix_media_pattern.sub(replace_wix_media, current)
        if current != source:
            code_file.write_text(current, encoding="utf-8")
            changed.append({"file": code_file.relative_to(package).as_posix(), "references_rewritten": file_count})
    return changed


def _strip_wix_generator_and_preview_iframe(html: str, remove_all_iframes: bool = False) -> tuple[str, list[str]]:
    """Remove known Wix viewer/runtime bootstrap while leaving rendered HTML and CSS intact.

    For Wix captures, the user's prompt explicitly forbids iframes, so every iframe is removed.
    Other providers retain the previous safer behavior unless remove_all_iframes is requested.
    """
    removed: list[str] = []
    soup = BeautifulSoup(html, "html.parser")
    text_lower = html.lower()
    looks_wix = remove_all_iframes or any(marker in text_lower for marker in (
        "wix-essential-viewer-model", "wix-viewer-model", "webpackjsonp__wix_thunderbolt_app",
        "static.parastorage.com/services/wix-thunderbolt", "static.wixstatic.com",
    ))

    for meta in list(soup.find_all("meta")):
        name = str(meta.get("name", "")).lower()
        prop = str(meta.get("property", "")).lower()
        content = str(meta.get("content", "")).lower()
        if (name == "generator" and "wix" in content):
            removed.append("Removed Wix generator metadata")
            meta.decompose()
        elif looks_wix and name == "robots" and "noindex" in content:
            removed.append("Removed noindex metadata from the reusable standalone copy")
            meta.decompose()
        elif looks_wix and prop == "og:url" and ("wix.com" in content or "wixstatic.com" in content):
            removed.append("Removed Wix preview Open Graph URL")
            meta.decompose()

    for link in list(soup.find_all("link")):
        rel = " ".join(link.get("rel", [])).lower() if isinstance(link.get("rel", []), list) else str(link.get("rel", "")).lower()
        href = str(link.get("href", ""))
        low_href = href.lower()
        if looks_wix and "canonical" in rel and any(host in low_href for host in ("wix.com", "wixstatic.com", "parastorage.com")):
            removed.append("Removed Wix preview canonical URL")
            link.decompose()
        elif looks_wix and any(token in rel for token in ("preconnect", "dns-prefetch", "prefetch", "modulepreload", "prerender")) and any(host in low_href for host in ("wix.com", "wixstatic.com", "parastorage.com")):
            removed.append(f"Removed Wix runtime resource hint: {href[:160]}")
            link.decompose()

    for iframe in list(soup.find_all("iframe")):
        src = str(iframe.get("src", ""))
        attrs = " ".join(str(iframe.get(k, "")) for k in ("title", "id", "class", "data-hook"))
        marker = (src + " " + attrs).lower()
        is_viewer = bool(re.search(r"desktop-iframe|manage\.wix\.com|wix\.com/.{0,40}(?:template|preview|viewer)", marker))
        if remove_all_iframes or is_viewer:
            removed.append(f"Removed iframe: {src or attrs or 'unnamed iframe'}")
            iframe.decompose()

    if looks_wix:
        runtime_script_markers = re.compile(
            r"wix-essential-viewer-model|wix-viewer-model|wix-warmup-data|wix-fedops|wix-first-paint|"
            r"wix-skip-played-animations|scroll-bar-width-calculation|bodycacheable|exclusionreason|"
            r"used-platform-apis-data|interactions-config|interactions-loader|pagehtmlembeds|"
            r"webpackjsonp__wix_thunderbolt_app|window\.viewerModel|window\.commonConfig|"
            r"window\.thunderbolttag|window\.fedops|window\.clientsiderender|"
            r"wix-thunderbolt|static\.parastorage\.com|static\.wixstatic\.com|"
            r"rb_wixui|thunderbolt|siteTags|browserZoom|accessibilityBrowser|builderModuleLoader|"
            r"builderContextProviders|builderComponent|sentryOnLoadSetup|sentry|window\.__tbAnchorGuard|"
            r"resolveExternalsRegistryPromise|wixAdsOffsetHeight|Powered and secured by Wix", re.I)
        for script in list(soup.find_all("script")):
            src = str(script.get("src", ""))
            data_url = str(script.get("data-url", ""))
            ident = str(script.get("id", ""))
            script_type = str(script.get("type", ""))
            content = script.string or script.get_text() or ""
            filename = Path(urlparse(src).path).name if src else ""
            is_viewer_data = script_type.lower() in {"application/json", "importmap", "speculationrules", "wix/htmlembeds"} and (
                runtime_script_markers.search(ident + " " + content) or
                (script_type.lower() == "importmap" and ("wix.com" in content.lower() or "blob:https://" in content.lower())) or
                script_type.lower() == "speculationrules")
            is_remote_wix_bundle = any(host in (src + " " + data_url).lower() for host in (
                "static.parastorage.com", "static.wixstatic.com", "parastorage.com"))
            is_known_runtime_file = bool(WIX_RUNTIME_FILE_HINTS.search(filename))
            is_inline_runtime = not src and bool(runtime_script_markers.search(content + " " + ident))
            if is_viewer_data or is_remote_wix_bundle or is_known_runtime_file or is_inline_runtime:
                removed.append(f"Removed Wix runtime/bootstrap script: {ident or filename or data_url[:100] or 'inline script'}")
                script.decompose()

        # Drop the Wix desktop-viewport marker but keep the standard responsive viewport meta.
        viewport = soup.find("meta", attrs={"name": re.compile(r"^viewport$", re.I)})
        if viewport and str(viewport.get("id", "")).lower().startswith("wix"):
            viewport.attrs.pop("id", None)

        # The Wix demo link and Wix footer badge are not part of the reusable website itself.
        for a in list(soup.find_all("a", href=True)):
            href = str(a.get("href", ""))
            parsed = urlparse(href)
            if (parsed.hostname or "").lower() in {"wix.com", "www.wix.com"} and parsed.path.startswith("/demone2/"):
                a["href"] = "#top" if parsed.path.rstrip("/").endswith("wh-modern-artist") else "#"
                removed.append(f"Converted captured Wix preview navigation to an in-page placeholder: {parsed.path[:100]}")
            elif (parsed.hostname or "").lower() in {"wix.com", "www.wix.com"} and a.get_text(" ", strip=True).lower() == "wix":
                a.decompose()
                removed.append("Removed Wix footer credit link")
        # Remove the words of the standard Wix credit while retaining the copyright text.
        for node in list(soup.find_all(string=True)):
            if node.parent and node.parent.name not in {"script", "style"}:
                updated = str(node).replace("Powered and secured by Wix", "").replace("Powered and secured by", "")
                if updated != str(node):
                    node.replace_with(updated.strip())
                    removed.append("Removed Wix footer credit text")

        # Source map comments are development metadata, not styles or resources required at runtime.
        for style in soup.find_all("style"):
            css = style.get_text() or ""
            css = re.sub(r"/\*[#@]\s*sourceMappingURL=.*?\*/", "", css, flags=re.I | re.S)
            css = re.sub(r"(?m)^\s*//[#@]\s*sourceMappingURL=.*?$", "", css, flags=re.I)
            style.clear()
            style.append(css)
            # data-url/data-href here merely identify the remote Wix stylesheet from which this
            # already-captured inline CSS came. Keep CSS; discard the dead source locator.
            for attr in ("data-url", "data-href"):
                value = str(style.get(attr, "")).lower()
                if any(host in value for host in ("wix.com", "wixstatic.com", "parastorage.com")):
                    style.attrs.pop(attr, None)

    return "<!DOCTYPE html>\n" + str(soup), removed


def _sanitize_wix_output_metadata(package: Path) -> list[str]:
    """Remove source preview URLs from copied metadata; keep useful local asset inventory."""
    notes = []
    wix_url_re = re.compile(r"https?://[^\s\"'<>]*(?:wix\.com|wixstatic\.com|parastorage\.com|manage\.wix\.com)[^\s\"'<>]*", re.I)

    def redact(value):
        if isinstance(value, str):
            return wix_url_re.sub("[Wix source URL removed]", value)
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, dict):
            clean = {}
            for key, item in value.items():
                if str(key).lower() in {"template_url", "preview_url", "final_url", "source_url", "url"}:
                    continue
                clean[key] = redact(item)
            return clean
        return value
    metadata_path = package / "metadata.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        for key in ("template_url", "preview_url", "final_url", "source_url", "url"):
            if key in metadata:
                metadata.pop(key, None)
                notes.append(f"Removed source URL field from metadata.json: {key}")
        metadata["standalone"] = True
        metadata["source_urls_removed"] = True
        metadata_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
    except (OSError, ValueError):
        pass

    manifest_path = package / "asset-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.pop("source_url", None)
        for key in ("unresolved_external_dependencies", "remote_hosts_mentioned_in_local_js_for_review"):
            values = manifest.get(key)
            if isinstance(values, list):
                manifest[key] = [v for v in values if not re.search(r"wix\.com|wixstatic\.com|parastorage\.com|manage\.wix\.com", str(v), re.I)]
        assets = manifest.get("assets", [])
        if isinstance(assets, list):
            for asset in assets:
                if isinstance(asset, dict):
                    asset.pop("url", None)
        manifest = redact(manifest)
        manifest["source_urls_removed"] = True
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    except (OSError, ValueError):
        pass
    return notes


def _redact_wix_urls_in_report(report: dict) -> None:
    """The report may describe removed resources, but should not retain literal Wix source URLs."""
    pattern = re.compile(r"https?://[^\s\"'<>]*(?:wix\.com|wixstatic\.com|parastorage\.com|manage\.wix\.com)[^\s\"'<>]*", re.I)
    def redact(value):
        if isinstance(value, str):
            return pattern.sub("[Wix URL removed]", value)
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, dict):
            return {key: redact(item) for key, item in value.items()}
        return value
    for key in list(report):
        report[key] = redact(report[key])


def _strip_source_map_references(package: Path) -> list[str]:
    """Remove development-only source-map locator comments from retained local code/assets."""
    changed = []
    for path in package.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".css", ".js", ".mjs", ".cjs"}:
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        cleaned = re.sub(r"/\*[#@]\s*sourceMappingURL=.*?\*/", "", source, flags=re.I | re.S)
        cleaned = re.sub(r"(?m)^\s*//[#@]\s*sourceMappingURL=.*?$", "", cleaned, flags=re.I)
        if cleaned != source:
            path.write_text(cleaned, encoding="utf-8")
            changed.append(f"Removed source-map locator comment: {path.relative_to(package).as_posix()}")
    return changed


def _remove_unreferenced_wix_runtime_files(package: Path) -> list[str]:
    """Delete orphaned local Wix runtime scripts after their bootstrap references are removed."""
    html_path = package / "index.html"
    try:
        html = html_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    referenced = set(re.findall(r"(?:src|href)\s*=\s*[\"']([^\"']+\.m?js)(?:[?#][^\"']*)?[\"']", html, re.I))
    # Keep JS files if still referenced by the surviving HTML. Inline CSS cannot require JS files.
    removed = []
    js_root = package / "assets" / "js"
    if js_root.is_dir():
        for path in js_root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in {".js", ".mjs", ".cjs"}:
                continue
            rel = path.relative_to(package).as_posix()
            if rel not in referenced and ("assets/js/" + path.name) not in referenced:
                path.unlink()
                removed.append(f"Removed unused copied Wix runtime asset: {rel}")
        for folder in sorted((x for x in js_root.rglob("*") if x.is_dir()), key=lambda x: len(x.parts), reverse=True):
            try:
                folder.rmdir()
            except OSError:
                pass
        try:
            js_root.rmdir()
        except OSError:
            pass
    return removed



def _validate_standalone_package(package: Path, is_wix: bool) -> dict:
    """Run static checks only; this does not substitute for a visual desktop/mobile browser test."""
    html_files = sorted(p for p in package.rglob("*") if p.is_file() and p.suffix.lower() in {".html", ".htm"})
    index = package / "index.html"
    result = {
        "html_page_files": len(html_files),
        "single_page_structure": len(html_files) == 1 and index.is_file(),
        "iframe_tags": 0,
        "wix_host_references_in_site_files": 0,
        "missing_local_assets": [],
        "responsive_viewport_present": False,
        "visual_browser_test_performed": False,
        "visual_browser_test_note": "Static checks only; compare in a browser at desktop and mobile widths before treating the result as pixel-perfect.",
    }
    if not index.is_file():
        result["missing_index_html"] = True
        result["status"] = "FAIL"
        return result
    html = index.read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")
    result["iframe_tags"] = len(soup.find_all("iframe"))
    result["responsive_viewport_present"] = soup.find("meta", attrs={"name": re.compile(r"^viewport$", re.I)}) is not None
    host_re = re.compile(r"wix\.com|wixstatic\.com|parastorage\.com|manage\.wix\.com", re.I)
    for path in package.rglob("*"):
        if not path.is_file() or path.name == "cleanup-report.json":
            continue
        if path.suffix.lower() not in {".html", ".htm", ".css", ".js", ".mjs", ".cjs"} and path.name not in {"metadata.json", "asset-manifest.json"}:
            continue
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        result["wix_host_references_in_site_files"] += len(host_re.findall(body))
    for tag in soup.find_all(["img", "script", "source", "video", "audio"]):
        raw = tag.get("src") or tag.get("poster")
        if not raw or str(raw).startswith(("data:", "blob:", "#", "http://", "https://", "//", "javascript:")):
            continue
        candidate = (package / str(raw).split("?", 1)[0].split("#", 1)[0]).resolve()
        try:
            candidate.relative_to(package.resolve())
        except ValueError:
            result["missing_local_assets"].append(f"Unsafe local path: {raw}")
            continue
        if not candidate.is_file():
            result["missing_local_assets"].append(str(raw))
    checks_ok = result["single_page_structure"] and (not is_wix or result["iframe_tags"] == 0) and result["wix_host_references_in_site_files"] == 0 and not result["missing_local_assets"] and result["responsive_viewport_present"]
    result["status"] = "PASS" if checks_ok else "REVIEW"
    return result


def _active_external_css_js_dependencies(package: Path) -> list[dict]:
    """Find only active, unresolved remote CSS/JS resources; ignore navigation and URL literals."""
    found = []
    seen = set()

    def add(kind: str, url: str, file: str):
        value = (url or "").strip().strip("\"'")
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return
        ext = Path(parsed.path.lower()).suffix
        allowed = {"css": {".css"}, "js": {".js", ".mjs", ".cjs"}}
        if kind not in allowed or ext not in allowed[kind]:
            return
        key = (kind, value, file)
        if key not in seen:
            seen.add(key)
            found.append({"type": kind, "url": value, "file": file})

    import_patterns = [
        re.compile(r"\bimport\s*\(\s*['\"]([^'\"]+\.(?:m?js|css)(?:\?[^'\"]*)?)['\"]\s*\)", re.I),
        re.compile(r"\bimport\s*['\"]([^'\"]+\.(?:m?js|css)(?:\?[^'\"]*)?)['\"]", re.I),
        re.compile(r"\b(?:import|export)\b[^;\n]*?\bfrom\s*['\"]([^'\"]+\.(?:m?js|css)(?:\?[^'\"]*)?)['\"]", re.I),
    ]
    css_import = re.compile(r"@import\s+(?:url\()?\s*['\"]([^'\"]+\.css(?:\?[^'\"]*)?)['\"]\s*\)?", re.I)

    for code_file in _all_code_files(package):
        rel = code_file.relative_to(package).as_posix()
        try:
            body = code_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        suffix = code_file.suffix.lower()
        if suffix in {".html", ".htm"}:
            soup = BeautifulSoup(body, "html.parser")
            for tag in soup.find_all("script", src=True):
                add("js", tag.get("src", ""), rel)
            for tag in soup.find_all("link", href=True):
                rels = {str(x).lower() for x in (tag.get("rel") or [])}
                if "stylesheet" in rels:
                    add("css", tag.get("href", ""), rel)
            for tag in soup.find_all("style"):
                css = tag.get_text() or ""
                for match in css_import.finditer(css):
                    add("css", match.group(1), rel)
                locator = tag.get("data-href") or tag.get("data-url")
                if locator and not css.strip():
                    add("css", locator, rel)
            for tag in soup.find_all(style=True):
                css_text = tag.get("style") or ""
                for match in css_import.finditer(css_text):
                    add("css", match.group(1), rel)
        elif suffix == ".css":
            for match in css_import.finditer(body):
                add("css", match.group(1), rel)
        elif suffix in {".js", ".mjs", ".cjs"}:
            for pattern in import_patterns:
                for match in pattern.finditer(body):
                    raw = match.group(1)
                    # Bare module names and relative references are not unresolved *remote* URLs.
                    add("js" if Path(urlparse(raw).path).suffix.lower() in {".js", ".mjs", ".cjs"} else "css", raw, rel)
    return found


def _focused_external_dependency_segments(text: str, dependencies: list[dict], limit: int = MAX_FILE_CHARS) -> list[str]:
    """Return a single compact excerpt containing unresolved active CSS/JS references."""
    snippets = []
    for item in dependencies[:8]:
        url = item.get("url", "")
        index = text.find(url)
        if index < 0:
            index = text.find(url.replace("&", "&amp;"))
        if index < 0:
            continue
        start = max(0, index - min(1200, limit // 3))
        end = min(len(text), start + limit)
        snippet = text[start:end]
        if snippet not in snippets:
            snippets.append(snippet)
    if not snippets:
        return []
    combined = "\n\n<!-- unresolved active stylesheet/script references -->\n\n".join(snippets)
    return [combined[:limit]]


def process_template(source_dir: str | Path, dest_dir: str | Path,
                     base_url: str = BASE_URL, model: str | None = None,
                     log: Callable[[str], None] | None = None,
                     force: bool = False) -> dict:
    """Create an isolated cleaned copy and report; never edits the original crawl."""
    src = Path(source_dir).resolve()
    dest = Path(dest_dir).resolve()
    log = log or (lambda _: None)
    if src == dest or src in dest.parents or dest in src.parents:
        raise ValueError("Raw source and standalone output must be separate, non-nested directories.")
    index = src / "index.html"
    if not index.is_file():
        raise FileNotFoundError(f"No index.html in template package: {src}")
    models = get_models(base_url)
    model_id = model or (str(models[0].get("id", "")) if models else "")
    if not model_id:
        raise RuntimeError("No local model is loaded. Start the local model server before cleaning templates.")

    signature = _source_signature(src)
    pipeline_signature = _pipeline_signature(model_id, base_url)
    if force or os.environ.get("STUDIO_FORCE_CLEANUP", "").strip().lower() in {"1", "true", "yes"}:
        force = True
    existing_report = dest / "cleanup-report.json"
    try:
        old_report = json.loads(existing_report.read_text(encoding="utf-8"))
        if (not force and old_report.get("source_signature") == signature
                and old_report.get("pipeline_signature") == pipeline_signature
                and (dest / "index.html").is_file()):
            return {"status": "skipped", "path": str(dest), "report": old_report}
    except (OSError, ValueError):
        pass

    temp = dest.with_name(dest.name + ".__cleaning_tmp__")
    if temp.exists():
        shutil.rmtree(temp, ignore_errors=True)
    if temp.is_dir():
        shutil.rmtree(temp)
    temp.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, temp, dirs_exist_ok=False)

    report = {
        "source": str(src), "source_signature": signature, "pipeline_signature": pipeline_signature,
        "pipeline_version": CLEANUP_PIPELINE_VERSION, "prompt_file": PROMPT_PATH.name,
        "prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode("utf-8", "replace")).hexdigest(),
        "model": model_id, "base_url": base_url,
        "mode": "local-only", "wix_static_extraction": _is_wix_package(src),
        "max_llm_segments_per_file": MAX_LLM_SEGMENTS_PER_FILE,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "files_reviewed": [], "asset_rewrites": [], "deterministic_removals": [], "llm_requests": [],
        "llm_edits": [], "rejected_edits": [], "notes": [], "dependencies_to_review": [],
        "remaining_external_urls": [], "unresolved_active_css_js_dependencies": [],
        "crawler_asset_failures": [], "llm_review_skipped_reason": "",
        "changes_applied": 0, "status": "partial",
    }

    # The package format is already index.html + local assets, not MHTML. Apply safe deterministic
    # cleanup first, using the crawler's manifest as the source of truth for local asset mappings.
    report["asset_rewrites"] = _rewrite_assets_from_manifest(temp)
    try:
        crawler_manifest = json.loads((temp / "asset-manifest.json").read_text(encoding="utf-8"))
        failures = crawler_manifest.get("failed", []) if isinstance(crawler_manifest, dict) else []
        report["crawler_asset_failures"] = failures if isinstance(failures, list) else []
    except (OSError, ValueError):
        report["crawler_asset_failures"] = []
    target_index = temp / "index.html"
    html = target_index.read_text(encoding="utf-8", errors="replace")
    visible_text_before = len(_visible_text(html))
    html, removed = _strip_known_tracking(html)
    report["deterministic_removals"].extend(removed)
    is_wix = _is_wix_package(temp)
    html, wrapper_removed = _strip_wix_generator_and_preview_iframe(html, remove_all_iframes=is_wix)
    report["deterministic_removals"].extend(wrapper_removed)
    target_index.write_text(html, encoding="utf-8")
    if is_wix:
        report["deterministic_removals"].extend(_remove_unreferenced_wix_runtime_files(temp))
        report["deterministic_removals"].extend(_strip_source_map_references(temp))
        report["notes"].extend(_sanitize_wix_output_metadata(temp))
        stale_validation = temp / "offline-validation.txt"
        if stale_validation.exists():
            stale_validation.unlink()
            report["deterministic_removals"].append("Removed stale crawler-stage offline-validation.txt; final validation is written after cleanup")

    active_css_js_dependencies = _active_external_css_js_dependencies(temp) if is_wix else []
    report["unresolved_active_css_js_dependencies"] = active_css_js_dependencies
    if is_wix and not active_css_js_dependencies:
        # URL mapping is deterministic and the known Wix viewer is already handled above.
        # Do not spend a generation call asking the model to repeat those mechanical checks.
        report["llm_review_skipped_reason"] = (
            "No active external CSS/JavaScript imports remain after deterministic localization; "
            "navigation links and inert URL strings are not downloaded or rewritten as assets."
        )
        review_files = []
    elif is_wix:
        # Review the file that actually contains the first unresolved import, not the whole
        # page or all vendor bundles. Cap Wix inference at one request per template.
        first_file = temp / str(active_css_js_dependencies[0].get("file", "index.html"))
        review_files = [first_file] if first_file.is_file() and first_file.suffix.lower() in CODE_EXTENSIONS else [temp / "index.html"]
    else:
        review_files = _code_files(temp)

    for src_file in review_files:
        rel = src_file.relative_to(temp).as_posix()
        try:
            text = src_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if is_wix and active_css_js_dependencies:
            segments = _focused_external_dependency_segments(text, active_css_js_dependencies)
            # At most one focused call per Wix package: the model is only a review fallback,
            # never the link-rewriter or a bulk source-code editor.
            segments = segments[:1]
        else:
            segments = _review_segments(text, wix_mode=is_wix)
        final_text = text
        file_applied, file_rejected, file_proposed = [], [], 0
        if not segments:
            report["notes"].append(f"Skipped LLM review for {rel}: no focused dependency excerpt found (large vendor/page files are not chunked wholesale).")
            continue
        for segment_number, segment in enumerate(segments, start=1):
            user_content = (f"File path: {rel}\nFile type: {src_file.suffix}\n"
                            f"Segment {segment_number} of {len(segments)}. The text below is an exact segment from the saved package. "
                            "If proposing an edit, copy the find text byte-for-byte and ensure it is unique in the full file.\n"
                            "Review for clearly removable viewer/editor/analytics/tracking code and genuinely unnecessary remote dependencies. "
                            "Do not delete remote image/font/CSS/JS resources unless the supplied context proves they are viewer-only or analytics. "
                            "The original crawler already saved the rendered page. Do not try to extract an unavailable MHTML file. "
                            "Suggest safe placeholders for obvious business-specific text only when this does not damage layout.\n\n"
                            "FILE CONTENT START\n" + segment + "\nFILE CONTENT END")
            call_record = {"file": rel, "segment": segment_number, "input_chars": len(user_content),
                           "response_chars": 0, "json_valid": False, "edits_proposed": 0,
                           "edits_applied": 0, "edits_rejected": 0}
            try:
                response_text = _chat([
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ], base_url=base_url, model=model_id)
                call_record["response_chars"] = len(response_text or "")
                plan = _extract_json(response_text)
                call_record["json_valid"] = isinstance(plan, dict) and bool(plan)
                if not call_record["json_valid"]:
                    report["notes"].append(f"LLM returned no parseable JSON for {rel} segment {segment_number}; no edits from this segment were applied.")
                    report["llm_requests"].append(call_record)
                    continue
            except Exception as exc:
                call_record["error"] = f"{type(exc).__name__}: {str(exc)[:220]}"
                report["llm_requests"].append(call_record)
                report["notes"].append(f"LLM review failed for {rel} segment {segment_number}: {type(exc).__name__}: {str(exc)[:180]}")
                continue
            edits = plan.get("edits", []) if isinstance(plan.get("edits", []), list) else []
            call_record["edits_proposed"] = len(edits)
            file_proposed += len(edits)
            final_text, applied, rejected = _apply_edits(final_text, edits, src_file.suffix.lower() in {".html", ".htm"})
            file_applied.extend(applied)
            file_rejected.extend(rejected)
            call_record["edits_applied"] = len(applied)
            call_record["edits_rejected"] = len(rejected)
            report["llm_requests"].append(call_record)
            report["notes"].extend([f"{rel}: {n}" for n in (plan.get("notes", []) if isinstance(plan.get("notes", []), list) else [])][:20])
            report["dependencies_to_review"].extend([{"file": rel, "item": str(x)[:500]} for x in (plan.get("dependencies_to_review", []) if isinstance(plan.get("dependencies_to_review", []), list) else [])][:50])
        if file_applied:
            src_file.write_text(final_text, encoding="utf-8")
            report["llm_edits"].extend([{"file": rel, **item} for item in file_applied])
        report["rejected_edits"].extend([{"file": rel, **item} for item in file_rejected])
        report["files_reviewed"].append({"file": rel, "segments_reviewed": len(segments), "edits_proposed": file_proposed, "edits_applied": len(file_applied), "edits_rejected": len(file_rejected)})

    # Audit remaining URL literals in all HTML/CSS/JS. Remote links used for navigation are also
    # reported, so the report is a review list rather than a claim that all external traffic is bad.
    ext = []
    for code_file in _all_code_files(temp):
        try:
            body = code_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for url in _external_urls(body):
            ext.append({"file": code_file.relative_to(temp).as_posix(), "url": url,
                        "classification": "tracker" if _is_tracker_url(url) else "external_or_navigation"})
    # unique entries by file/url
    seen = set()
    for row in ext:
        key = (row["file"], row["url"])
        if key not in seen:
            seen.add(key)
            report["remaining_external_urls"].append(row)
    report["changes_applied"] = len(report["asset_rewrites"]) + len(report["deterministic_removals"]) + len(report["llm_edits"])
    final_html = (temp / "index.html").read_text(encoding="utf-8", errors="replace")
    visible_text_after = len(_visible_text(final_html))
    report["visible_text_chars_before_cleanup"] = visible_text_before
    report["visible_text_chars_after_cleanup"] = visible_text_after
    report["visible_text_retention_percent"] = round(100 * visible_text_after / visible_text_before, 2) if visible_text_before else 100.0
    report["validation"] = _validate_standalone_package(temp, is_wix)
    # `remaining_external_urls` is a broad audit of URL literals, so it includes harmless
    # navigation links, SVG namespaces, and library license URLs. Only unresolved *active*
    # resource dependencies, failed crawler downloads, LLM concerns, or validation failures
    # should force the result into a review status.
    llm_review_notes = [n for n in report["notes"] if str(n).lower().startswith(("llm", "skipped llm review"))]
    has_review_items = bool(
        report["unresolved_active_css_js_dependencies"]
        or report["crawler_asset_failures"]
        or report["dependencies_to_review"]
        or report["rejected_edits"]
        or llm_review_notes
        or report["validation"].get("status") != "PASS"
    )
    if report["changes_applied"] and has_review_items:
        report["status"] = "cleaned_with_review_items"
    elif report["changes_applied"]:
        report["status"] = "cleaned"
    elif has_review_items:
        report["status"] = "unchanged_needs_review"
    else:
        report["status"] = "unchanged_no_safe_edits"
    _redact_wix_urls_in_report(report)
    (temp / "cleanup-report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    (temp / "standalone-validation.txt").write_text(
        f"STATUS: {report['status']}\n"
        f"Model: {model_id}\n"
        f"Pipeline version: {CLEANUP_PIPELINE_VERSION}\n"
        f"Prompt: {PROMPT_PATH.name} (SHA-256 {report['prompt_sha256']})\n"
        f"Files reviewed: {len(report['files_reviewed'])}\n"
        f"LLM requests: {len(report['llm_requests'])}\n"
        f"Unresolved active CSS/JS dependencies: {len(report['unresolved_active_css_js_dependencies'])}\n"
        f"Crawler asset download failures: {len(report['crawler_asset_failures'])}\n"
        f"LLM review skipped: {report['llm_review_skipped_reason'] or 'no'}\n"
        f"LLM edits proposed: {sum(int(r.get('edits_proposed', 0)) for r in report['llm_requests'])}\n"
        f"LLM edits applied: {len(report['llm_edits'])}\n"
        f"LLM edits rejected: {len(report['rejected_edits'])}\n"
        f"Files with asset references localized: {len(report['asset_rewrites'])}\n"
        f"Deterministic removals: {len(report['deterministic_removals'])}\n"
        f"Remaining external URL references: {len(report['remaining_external_urls'])}\n"
        f"Dependencies flagged for review: {len(report['dependencies_to_review'])}\n"
        f"Changes applied: {report['changes_applied']}\n"
        f"Static validation: {report['validation'].get('status', 'REVIEW')}\n"
        f"HTML page files: {report['validation'].get('html_page_files', 'unknown')}\n"
        f"Iframe tags in index.html: {report['validation'].get('iframe_tags', 'unknown')}\n"
        f"Wix host references in site files: {report['validation'].get('wix_host_references_in_site_files', 'unknown')}\n"
        f"Responsive viewport present: {report['validation'].get('responsive_viewport_present', False)}\n"
        f"Missing local assets: {len(report['validation'].get('missing_local_assets', []))}\n"
        f"Visible text retained: {report.get('visible_text_retention_percent', 'unknown')}%\n"
        f"Browser visual test performed: No\n"
        f"Note: original source folder was not modified.\n",
        encoding="utf-8")

    # Promote completed output atomically; source remains untouched.
    if dest.exists():
        old = dest.with_name(dest.name + ".__previous__")
        shutil.rmtree(old, ignore_errors=True)
        os.replace(dest, old)
        try:
            os.replace(temp, dest)
        except Exception:
            if not dest.exists() and old.exists():
                os.replace(old, dest)
            raise
        shutil.rmtree(old, ignore_errors=True)
    else:
        os.replace(temp, dest)
    log(f"[cleanup] {src.name}: {report['status']} | model requests: {len(report['llm_requests'])}, LLM edits applied: {len(report['llm_edits'])}, asset files updated: {len(report['asset_rewrites'])}, deterministic removals: {len(report['deterministic_removals'])}")
    if report["changes_applied"] == 0:
        log(f"[cleanup] NO FILE CHANGES for {src.name}. Open {dest / 'cleanup-report.json'} to see whether the model returned no edits, invalid JSON, or edits were rejected.")
    else:
        log(f"[cleanup] cleaned copy: {dest} (raw source is unchanged)")
    return {"status": report["status"], "path": str(dest), "report": report}


def _package_signature(package: Path) -> tuple[str, int, int]:
    """Cheap change detector used by the watcher; exact signature is computed at finalization."""
    stats = []
    newest = 0
    total_bytes = 0
    for p in package.rglob("*"):
        if p.is_file():
            try:
                st = p.stat()
            except OSError:
                continue
            newest = max(newest, st.st_mtime_ns)
            total_bytes += st.st_size
            if p.name in {"index.html", "metadata.json", "asset-manifest.json"}:
                stats.append((p.name, st.st_size, st.st_mtime_ns))
    return hashlib.sha256(repr(sorted(stats)).encode()).hexdigest(), total_bytes, newest


def discover_packages(root: str | Path) -> list[Path]:
    root = Path(root)
    found = []
    if not root.is_dir():
        return found
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in {".git", "__pycache__", "node_modules"}]
        if "index.html" in files and "metadata.json" in files:
            found.append(Path(current))
            dirs[:] = []
    return found


def watch_and_clean(source_root: str | Path, cleaned_root: str | Path,
                    should_continue: Callable[[], bool], log: Callable[[str], None] | None = None,
                    base_url: str = BASE_URL, model: str | None = None,
                    poll_seconds: float = 3.0) -> None:
    """Clean completed template packages as the crawl runs, then drain pending templates."""
    log = log or (lambda _: None)
    observations: dict[str, tuple[tuple[str, int, int], int]] = {}
    processed: dict[str, tuple[str, int, int]] = {}
    idle_passes = 0
    source_root = Path(source_root).resolve()
    cleaned_root = Path(cleaned_root).resolve()
    while True:
        packages = discover_packages(source_root)
        ready = []
        for package in packages:
            key = str(package)
            try:
                sig = _package_signature(package)
            except OSError:
                continue
            prev_sig, stable = observations.get(key, (sig, 0))
            stable = stable + 1 if prev_sig == sig else 0
            observations[key] = (sig, stable)
            if stable >= 1 and processed.get(key) != sig:
                ready.append((package, sig))
        if ready:
            idle_passes = 0
            for package, sig in ready:
                try:
                    rel = package.relative_to(source_root)
                    destination = cleaned_root / rel
                    process_template(package, destination, base_url=base_url, model=model, log=log,
                                     force=os.environ.get("STUDIO_FORCE_CLEANUP", "").strip().lower() in {"1", "true", "yes"})
                    processed[str(package)] = sig
                except Exception as exc:
                    temp = (cleaned_root / rel).with_name((cleaned_root / rel).name + ".__cleaning_tmp__")
                    if temp.is_dir():
                        try:
                            (temp / "cleanup-error.json").write_text(json.dumps({
                                "status": "failed", "package": package.name,
                                "error_type": type(exc).__name__, "error": str(exc)[:1000],
                                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")
                            }, indent=2), encoding="utf-8")
                            failed = temp.with_name(temp.name.replace(".__cleaning_tmp__", ".__failed__"))
                            shutil.rmtree(failed, ignore_errors=True)
                            os.replace(temp, failed)
                        except OSError:
                            shutil.rmtree(temp, ignore_errors=True)
                    log(f"[cleanup] FAILED for {package.name}: {type(exc).__name__}: {str(exc)[:240]}")
                    # Avoid hammering the same broken package every polling cycle.
                    processed[str(package)] = sig
        elif not should_continue():
            idle_passes += 1
            if idle_passes >= 3:
                break
        else:
            idle_passes = 0
        time.sleep(poll_seconds)
