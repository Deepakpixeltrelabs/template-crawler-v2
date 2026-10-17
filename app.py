"""Website Crawler Studio - one page, pick a provider, press Crawl, watch the log."""
import atexit
import csv
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from urllib.parse import urlparse

from flask import Flask, jsonify, render_template, request

from local_ai_cleanup import BASE_URL as LOCAL_AI_BASE_URL, local_status, watch_and_clean

BASE = os.path.dirname(os.path.abspath(__file__))
PROVIDERS_FILE = os.environ.get("STUDIO_PROVIDERS", os.path.join(BASE, "providers.json"))
OUTPUT_ROOT = os.environ.get("STUDIO_OUTPUT", os.path.join(BASE, "outputs"))
STANDALONE_ROOT = os.environ.get("STUDIO_STANDALONE_OUTPUT", os.path.join(BASE, "outputs_standalone"))
LOG_DIR = os.path.join(BASE, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
STATE_FILE = os.path.join(LOG_DIR, "last_run.json")

app = Flask(__name__)
IS_WIN = os.name == "nt"


def load_providers():
    with open(PROVIDERS_FILE, encoding="utf-8") as f:
        items = json.load(f)
    for p in items:
        p["script_path"] = os.path.normpath(os.path.join(BASE, p["script"]))
    return {p["id"]: p for p in items}


# ---------------------------------------------------------------- run state
class Run:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.proc = None
        self.state = "idle"          # idle | running | finished | failed | stopped
        self.provider = None
        self.started = None
        self.ended = None
        self.lines = []              # [{n, t, text}]
        self.step = ""
        self.counts = dict(listings=0, pages=0, discovered=0, saved=0,
                           validation_failed=0, failed=0, skipped=0)
        self.exit_code = None
        self.output_dir = None
        self.log_file = None
        self.stop_requested = False
        self.urls = []
        self.provider_id = None
        self.run_id = None
        self.mode = "crawl"
        self.force_refresh = False
        self.standalone_cleanup = False
        self.local_model_id = None
        self.cleanup_running = False


run = Run()

# Progress comes ONLY from what the crawler actually prints. If a line isn't
# recognised it is still shown in the log; it just doesn't move a counter.
RULES = [
    (re.compile(r"^\s*\[step\]\s*(.+?)\s*$"),
     lambda m, c: m.group(1)),
    (re.compile(r"=== Listing: (.+?) ==="),
     lambda m, c: (c.__setitem__("listings", c["listings"] + 1), f"Opening listing: {m.group(1)}")[1]),
    (re.compile(r"detected (\d+) page"),
     lambda m, c: f"Listing has {m.group(1)} page(s)"),
    (re.compile(r"scanning page (\d+)"),
     lambda m, c: (c.__setitem__("pages", c["pages"] + 1), f"Scanning listing page {m.group(1)}")[1]),
    (re.compile(r"found (\d+) unique templates"),
     lambda m, c: (c.__setitem__("discovered", c["discovered"] + int(m.group(1))),
                   f"Found {m.group(1)} templates in this listing")[-1]),
    (re.compile(r"skip \(already done\): (\S+)"),
     lambda m, c: (c.__setitem__("skipped", c["skipped"] + 1), "Skipped (already saved)")[1]),
    (re.compile(r"saved: (.+?) \| assets: (\d+) \| validation: (\w+)"),
     lambda m, c: (c.__setitem__("saved", c["saved"] + 1),
                   c.__setitem__("validation_failed", c["validation_failed"] + (m.group(3) != "OK")),
                   f"Saved template ({m.group(2)} assets, validation {m.group(3)})")[-1]),
    (re.compile(r"FAILED: (\S+)"),
     lambda m, c: (c.__setitem__("failed", c["failed"] + 1), "A template failed - continuing")[1]),
]


def persist_last_run():
    """Persist enough run context to recover after app/browser/PC interruption."""
    payload = {"provider_id": run.provider_id, "provider": run.provider,
               "urls": run.urls, "output_dir": run.output_dir,
               "mode": run.mode, "force_refresh": run.force_refresh,
               "standalone_cleanup": run.standalone_cleanup, "run_id": run.run_id,
               "updated_at": datetime.now().isoformat(timespec="seconds")}
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, STATE_FILE)


