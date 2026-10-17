"""Folder-local Qwen model installer and launcher for Website Crawler Studio (Windows).

No hosted LLM API is used. `install` downloads a pinned GGUF and the official llama.cpp
Windows CPU runtime into this project's LLM folder. `run` starts the local model server,
waits for readiness, launches app.py, and shuts the model server down on exit.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Iterable

import requests

ROOT = Path(__file__).resolve().parent
LLM_DIR = ROOT / "LLM"
MODEL_DIR = LLM_DIR / "models"
RUNTIME_DIR = LLM_DIR / "bin" / "runtime"
LOG_DIR = ROOT / "logs"
MODEL_NAME = "Qwen2.5-Coder-3B-Instruct-Q4_K_M.gguf"
MODEL_PATH = MODEL_DIR / MODEL_NAME
MODEL_URL = (
    "https://huggingface.co/bartowski/Qwen2.5-Coder-3B-Instruct-GGUF/"
    "resolve/main/Qwen2.5-Coder-3B-Instruct-Q4_K_M.gguf"
)
MODEL_SHA256 = "3da3afe6cf5c674ac195803ea0dd6fee7e1c228c2105c1ce8c66890d1d4ab460"
MODEL_MIN_BYTES = 1_800_000_000
GITHUB_RELEASES_API = "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=20"
DEFAULT_LLM_PORT = 1234
DEFAULT_CONTEXT_SIZE = 4096
DEFAULT_THREADS = 2  # Conservative for an older 6th-gen i3 while Chrome is crawling.


def say(message: str) -> None:
    print(f"\n[local-ai] {message}", flush=True)


def fail(message: str, code: int = 1) -> None:
    print(f"\nERROR: {message}", file=sys.stderr, flush=True)
    raise SystemExit(code)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def model_file_looks_valid(path: Path) -> bool:
    """Fast startup check; avoid hashing 1.9 GB on every app launch."""
    try:
        if not path.is_file() or path.stat().st_size < MODEL_MIN_BYTES:
            return False
        with path.open("rb") as handle:
            return handle.read(4) == b"GGUF"
    except OSError:
        return False


def valid_model(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < MODEL_MIN_BYTES:
        return False
    say("Verifying the model SHA-256 (this reads the 1.9 GB file once)...")
    actual = sha256_file(path)
    if actual.lower() != MODEL_SHA256:
        say(f"Model checksum mismatch: {actual}")
        return False
    return True


def download_resumable(url: str, destination: Path, expected_sha256: str, min_bytes: int) -> None:
    """Download to .part, support HTTP Range resume, then verify before promotion."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    if destination.is_file():
        if destination.stat().st_size >= min_bytes and sha256_file(destination).lower() == expected_sha256.lower():
            say(f"Verified existing file: {destination.relative_to(ROOT)}")
            return
        say("An existing model file is incomplete or failed checksum validation; re-downloading it.")
        destination.unlink(missing_ok=True)

    last_error: Exception | None = None
    for attempt in range(1, 4):
        offset = partial.stat().st_size if partial.exists() else 0
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        say((f"Resuming model download at {offset / 1e9:.2f} GB (attempt {attempt}/3)...")
             if offset else f"Downloading the local model (attempt {attempt}/3). This is about 1.93 GB.")
        try:
            with requests.get(url, headers=headers, stream=True, allow_redirects=True,
                              timeout=(30, 180)) as response:
                if offset and response.status_code == 416:
                    response.close()
                    partial.unlink(missing_ok=True)
                    continue
                response.raise_for_status()
                append = bool(offset and response.status_code == 206)
                mode = "ab" if append else "wb"
                if offset and not append:
                    offset = 0  # Server ignored Range; restart from byte zero.
                total_header = response.headers.get("Content-Length")
                try:
                    total = offset + int(total_header) if total_header else None
                except ValueError:
                    total = None
                written = offset
                next_report = written + 100 * 1024 * 1024
                with partial.open(mode) as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        handle.write(chunk)
                        written += len(chunk)
                        if written >= next_report:
                            if total:
                                print(f"  {written / 1e9:.2f}/{total / 1e9:.2f} GB", flush=True)
                            else:
                                print(f"  {written / 1e9:.2f} GB downloaded", flush=True)
                            next_report = written + 100 * 1024 * 1024
            size = partial.stat().st_size if partial.exists() else 0
            if size < min_bytes:
                raise RuntimeError(f"Downloaded file is too small ({size:,} bytes); the transfer may be incomplete.")
            say("Checking model SHA-256...")
            actual = sha256_file(partial)
            if actual.lower() != expected_sha256.lower():
                partial.unlink(missing_ok=True)
                raise RuntimeError(f"Model SHA-256 mismatch. Expected {expected_sha256}; got {actual}. Partial file removed.")
            os.replace(partial, destination)
            say(f"Model saved and verified: {destination.relative_to(ROOT)}")
            return
        except Exception as exc:
            last_error = exc
            say(f"Download attempt {attempt} failed: {type(exc).__name__}: {str(exc)[:220]}")
            if attempt < 3:
                time.sleep(2 * attempt)
    raise RuntimeError(f"Model download did not complete after 3 attempts: {last_error}")


