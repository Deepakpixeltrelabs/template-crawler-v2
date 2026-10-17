"""Runs ONE provider crawler as a child process.

The crawler script itself is not modified. We import it, point its output
folder (and, optionally, its listing URLs) at what the UI asked for, then call
its own entry point.

Two crawler styles are supported:
  * crawl(urls, output_dir) -> (saved, failed, skipped)    preferred for new crawlers
  * run() with module globals OUTPUT_DIR / LOG_FILE / LISTING_URLS    the Wix script

Usage: python runner.py <crawler_script> <output_dir> [<json list of listing urls>]
"""
import importlib.util
import json
import os
import re
import sys
import traceback


# ---------------------------------------------------------------- chromedriver
def wdm_dir():
    return os.path.join(os.path.expanduser("~"), ".wdm")


def clear_stale_wdm_locks():
    """webdriver-manager guards its download with .wdm-lock-* files. If a run is killed
    mid-download (Stop button, closed window, crash) the lock stays forever and every
    later run waits ~60s and then crashes. The app runs one crawl at a time, so any lock
    found when a crawl starts is stale."""
    removed = []
    try:
        for name in os.listdir(wdm_dir()):
            if name.startswith(".wdm-lock"):
                try:
                    os.remove(os.path.join(wdm_dir(), name))
                    removed.append(name)
                except OSError:
                    pass
    except OSError:
        pass
    return removed


def install_safe_driver_manager(mod):
    """If the crawler uses webdriver-manager's ChromeDriverManager, wrap it so that
    (1) stale locks are cleared first and (2) if it still fails (offline, GitHub/Google
    blocked, lock held, wrong version) we fall back to Selenium's built-in driver
    manager instead of crashing. The crawler file is not edited."""
    real = getattr(mod, "ChromeDriverManager", None)
    if real is None:
        return

    class SafeChromeDriverManager:
        def __init__(self, *a, **k):
            self._real = real(*a, **k)

        def install(self):
            gone = clear_stale_wdm_locks()
            if gone:
                print(f"[studio] removed stale webdriver-manager lock file(s): {', '.join(gone)}", flush=True)
            try:
                return self._real.install()
            except Exception as e:
                first = (str(e).strip().splitlines() or [""])[0][:200]
                print(f"[studio] webdriver-manager could not provide chromedriver ({type(e).__name__}: {first})", flush=True)
                print("[studio] falling back to Selenium's built-in driver manager", flush=True)
                clear_stale_wdm_locks()
                return None          # Service(None) -> Selenium Manager picks/downloads the driver

    mod.ChromeDriverManager = SafeChromeDriverManager


# ----------------------------------------------------------- friendly errors
HINTS = [
    (r"wdm-lock|Timed out waiting for webdriver-manager",
     "A previous run left a driver-download lock. It is cleared automatically on the next start - just press Crawl again."),
    (r"cannot find Chrome binary|Chrome binary|chrome not reachable|no chrome",
     "Google Chrome was not found. Install Chrome (https://www.google.com/chrome) and try again."),
    (r"only supports Chrome version|session not created|This version of ChromeDriver",
     "Chrome and chromedriver versions do not match. Update Google Chrome, restart the app, and try again."),
    (r"Unable to obtain driver|Could not reach host|Max retries exceeded|getaddrinfo|NameResolutionError|ConnectionError|Temporary failure",
     "The internet connection (or a firewall/proxy) blocked the driver download or the website. Check your connection and try again."),
    (r"WinError 5|WinError 32|PermissionError|Access is denied|being used by another process",
     "Windows refused file access. Close any open output files/folders, pause antivirus scanning of the project folder, and try again."),
    (r"WinError 206|WinError 3\b|File name too long|path too long",
     "The file path is too long for Windows. Move the whole crawler_studio folder to a short path such as C:\\crawler_studio."),
    (r"invalid session id|disconnected|chrome not reachable|target window already closed|DevToolsActivePort",
     "Chrome closed or crashed during the crawl. Close other Chrome/chromedriver windows and try again; completed templates are kept."),
]


def explain(text):
    for pattern, hint in HINTS:
        if re.search(pattern, text, re.I):
            return hint
    return None


def warn_if_path_long(outdir):
    if os.name == "nt" and len(outdir) > 90:
        print(f"[studio] WARNING: the output folder path is {len(outdir)} characters long. Windows limits full file paths "
              "to about 260 characters, and saved assets sit several folders deeper. If you see 'path too long' "
              "errors, move the crawler_studio folder to a short path such as C:\\crawler_studio.", flush=True)


def main():
    script, outdir = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
    urls = json.loads(sys.argv[3]) if len(sys.argv) > 3 else []

    sys.path.insert(0, os.path.dirname(script))
    spec = importlib.util.spec_from_file_location("provider_crawler", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    os.makedirs(outdir, exist_ok=True)
    print(f"[studio] output folder: {outdir}", flush=True)
    warn_if_path_long(outdir)
    install_safe_driver_manager(mod)

    # Newer crawlers expose crawl(urls, output_dir) -> (saved, failed, skipped)
    if hasattr(mod, "crawl"):
        saved, failed, skipped = mod.crawl(urls, outdir)
        sys.exit(1 if failed and not (saved or skipped) else 0)

    # Legacy crawlers (e.g. the Wix script) use module globals + run()
    if hasattr(mod, "OUTPUT_DIR"):
        mod.OUTPUT_DIR = outdir
        mod.LOG_FILE = os.path.join(outdir, "log.csv")
    if urls and hasattr(mod, "LISTING_URLS"):
        mod.LISTING_URLS = urls

    if urls:
        print(f"[studio] using {len(urls)} listing URL(s) from the page", flush=True)
    else:
        print("[studio] using the crawler's built-in listing URLs", flush=True)
    mod.run()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as e:
        tb = traceback.format_exc()
        print(tb, flush=True)
        hint = explain(tb)
        print("[studio] ERROR: the crawler stopped with an error." + (f"\n[studio] HINT: {hint}" if hint else ""), flush=True)
        sys.exit(1)
