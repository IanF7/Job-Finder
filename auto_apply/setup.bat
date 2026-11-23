@echo off
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo Python isn't installed. Get it from https://www.python.org/downloads/windows/
  echo During install, check "Add python.exe to PATH". Then run this again.
  pause
  exit /b 1
)
echo Installing the auto-applier's libraries...
py -m pip install --upgrade pip
py -m pip install -r requirements.txt
echo Installing the browser it uses...
py -m playwright install chromium
echo.
echo Setup finished. Next: open config.yaml in Notepad and fill in every TODO.
pause