def select_windows_cpu_asset(assets: Iterable[dict]) -> dict:
    """Select an official prebuilt Windows x64 CPU ZIP, never a GPU/ARM/source build."""
    candidates = []
    for asset in assets:
        name = str(asset.get("name", "")).lower()
        if not name.endswith(".zip") or "win-cpu-x64" not in name:
            continue
        if any(bit in name for bit in ("cuda", "vulkan", "rocm", "openvino", "sycl", "mtmd", "arm64", "source")):
            continue
        if asset.get("browser_download_url"):
            candidates.append(asset)
    if not candidates:
        raise RuntimeError(
            "No compatible Windows x64 CPU ZIP was found in this llama.cpp release. "
            "The installer scans recent releases, including prereleases, because the stable /latest endpoint "
            "may not include a downloadable CPU runtime. Check https://github.com/ggml-org/llama.cpp/releases."
        )
    candidates.sort(key=lambda a: ("bin-win-cpu-x64" not in str(a.get("name", "")).lower(), len(str(a.get("name", "")))))
    return candidates[0]


def select_windows_cpu_release(releases: Iterable[dict]) -> tuple[dict, dict]:
    """Return the newest recent release carrying a Windows x64 CPU runtime asset.

    Do not use GitHub's /releases/latest endpoint here: it excludes prereleases, and
    llama.cpp publishes its current prebuilt binaries on prereleases/nightly builds.
    """
    for release in releases:
        try:
            asset = select_windows_cpu_asset(release.get("assets", []))
        except RuntimeError:
            continue
        return release, asset
    raise RuntimeError(
        "No compatible Windows x64 CPU ZIP was found in the 20 most recent llama.cpp releases. "
        "Open https://github.com/ggml-org/llama.cpp/releases and verify that a Windows x64 (CPU) asset is available."
    )


def safe_extract_zip(archive: Path, destination: Path) -> None:
    """Extract only paths contained within destination."""
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with zipfile.ZipFile(archive) as zf:
        for member in zf.infolist():
            normalized = member.filename.replace("\\", "/")
            posix = PurePosixPath(normalized)
            windows = PureWindowsPath(normalized)
            if posix.is_absolute() or windows.is_absolute() or windows.drive or ".." in posix.parts:
                raise RuntimeError(f"Refusing unsafe path in runtime ZIP: {member.filename!r}")
            target = (destination / Path(*posix.parts)).resolve()
            if target != root and root not in target.parents:
                raise RuntimeError(f"Refusing runtime ZIP path outside destination: {member.filename!r}")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)


def find_server(runtime_dir: Path = RUNTIME_DIR) -> Path | None:
    matches = sorted(runtime_dir.rglob("llama-server.exe")) if runtime_dir.exists() else []
    return matches[0] if matches else None