def read_last_run():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def templates_in_output(provider_id):
    """Return completed, partial and _incomplete templates, merging latest log rows."""
    root = os.path.join(OUTPUT_ROOT, provider_id or "")
    if not os.path.isdir(root):
        return []
    by_id = {}
    for parent in (root, os.path.join(root, "_incomplete")):
        if not os.path.isdir(parent):
            continue
        for folder in os.listdir(parent):
            if folder.startswith(".") or (parent == root and folder.startswith("_")):
                continue
            meta_path = os.path.join(parent, folder, "metadata.json")
            try:
                with open(meta_path, encoding="utf-8") as f:
                    m = json.load(f)
                by_id[folder] = {"id": folder, "title": m.get("title") or folder,
                    "status": m.get("status", "unknown"), "template_url": m.get("template_url", ""),
                    "preview_url": m.get("preview_url", ""), "assets": m.get("asset_count", 0),
                    "warnings": m.get("warnings", [])}
            except (OSError, ValueError):
                continue
    # The CSV records failures that happened before a metadata package could be created.
    log_path = os.path.join(root, "log.csv")
    try:
        with open(log_path, newline="", encoding="utf-8") as f:
            for row in csv.reader(f):
                if len(row) < 6 or row[0] == "time" or not row[1]:
                    continue
                tid = row[1]
                status = row[5]
                if tid not in by_id or status in ("failed", "partial", "no_preview"):
                    existing = by_id.get(tid, {})
                    by_id[tid] = {"id": tid, "title": row[2] or existing.get("title") or tid,
                        "status": status, "template_url": row[3] if len(row)>3 else existing.get("template_url", ""),
                        "preview_url": row[4] if len(row)>4 else existing.get("preview_url", ""),
                        "assets": existing.get("assets", row[7] if len(row)>7 and str(row[7]).isdigit() else 0),
                        "warnings": existing.get("warnings", [])}
    except OSError:
        pass
    return sorted(by_id.values(), key=lambda x: x["title"].lower())


def failed_template_ids(provider_id):
    # Failed and partial packages are retryable; completed packages stay untouched.
    return [r["id"] for r in templates_in_output(provider_id)
            if str(r.get("status", "")).lower() in ("failed", "partial", "warning", "incomplete")]


def handle_line(text):
    with run.lock:
        n = len(run.lines)
        run.lines.append({"n": n, "t": datetime.now().strftime("%H:%M:%S"), "text": text})
        for rx, fn in RULES:
            m = rx.search(text)
            if m:
                run.step = fn(m, run.counts)
                break
        if run.log_file:
            run.log_file.write(f"{run.lines[-1]['t']}  {text}\n")
            run.log_file.flush()


def reader(proc):
    for raw in proc.stdout:
        text = raw.rstrip("\r\n")
        if text.strip():
            handle_line(text)
    code = proc.wait()
    with run.lock:
        run.exit_code = code
        run.ended = time.time()
        if run.stop_requested:
            run.state = "stopped"
        else:
            run.state = "finished" if code == 0 else "failed"
    handle_line(f"[studio] crawler process ended (exit code {code}) - status: {run.state}")
    if run.log_file:
        run.log_file.close()
        run.log_file = None


def local_cleanup_worker(source_root, cleaned_root, model_id):
    try:
        watch_and_clean(source_root, cleaned_root, lambda: run.state == "running",
                        handle_line, base_url=LOCAL_AI_BASE_URL, model=model_id, poll_seconds=3.0)
    except Exception as exc:
        handle_line(f"[cleanup] worker error: {type(exc).__name__}: {str(exc)[:220]}")
    finally:
        run.cleanup_running = False
        handle_line("[cleanup] local standalone cleanup queue finished")


def kill_tree(proc):
    """Stop the crawler AND the Chrome/chromedriver processes it started."""
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception as e:
        handle_line(f"[studio] could not stop cleanly: {e}")


def stop_running_crawl():
    """Used when the app itself is closed: never leave a crawler/Chrome running unattended."""
    proc = run.proc
    if proc and proc.poll() is None:
        kill_tree(proc)


atexit.register(stop_running_crawl)
if IS_WIN:
    try:                                   # also fires when the console window is closed with X
        import ctypes
        _Handler = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)

        def _on_console_event(event):
            if event in (0, 1, 2, 5, 6):   # Ctrl+C, Ctrl+Break, close, logoff, shutdown
                stop_running_crawl()
            return False
        _console_handler = _Handler(_on_console_event)     # keep a reference
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_console_handler, True)
    except Exception:
        pass


def pick_port(start):
    """First free port from `start` upward (port 5000 is often taken on Windows)."""
    for p in range(start, start + 30):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return start


# ------------------------------------------------------------------- routes
@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/providers")
def providers():
    keys = ("id", "name", "description", "url_required", "url_help", "url_placeholder")
    return jsonify([{k: p.get(k, False if k == "url_required" else "") for k in keys}
                    for p in load_providers().values()])


@app.get("/api/local-ai/status")
def local_ai_status():
    """Check only the configured local LLM endpoint. This route never calls a hosted provider."""
    return jsonify(local_status(LOCAL_AI_BASE_URL))


