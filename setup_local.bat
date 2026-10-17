@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo ============================================================
echo Website Crawler Studio - Local AI setup (Windows x64)
echo ============================================================
echo This installs Python dependencies and downloads the local 3B
echo coding model plus the llama.cpp CPU runtime into the LLM folder.
echo No hosted LLM API key or subscription is required.
echo.

where py >nul 2>nul
if %errorlevel%==0 (
    set "PY=py -3"
) else (
    set "PY=python"
)

%PY% -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)"
if errorlevel 1 (
    echo ERROR: Python 3.10 or newer is required.
    echo Download Python for Windows from https://www.python.org/downloads/windows/
    echo During installation, enable the option to add Python to PATH.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating the project virtual environment...
    %PY% -m venv .venv
    if errorlevel 1 goto :failed
)

echo Installing/updating Python packages...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :failed
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :failed

echo.
echo Installing the Windows CPU runtime and downloading/verifying the 3B model...
".venv\Scripts\python.exe" local_llm.py install
if errorlevel 1 goto :failed

echo.
echo Setup finished. From now on, double-click run.bat to start the crawler and local AI.
pause
exit /b 0

:failed
echo.
echo Setup did not finish. Read the error above, fix the issue, and run setup_local.bat again.
echo Model downloads resume when the host supports HTTP Range requests.
pause
exit /b 1
