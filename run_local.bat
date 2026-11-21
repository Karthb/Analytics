@echo off
REM Optional: run the screener on this Windows PC instead of, or as well as, GitHub.
REM Needs Python 3.10 or newer from https://www.python.org/downloads/ - tick "Add python.exe to PATH".
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe call :setup || goto :nopython
echo Fetching the latest NSE data - the first run downloads about 2 years and takes 10-15 minutes...
.venv\Scripts\python scripts\build_data.py
.venv\Scripts\python scripts\build_rotation.py
start "" "%~dp0site\index.html"
start "" "%~dp0site\rotation.html"
goto :eof

:setup
echo Setting up Python packages - first time only...
python -m venv .venv || exit /b 1
.venv\Scripts\python -m pip install --quiet -r requirements.txt || exit /b 1
exit /b 0

:nopython
echo Python was not found. Install it from https://www.python.org/downloads/ and run this file again.
pause
