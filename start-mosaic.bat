@echo off
rem Mosaic one-click launcher (Windows): sets up on first run, then starts the web app.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [Mosaic] First run: creating the Python environment...
  python -m venv .venv || goto :error
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip || goto :error
  ".venv\Scripts\python.exe" -m pip install --quiet -e ".[dev]" || goto :error
)

if not exist ".env" (
  copy /y ".env.example" ".env" >nul
  echo [Mosaic] Created .env - add ANTHROPIC_API_KEY and your database keys, save, then run this again.
  notepad ".env"
  exit /b 0
)

set "PORT=8000"
for /f "tokens=1,* delims==" %%a in ('findstr /b /c:"BIOPHA_API_PORT=" ".env"') do if not "%%b"=="" set "PORT=%%b"

echo [Mosaic] Starting on http://127.0.0.1:%PORT%  (close this window or press Ctrl+C to stop)
start "" cmd /c "timeout /t 4 >nul & start http://127.0.0.1:%PORT%"
".venv\Scripts\python.exe" -m mosaic serve --port %PORT%
exit /b %errorlevel%

:error
echo [Mosaic] Setup failed. Make sure Python 3.11+ is installed and on PATH, then try again.
pause
exit /b 1
