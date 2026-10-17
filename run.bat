@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Local setup is missing. Please run setup_local.bat first.
    pause
    exit /b 1
)
if not exist "LLM\models\Qwen2.5-Coder-3B-Instruct-Q4_K_M.gguf" (
    echo Local model is missing. Please run setup_local.bat first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" local_llm.py run
if errorlevel 1 echo Website Crawler Studio stopped with an error. Check logs\llama-server.log if the model failed to load.
pause
