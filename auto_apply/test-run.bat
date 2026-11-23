@echo off
rem Fills in the forms but never clicks Submit, so you can check its work.
cd /d "%~dp0"
py apply.py --dry-run %*
echo.
pause