@app.post("/api/start")
def start():
    body = request.get_json(silent=True) or {}
    pid = body.get("provider")
    urls = [u.strip() for u in (body.get("urls") or []) if u.strip()]
    mode = str(body.get("mode") or "crawl")
    template_ids = [str(x) for x in (body.get("template_ids") or []) if str(x).strip()]
    standalone_cleanup = bool(body.get("standalone_cleanup"))
    local_model_id = None
    if standalone_cleanup:
        ai_status = local_status(LOCAL_AI_BASE_URL)
        if not ai_status.get("ok"):
            return jsonify(error=("Local AI cleanup is enabled, but no local model is ready. "
                                  + ai_status.get("message", "Start the local model server and load a model."))), 400
        local_model_id = ai_status["models"][0]
    # Selected retries explicitly re-run those templates; the checkbox refreshes every discovered template.
    force_refresh = bool(body.get("force_refresh")) or mode == "retry_selected"
    provs = load_providers()
    if pid not in provs:
        return jsonify(error="Choose a provider first."), 400
    prov = provs[pid]
    if prov.get("url_required") and not urls:
        return jsonify(error=f"{prov['name']} needs a URL - paste it in the box above."), 400
    hosts = prov.get("allowed_hosts") or []
    for u in urls:
        if not re.match(r"^https?://[^\s]+$", u, re.I):
            return jsonify(error=f"Not a valid http(s) URL: {u}"), 400
        host = (urlparse(u).hostname or "").lower()
        if hosts and not any(host == h or host.endswith("." + h) for h in hosts):
            return jsonify(error=f"{u} is not a {prov['name']} address (expected {' / '.join(hosts)})."), 400
    if not os.path.isfile(prov["script_path"]):
        return jsonify(error=f"Crawler file not found: {prov['script']}"), 400

    with run.lock:
        if run.state == "running" or run.cleanup_running:
            return jsonify(error="A crawl or standalone-cleanup queue is already running. Let it finish before starting another run."), 409
        run.reset()
        run.state = "running"
        run.provider = prov["name"]
        run.started = time.time()
        run.output_dir = os.path.join(OUTPUT_ROOT, pid)
        run.provider_id = pid
        run.urls = urls
        run.mode = mode
        run.force_refresh = force_refresh
        run.standalone_cleanup = standalone_cleanup
        run.local_model_id = local_model_id
        run.run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        stamp = run.run_id
        run.log_file = open(os.path.join(LOG_DIR, f"{pid}_{stamp}.log"), "w", encoding="utf-8")

    persist_last_run()
    handle_line(f"[studio] starting {prov['name']} crawler ({prov['script']}) - mode: {mode}")
    if standalone_cleanup:
        cleaned_dir = os.path.join(STANDALONE_ROOT, pid)
        handle_line(f"[studio] local-only AI cleanup enabled with model: {local_model_id}")
        handle_line(f"[studio] standalone copies will be written to: {cleaned_dir}")
        handle_line("[studio] no cloud API or API key is used; cleanup runs on the folder-local or configured local model server")
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    if force_refresh:
        env["STUDIO_FORCE_REFRESH"] = "1"
        env["STUDIO_FORCE_RETRY_NO_PREVIEW"] = "1"
        handle_line("[studio] refresh enabled: saved templates will be re-crawled and replaced only after a new package is built")
    if mode == "retry_failed":
        template_ids = failed_template_ids(pid)
    if mode in ("retry_failed", "retry_selected"):
        if not template_ids:
            with run.lock:
                run.state = "finished"
                run.ended = time.time()
            handle_line("[studio] no failed/selected templates were available to retry")
            return jsonify(ok=True, empty=True)
        env["STUDIO_RETRY_TEMPLATE_IDS"] = json.dumps(template_ids)
        if mode == "retry_selected":
            env["STUDIO_FORCE_RETRY_NO_PREVIEW"] = "1"
        handle_line(f"[studio] retry scope: {len(template_ids)} template(s); completed templates will be preserved")
    cmd = [sys.executable, "-u", os.path.join(BASE, "runner.py"),
           prov["script_path"], run.output_dir]
    if urls:
        cmd.append(json.dumps(urls))
    try:
        proc = subprocess.Popen(
            cmd, cwd=os.path.dirname(prov["script_path"]), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            start_new_session=not IS_WIN)
    except Exception as e:
        with run.lock:
            run.state = "failed"
        handle_line(f"[studio] could not start crawler: {e}")
        return jsonify(error=str(e)), 500
    run.proc = proc
    if standalone_cleanup:
        # Set before starting the reader thread so a very short crawl cannot stop UI polling
        # before the cleanup worker has a chance to start.
        run.cleanup_running = True
        cleaned_dir = os.path.join(STANDALONE_ROOT, pid)
        threading.Thread(
            target=local_cleanup_worker,
            args=(run.output_dir, cleaned_dir, local_model_id), daemon=True,
        ).start()
    threading.Thread(target=reader, args=(proc,), daemon=True).start()
    return jsonify(ok=True)


