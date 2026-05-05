@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

echo ==============================================
echo   Synergy Fresh Installer (Windows)
echo ==============================================
echo.

:: --- Elevate to admin if needed ---
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo Requesting administrator privileges...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

set "PY_VER=3.12.8"
set "NODE_VER=20.18.0"
set "PY_EXE=py"
set "NODE_OK=0"
set "PY_OK=0"

:: --- Check winget ---
where winget >nul 2>&1
if %errorlevel% neq 0 (
  echo [ERROR] winget is not installed on this PC.
  echo Please install App Installer from Microsoft Store, then rerun.
  pause
  exit /b 1
)

echo [1/7] Checking Python...
where py >nul 2>&1
if %errorlevel%==0 (
  for /f "tokens=2 delims= " %%A in ('py -3.12 --version 2^>nul') do set "FOUND_PY=%%A"
  if defined FOUND_PY (
    echo Found Python !FOUND_PY!
    set "PY_OK=1"
  )
)
if "%PY_OK%"=="0" (
  echo Installing Python %PY_VER%...
  winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements --silent
  if errorlevel 1 (
    echo [ERROR] Python install failed.
    pause
    exit /b 1
  )
)

echo [2/7] Refreshing environment...
call "%SystemRoot%\System32\setx.exe" TEMP "%TEMP%" >nul 2>&1
set "PATH=%PATH%;%LocalAppData%\Programs\Python\Python312;%LocalAppData%\Programs\Python\Python312\Scripts;%ProgramFiles%\nodejs"

echo [3/7] Checking Node.js...
where node >nul 2>&1
if %errorlevel%==0 (
  for /f "tokens=*" %%A in ('node --version 2^>nul') do set "FOUND_NODE=%%A"
  if defined FOUND_NODE (
    echo Found Node !FOUND_NODE!
    set "NODE_OK=1"
  )
)
if "%NODE_OK%"=="0" (
  echo Installing Node.js %NODE_VER% LTS...
  winget install -e --id OpenJS.NodeJS.LTS --accept-package-agreements --accept-source-agreements --silent
  if errorlevel 1 (
    echo [ERROR] Node.js install failed.
    pause
    exit /b 1
  )
)

echo [4/7] Verifying npm...
where npm >nul 2>&1
if %errorlevel% neq 0 (
  set "PATH=%PATH%;%ProgramFiles%\nodejs"
)
where npm >nul 2>&1
if %errorlevel% neq 0 (
  echo [ERROR] npm not found after Node install.
  pause
  exit /b 1
)

echo [5/7] Installing Python backend dependencies...
if exist backend\requirements.txt (
  py -3.12 -m pip install --upgrade pip setuptools wheel
  py -3.12 -m pip install -r backend\requirements.txt
  if errorlevel 1 (
    echo [ERROR] Python dependency install failed.
    pause
    exit /b 1
  )
  if errorlevel 1 (
    echo [ERROR] Python dependency install failed.
    pause
    exit /b 1
  )
) else (
  echo [WARN] backend\requirements.txt not found - skipping backend deps.
)

echo [6/7] Installing Electron dependencies...
if exist electron\package.json (
  pushd electron
  call npm install
  if errorlevel 1 (
    popd
    echo [ERROR] npm install failed.
    pause
    exit /b 1
  )
  popd
) else (
  echo [WARN] electron\package.json not found - skipping frontend deps.
)

echo [7/7] Final verification...
py -3.12 --version
node --version
npm --version

echo.
echo ==============================================
echo Installation complete.
echo Python and Node were verified/installed,
echo dependencies installed, and PATH refreshed.
echo ==============================================
echo.
pause
exit /b 0
