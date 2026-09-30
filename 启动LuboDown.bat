@echo off
rem LuboDown launcher: checks Python/deps, then runs windowless
cd /d %~dp0

where python >nul 2>nul
if errorlevel 1 (
  echo [LuboDown] Python not found. Please install Python 3.10+ and check "Add Python to PATH".
  pause
  exit /b 1
)

python -c "import webview, pystray, PIL" >nul 2>nul
if errorlevel 1 (
  echo [LuboDown] First run: installing dependencies ^(pywebview / pystray / pillow^)...
  python -m pip install -r requirements.txt
  if errorlevel 1 (
    echo [LuboDown] Dependency install failed. Check your network and retry.
    pause
    exit /b 1
  )
)

start "" pythonw -m app.main
