# Website Crawler Studio — Local Windows + AI Cleanup

This build is intended for your local Windows x64 computer (16 GB RAM, older Intel i3). It keeps the existing crawler UI and provider crawlers, and adds a folder-local Qwen 3B model running through the official llama.cpp Windows CPU runtime.

## What you get

- Existing Website Crawler Studio UI and crawlers.
- Local AI cleanup as an optional crawl stage.
- Qwen2.5-Coder-3B-Instruct, GGUF Q4_K_M (about 1.93 GB), stored under `LLM/models/`.
- llama.cpp Windows x64 CPU runtime stored under `LLM/bin/runtime/`.
- No OpenRouter, cloud inference endpoint, hosted API key, token charges or per-day API quota.
- Raw crawl output stays under `outputs/`; standalone copies and audit reports go under `outputs_standalone/`.

## Requirements

- Windows 10/11 x64 (not Windows ARM in this package).
- Python 3.10+ installed. Python 3.11 64-bit is a good default.
- Google Chrome installed for browser-based crawling.
- At least 3.5 GB free disk space for first-time model/runtime installation; additional space is needed for crawled templates.
- 16 GB RAM recommended for this configuration. The model runs on CPU, so generation may be slow while Chrome is working. The launcher defaults to 2 inference threads and a 4096-token context to leave resources for crawling.

## First-time setup

1. Extract this ZIP to a short folder path, e.g. `C:\WebsiteCrawlerStudio`. Avoid deeply nested paths in Downloads.
2. Ensure Python 3.10+ and Google Chrome are installed.
3. Double-click `setup_local.bat`.
4. Wait for the runtime and model downloads to complete. The installer scans recent official llama.cpp releases (including prereleases) to locate the Windows x64 CPU ZIP; it does not rely on GitHub’s `/releases/latest`, which may not contain the binary. The model download is approximately 1.93 GB. The installer verifies the model SHA-256 before accepting it. If setup stops, fix the reported error and rerun `setup_local.bat`; installed Python packages and valid downloaded files are reused.
5. If the runtime reports missing Microsoft Visual C++ runtime files, install the official x64 redistributable: <https://aka.ms/vs/17/release/vc_redist.x64.exe>, then rerun setup.

## Normal use

Double-click `run.bat`. It starts the model server on a loopback address (`127.0.0.1`), waits for it to become ready, then launches the existing Studio UI in your browser. Closing the app/console stops the local model server too.

In the crawler UI, check **Create a standalone-cleaned copy using local AI** to enable cleanup. Leave it unchecked if you only want the raw crawl. The model uses your CPU and RAM; no AI request is sent to OpenRouter or another hosted model provider.

The active instructions are stored in `standalone_cleanup_prompt.txt`. The complete prompt you supplied is preserved verbatim at the start of that file; a clearly labeled compatibility note follows it because Studio currently supplies a rendered `index.html` + assets package to the cleaner, not a raw `.mhtml` file. In this workflow, the browser capture has already produced the page DOM and downloaded assets; the cleaner must not claim it extracted an MHTML archive. If you need the app to accept an uploaded raw `.mhtml` file, that requires a separate MHTML-ingestion workflow.

For Wix packages, the crawler handles static dependencies before the LLM: it downloads active stylesheet and script URLs, follows CSS `@import` and `url(...)` references, follows literal JavaScript `import`/`export ... from` and `import(...)` module references, and rewrites those references to local relative paths. The asset manifest records downloaded resources and failures. If Wix has already embedded a stylesheet's CSS inside a `<style>` element, its stale `data-href`/`data-url` source locator is removed rather than downloading and loading a duplicate copy. Ordinary page-navigation links are not mistaken for assets.

The standalone cleanup then uses the manifest for deterministic URL mapping, removes known Wix viewer/runtime bootstrap scripts and metadata, removes iframe tags as your prompt requires, and keeps the rendered page HTML/CSS. For Wix, the LLM is skipped when no active external CSS/JavaScript imports remain. If unresolved active CSS/JS imports do remain, it receives one focused review excerpt at most; it is not used for routine URL replacement and Wix vendor bundles are not bulk-sent to the model. This reduces unnecessary CPU inference while keeping source files intact. The report keeps a broad external-URL audit for reference, but harmless navigation URLs, SVG namespaces, and library license URLs do not by themselves force a review status. Other providers retain the general dependency-review path.

Original crawled packages are not modified by cleanup. Inspect `outputs_standalone/<provider>/.../cleanup-report.json` and `standalone-validation.txt`. The report contains prompt/pipeline fingerprints, model ID, LLM request counts, JSON parse status, proposed/applied/rejected edits, deterministic removals, asset rewrites, and unresolved references. If cleanup fails, a labeled `.__failed__` folder with `cleanup-error.json` is retained instead of an unexplained unfinished `.__cleaning_tmp__` folder. A prompt/model/pipeline version change invalidates the cleanup cache, so older output is not silently reused after instructions change. Set `STUDIO_FORCE_CLEANUP=1` before launch to force a re-clean of unchanged sources.

Because Wix serves different transformed URLs for the same media item, the cleaner can match Wix media IDs to already-downloaded local assets when an exact URL does not match. It preserves CSS rules and inline styles to protect the original appearance. Some Wix-dependent animations or interactions may still require a separate native implementation; these are not silently replaced with a new design.

## Logs and troubleshooting

- `logs/llama-server.log` — local model startup and runtime errors.
- `logs/` — crawler logs and last-run state.
- If the runtime download fails, rerun `setup_local.bat`; it searches the 20 most recent official llama.cpp releases for a Windows x64 CPU build. If none is available, check the official release page linked below.
- If the model doesn't load, confirm there is enough free RAM and disk space, then try again.
- If port 1234 is occupied, the launcher selects a free port in the 1234–1235 range and passes it to the app.
- To tune performance, set Windows environment variables before launching `run.bat`:
  - `STUDIO_LLM_THREADS=2` (default; try 3 or 4 only if the computer remains responsive)
  - `STUDIO_LLM_CONTEXT_SIZE=4096` (default)
  - `STUDIO_LLM_MAX_FILE_CHARS=4500` (default)
  - `STUDIO_LLM_MAX_TOKENS=900` (default)
  - `STUDIO_LLM_TIMEOUT=600` (default)
- For Python/dependency problems, rerun `setup_local.bat`.

## Sources and licensing

- Model file: <https://huggingface.co/bartowski/Qwen2.5-Coder-3B-Instruct-GGUF/blob/main/Qwen2.5-Coder-3B-Instruct-Q4_K_M.gguf>
- Official llama.cpp releases: <https://github.com/ggml-org/llama.cpp/releases>
- Microsoft VC++ Redistributable (x64): <https://aka.ms/vs/17/release/vc_redist.x64.exe>

Review the upstream model card and license terms before redistributing the model. The ZIP package does not bundle the 1.93 GB model weights; `setup_local.bat` fetches them directly from the model source.