def runtime_env(server_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    dll_dirs = {server_path.parent}
    try:
        for dll in server_path.parent.rglob("*.dll"):
            dll_dirs.add(dll.parent)
    except OSError:
        pass
    env["PATH"] = os.pathsep.join(str(p) for p in sorted(dll_dirs, key=str)) + os.pathsep + env.get("PATH", "")
    return env


def verify_server(server: Path) -> bool:
    try:
        result = subprocess.run([str(server), "--version"], cwd=str(server.parent),
                                env=runtime_env(server), capture_output=True, text=True,
                                timeout=30, check=False)
        if result.returncode == 0:
            return True
        print((result.stderr or result.stdout or "").strip()[-600:])
        return False
    except Exception as exc:
        say(f"llama-server version check failed: {type(exc).__name__}: {str(exc)[:180]}")
        return False


def install_runtime() -> Path:
    server = find_server()
    if server and verify_server(server):
        say(f"Using existing llama.cpp runtime: {server.relative_to(ROOT)}")
        return server

    if RUNTIME_DIR.exists():
        say("Removing an incomplete or incompatible runtime folder before reinstalling.")
        shutil.rmtree(RUNTIME_DIR, ignore_errors=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

    say("Searching recent official llama.cpp releases for a Windows x64 CPU runtime...")
    response = requests.get(GITHUB_RELEASES_API,
                            headers={"Accept": "application/vnd.github+json", "User-Agent": "WebsiteCrawlerStudioLocal"},
                            timeout=(20, 60))
    response.raise_for_status()
    releases = response.json()
    if not isinstance(releases, list):
        raise RuntimeError("GitHub did not return the expected list of llama.cpp releases.")
    release, asset = select_windows_cpu_release(releases)
    release_label = str(release.get("tag_name") or release.get("name") or "release")
    say(f"Selected official runtime: {asset['name']} (release {release_label})")
    with tempfile.TemporaryDirectory(prefix="website-crawler-llama-") as temp:
        archive = Path(temp) / "llama-runtime.zip"
        with requests.get(asset["browser_download_url"], stream=True, timeout=(30, 120)) as download:
            download.raise_for_status()
            with archive.open("wb") as handle:
                for chunk in download.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        handle.write(chunk)
        digest = str(asset.get("digest") or "")
        if digest.lower().startswith("sha256:"):
            actual = sha256_file(archive)
            expected = digest.split(":", 1)[1].lower()
            if actual.lower() != expected:
                raise RuntimeError(f"Runtime ZIP SHA-256 mismatch: expected {expected}, got {actual}")
        safe_extract_zip(archive, RUNTIME_DIR)

    server = find_server()
    if not server:
        raise RuntimeError(f"The official runtime ZIP did not contain llama-server.exe: {RUNTIME_DIR}")
    if not verify_server(server):
        raise RuntimeError(
            "The downloaded llama-server.exe could not start. Install the Microsoft Visual C++ 2015-2022 "
            "Redistributable (x64) from https://aka.ms/vs/17/release/vc_redist.x64.exe, then rerun setup_local.bat."
        )
    say(f"Runtime installed: {server.relative_to(ROOT)}")
    return server


def install() -> int:
    if os.name != "nt":
        fail("This local package is configured for Windows x64. Use a separate Linux deployment package for a VPS.")
    machine = os.environ.get("PROCESSOR_ARCHITECTURE", "").lower()
    if machine and machine not in ("amd64", "x86_64"):
        fail(f"This package expects Windows x64; detected PROCESSOR_ARCHITECTURE={machine!r}.")
    free = shutil.disk_usage(ROOT).free
    if free < 3_500_000_000:
        fail(f"Please free at least 3.5 GB on the drive containing this project. Free now: {free / 1e9:.2f} GB.")

    LLM_DIR.mkdir(parents=True, exist_ok=True)
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    (LLM_DIR / "bin").mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    say("Installing the local CPU inference runtime...")
    install_runtime()
    say("Installing/verifying Qwen2.5-Coder-3B-Instruct Q4_K_M...")
    download_resumable(MODEL_URL, MODEL_PATH, MODEL_SHA256, MODEL_MIN_BYTES)
    # download_resumable validates size and SHA-256 both for existing and downloaded files.

    model_readme = LLM_DIR / "README_MODEL.txt"
    model_readme.write_text(
        "Website Crawler Studio — local model\n"
        "Model: Qwen2.5-Coder-3B-Instruct, GGUF Q4_K_M quantization\n"
        f"File: models/{MODEL_NAME}\n"
        f"SHA-256: {MODEL_SHA256}\n"
        f"Source: {MODEL_URL}\n"
        "Runtime: official llama.cpp Windows x64 CPU release, stored in bin/runtime\n"
        "Inference endpoint: localhost only (127.0.0.1); no hosted LLM API is used.\n"
        "Review the model card and license terms before redistributing the model file.\n",
        encoding="utf-8",
    )
    say("Setup completed successfully. Double-click run.bat to start the crawler and local model.")
    return 0


def free_port(start: int = DEFAULT_LLM_PORT, attempts: int = 12) -> int:
    for port in range(start, start + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No free local model port between {start} and {start + attempts - 1}.")


def read_log_tail(path: Path, lines: int = 35) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(content[-lines:])
    except OSError:
        return "(No model log was created.)"


def run_local() -> int:
    if os.name != "nt":
        fail("run.bat / local mode is for Windows. Use the VPS package for Linux.")
    python = ROOT / ".venv" / "Scripts" / "python.exe"
    app = ROOT / "app.py"
    if not python.is_file():
        fail("Python environment is missing. Run setup_local.bat first.")
    if not model_file_looks_valid(MODEL_PATH):
        fail("The 3B local model is missing or invalid. Run setup_local.bat to download/repair it.")
    server = find_server()
    if not server or not verify_server(server):
        fail("The local llama.cpp runtime is missing or cannot start. Run setup_local.bat to repair it.")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    port = free_port()
    base_url = f"http://127.0.0.1:{port}/v1"
    env = os.environ.copy()
    env.update({
        "STUDIO_LLM_BASE_URL": base_url,
        "STUDIO_LLM_TIMEOUT": os.environ.get("STUDIO_LLM_TIMEOUT", "600"),
        "STUDIO_LLM_MAX_FILE_CHARS": os.environ.get("STUDIO_LLM_MAX_FILE_CHARS", "4500"),
        "STUDIO_LLM_MAX_TOKENS": os.environ.get("STUDIO_LLM_MAX_TOKENS", "900"),
        "STUDIO_LLM_CONTEXT_SIZE": os.environ.get("STUDIO_LLM_CONTEXT_SIZE", str(DEFAULT_CONTEXT_SIZE)),
        "STUDIO_LLM_THREADS": os.environ.get("STUDIO_LLM_THREADS", str(DEFAULT_THREADS)),
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
    })
    try:
        context_size = max(2048, int(env["STUDIO_LLM_CONTEXT_SIZE"]))
        threads = max(1, int(env["STUDIO_LLM_THREADS"]))
    except ValueError:
        fail("STUDIO_LLM_CONTEXT_SIZE and STUDIO_LLM_THREADS must be integers.")

    cmd = [str(server), "-m", str(MODEL_PATH), "--host", "127.0.0.1", "--port", str(port),
           "-c", str(context_size), "-t", str(threads), "--parallel", "1"]
    model_log_path = LOG_DIR / "llama-server.log"
    say(f"Starting the local 3B model (context={context_size}, CPU threads={threads})...")
    model_log = model_log_path.open("w", encoding="utf-8", errors="replace")
    server_proc: subprocess.Popen | None = None
    app_proc: subprocess.Popen | None = None
    try:
        server_proc = subprocess.Popen(cmd, cwd=str(server.parent), env=runtime_env(server),
                                       stdout=model_log, stderr=subprocess.STDOUT,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        ready = False
        for seconds in range(180):
            if server_proc.poll() is not None:
                break
            try:
                response = requests.get(f"{base_url}/models", timeout=2)
                if response.ok and response.json().get("data"):
                    ready = True
                    break
            except Exception:
                pass
            if seconds and seconds % 10 == 0:
                print(f"[local-ai] Loading model... {seconds}s", flush=True)
            time.sleep(1)
        if not ready:
            model_log.flush()
            if server_proc.poll() is None:
                server_proc.terminate()
                try:
                    server_proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    server_proc.kill()
            fail("The local model did not become ready. Check logs/llama-server.log. Last log lines:\n" + read_log_tail(model_log_path))

        say("Local model is ready. No cloud model/API is used.")
        say("Starting Website Crawler Studio. The browser should open automatically.")
        say("Enable 'Create a standalone-cleaned copy using local AI' when you want the LLM cleanup stage.")
        app_proc = subprocess.Popen([str(python), "-u", str(app)], cwd=str(ROOT), env=env)
        return_code = app_proc.wait()
        return return_code
    except KeyboardInterrupt:
        say("Shutdown requested.")
        return 0
    finally:
        if app_proc and app_proc.poll() is None:
            app_proc.terminate()
            try:
                app_proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                app_proc.kill()
        if server_proc and server_proc.poll() is None:
            say("Stopping the local model server...")
            server_proc.terminate()
            try:
                server_proc.wait(timeout=12)
            except subprocess.TimeoutExpired:
                server_proc.kill()
                server_proc.wait(timeout=5)
        model_log.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Local-only LLM setup and launch for Website Crawler Studio")
    parser.add_argument("command", choices=("install", "run"), help="install the model/runtime or launch the crawler")
    args = parser.parse_args()
    try:
        return install() if args.command == "install" else run_local()
    except requests.RequestException as exc:
        fail(f"Network/download error: {type(exc).__name__}: {str(exc)[:240]}. Check the connection and rerun setup_local.bat.")
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as exc:
        fail(f"{type(exc).__name__}: {str(exc)[:400]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