@app.get("/api/recovery")
def recovery():
    saved = read_last_run()
    pid = saved.get("provider_id")
    return jsonify(last_run={"provider_id": pid, "provider": saved.get("provider"),
                             "urls": saved.get("urls", []), "mode": saved.get("mode", "crawl"),
                             "force_refresh": saved.get("force_refresh", False),
                             "standalone_cleanup": saved.get("standalone_cleanup", False),
                             "updated_at": saved.get("updated_at")},
                   templates=templates_in_output(pid) if pid else [],
                   failed_ids=failed_template_ids(pid) if pid else [])


@app.post("/api/recovery-action")
def recovery_action():
    body = request.get_json(silent=True) or {}
    action = body.get("action")
    saved = read_last_run()
    pid = body.get("provider") or saved.get("provider_id")
    if action not in ("resume", "retry_failed", "retry_selected"):
        return jsonify(error="Unknown recovery action."), 400
    if not pid or pid not in load_providers():
        return jsonify(error="No previous run is available. Select a provider and start a crawl first."), 400
    if action == "retry_selected":
        ids = [str(x) for x in (body.get("template_ids") or []) if str(x).strip()]
        if not ids:
            return jsonify(error="Select at least one template to retry."), 400
    else:
        ids = []
    urls = saved.get("urls", []) if saved.get("provider_id") == pid else []
    # If prior run used the provider defaults, an empty URL list is intentional.
    return start_recovery(pid, urls, action, ids)


def start_recovery(pid, urls, action, template_ids):
    # Share the normal validation/start pipeline while retaining recovery intent.
    saved = read_last_run()
    body = {"provider": pid, "urls": urls, "mode": action if action != "resume" else "resume",
            "template_ids": template_ids, "standalone_cleanup": saved.get("standalone_cleanup", False)}
    with app.test_request_context("/api/start", method="POST", json=body):
        return start()


@app.post("/api/stop")
def stop():
    with run.lock:
        if run.state != "running" or not run.proc:
            return jsonify(error="Nothing is running."), 400
        run.stop_requested = True
        proc = run.proc
    handle_line("[studio] stop requested - ending crawler and its browser")
    kill_tree(proc)
    return jsonify(ok=True)


@app.get("/api/status")
def status():
    since = int(request.args.get("since", 0))
    with run.lock:
        end = run.ended or time.time()
        c = dict(run.counts)
        done = c["saved"] + c["failed"] + c["skipped"]
        return jsonify(
            state=run.state, cleanup_running=run.cleanup_running,
            provider=run.provider, provider_id=run.provider_id, step=run.step,
            elapsed=int(end - run.started) if run.started else 0,
            counts=c, processed=done,
            percent=round(100 * done / c["discovered"]) if c["discovered"] else None,
            output_dir=run.output_dir, exit_code=run.exit_code,
            lines=run.lines[since:], next=len(run.lines))


@app.post("/api/open-output")
def open_output():
    d = run.output_dir or os.path.join(OUTPUT_ROOT, (request.get_json(silent=True) or {}).get("provider", ""))
    os.makedirs(d, exist_ok=True)
    try:
        if IS_WIN:
            os.startfile(d)  # noqa
        elif sys.platform == "darwin":
            subprocess.Popen(["open", d])
        else:
            subprocess.Popen(["xdg-open", d])
    except Exception as e:
        return jsonify(error=str(e)), 500
    return jsonify(ok=True, path=d)


@app.post("/api/open-cleaned-output")
def open_cleaned_output():
    pid = (request.get_json(silent=True) or {}).get("provider") or run.provider_id
    if not pid or pid not in load_providers():
        return jsonify(error="Choose a provider first."), 400
    d = os.path.join(STANDALONE_ROOT, pid)
    os.makedirs(d, exist_ok=True)
    try:
        if IS_WIN:
            os.startfile(d)  # noqa
        elif sys.platform == "darwin":
            subprocess.Popen(["open", d])
        else:
            subprocess.Popen(["xdg-open", d])
    except Exception as e:
        return jsonify(error=str(e)), 500
    return jsonify(ok=True, path=d)


if __name__ == "__main__":
    wanted = int(os.environ.get("STUDIO_PORT", 5000))
    port = pick_port(wanted)
    if port != wanted:
        print(f"Port {wanted} is in use - using {port} instead.")
    print(f"Website Crawler Studio is running at http://127.0.0.1:{port}  (press Ctrl+C or close this window to stop)")
    if not os.environ.get("STUDIO_NO_BROWSER"):
        threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
