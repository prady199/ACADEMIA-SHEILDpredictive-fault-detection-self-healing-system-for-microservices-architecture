@echo off
rem Sets up on first run, then starts Academia Shield.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo First run: installing packages. This takes a minute or two.
  py -3 -m venv .venv 2>nul || python -m venv .venv
  if errorlevel 1 (
    echo Python 3.10 or newer was not found. Install it from python.org and tick "Add python.exe to PATH".
    pause
    exit /b 1
  )
  .venv\Scripts\python.exe -m pip install --upgrade pip
  .venv\Scripts\python.exe -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Installing packages failed. Check your internet connection and run this again.
    pause
    exit /b 1
  )
)
.venv\Scripts\python.exe run.py %*
pause
