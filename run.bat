@echo off
rem Jal-Vigil launcher for Windows.
rem   run.bat          start on port 8000 and open the browser
rem   run.bat 8001     use another port
rem   set NO_BROWSER=1 before running to skip opening the browser
setlocal EnableExtensions
title Jal-Vigil
cd /d "%~dp0"

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8000"
set "VENV_PY=.venv\Scripts\python.exe"

echo.
echo  Jal-Vigil
echo  =========
echo.

rem --- 1. Python 3.10 or newer -------------------------------------------------------------------
if exist "%VENV_PY%" goto have_venv
set "PY="
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "PY=python"
if not defined PY py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "PY=py -3"
if not defined PY (
  echo  [x] Python 3.10 or newer was not found.
  echo      Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
  goto fail
)

rem --- 2. Private virtual environment, so the app's packages never clash with others -------------
echo  [1/4] Creating a virtual environment in .venv ...
%PY% -m venv .venv
if errorlevel 1 (
  echo  [x] Could not create the virtual environment.
  goto fail
)

:have_venv
rem --- 3. Install packages, only when requirements.txt changed since the last install ------------
fc /b "backend\requirements.txt" ".venv\requirements.installed" >nul 2>nul
if errorlevel 1 (
  echo  [2/4] Installing packages, the first time takes a minute ...
  "%VENV_PY%" -m pip install --disable-pip-version-check -q -r backend\requirements.txt
  if errorlevel 1 (
    echo  [x] Package install failed. Check your internet connection and try again.
    goto fail
  )
  copy /y "backend\requirements.txt" ".venv\requirements.installed" >nul
) else (
  echo  [2/4] Packages are up to date.
)

rem --- 4. API keys -------------------------------------------------------------------------------
if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo  [3/4] Created .env from .env.example. Add your API keys there; the app runs without them,
  echo        with template alerts in English only.
) else (
  echo  [3/4] Using API keys from .env
)

rem --- 5. Port check -----------------------------------------------------------------------------
netstat -ano | findstr /r /c:":%PORT% .*LISTENING" >nul
if not errorlevel 1 (
  echo  [x] Port %PORT% is already in use, maybe the app is already running.
  echo      Open http://localhost:%PORT% or start on another port: run.bat 8001
  goto fail
)

rem --- 6. Start ----------------------------------------------------------------------------------
echo  [4/4] Starting on http://localhost:%PORT%   (press Ctrl+C to stop)
echo.
if not defined NO_BROWSER (
  start "" /b powershell -NoProfile -WindowStyle Hidden -Command "for ($i = 0; $i -lt 60; $i++) { try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 'http://127.0.0.1:%PORT%/api/health' | Out-Null; Start-Process 'http://localhost:%PORT%/'; break } catch { Start-Sleep 1 } }"
)
"%VENV_PY%" -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port %PORT%
goto end

:fail
echo.
pause
exit /b 1

:end
endlocal
