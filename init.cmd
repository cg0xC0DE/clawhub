@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul

set ROOT=%~dp0
pushd "%ROOT%"

echo.
echo "============================================================"
echo "  OpenClaw Hub Manager - Initialization"
echo "  Project: hougong-hub"
echo "============================================================"
echo.

:: ============================================================
::  winget pre-check
:: ============================================================
set WINGET_AVAILABLE=0
winget --version >nul 2>&1
if not errorlevel 1 set WINGET_AVAILABLE=1

:: ============================================================
::  Phase 1: Environment Check
:: ============================================================
echo "============================================================"
echo "  Phase 1: Environment Check"
echo "============================================================"
echo.

:: ---- Python (Required) ----
:CHECK_PYTHON
python --version >nul 2>&1
if not errorlevel 1 goto PYTHON_OK

echo "[MISSING] Python is not detected."
echo "         Impact: The backend (Flask API) cannot run at all."
echo "                 hub manager UI will be unavailable."
if %WINGET_AVAILABLE%==0 goto PYTHON_MANUAL
set /p INSTALL_PY="  Install Python via winget? (Y/N): "
if /i not "!INSTALL_PY!"=="Y" goto PYTHON_MANUAL
echo "[INFO] Installing Python via winget..."
winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements
echo "[INFO] Re-checking Python..."
goto CHECK_PYTHON

:PYTHON_MANUAL
echo "[INFO] Please install Python 3.10+ from https://www.python.org/downloads/"
echo "       Ensure 'Add Python to PATH' is checked during installation."
set /p _="  After installing, press ENTER to re-check..."
goto CHECK_PYTHON

:PYTHON_OK
echo "[OK] Python detected."

:: ---- pip (Required) ----
:CHECK_PIP
python -m pip --version >nul 2>&1
if not errorlevel 1 goto PIP_OK

echo "[MISSING] pip is not detected."
echo "         Impact: Cannot install Python dependencies (flask, psutil, etc.)."
echo "                 Backend will fail to start."
echo "[INFO] Attempting: python -m ensurepip --upgrade"
python -m ensurepip --upgrade >nul 2>&1
python -m pip --version >nul 2>&1
if not errorlevel 1 goto PIP_OK
set /p _="  pip still missing. Fix manually, then press ENTER to re-check..."
goto CHECK_PIP

:PIP_OK
echo "[OK] pip detected."

:: ---- git (Optional) ----
:CHECK_GIT
git --version >nul 2>&1
if not errorlevel 1 goto GIT_OK

echo "[WARN] git is not detected."
echo "       Impact: Cannot run 'hub.ps1 update' (which uses npm/git under the hood)."
echo "               Core hub manager functionality is unaffected."
if %WINGET_AVAILABLE%==0 goto GIT_SKIP
set /p INSTALL_GIT="  Install git via winget? (Y/N): "
if /i not "!INSTALL_GIT!"=="Y" goto GIT_SKIP
winget install --id Git.Git -e --accept-source-agreements --accept-package-agreements
echo "       Please restart this script after installation to refresh PATH."
goto GIT_DONE

:GIT_SKIP
echo "       Install manually: https://git-scm.com/download/win"
:GIT_OK
:GIT_DONE
echo "[OK] git check complete."

:: ---- openclaw (Optional) ----
:CHECK_OPENCLAW
where openclaw >nul 2>&1
if not errorlevel 1 goto OPENCLAW_OK

echo "[WARN] openclaw CLI is not detected."
echo "       Impact: Cannot start/stop Gateway instances from hub.ps1."
echo "               The hub manager UI will still run but Gateway control will fail."
echo "       Install: npm install -g openclaw"
goto OPENCLAW_DONE
:OPENCLAW_OK
:OPENCLAW_DONE
echo "[OK] openclaw check complete."

echo.
echo "[OK] Phase 1 complete. All required dependencies satisfied."
echo.

:: ============================================================
::  Phase 2: Automated Installation
:: ============================================================
echo "============================================================"
echo "  Phase 2: Automated Installation"
echo "============================================================"
echo.

:: ---- Create backend venv ----
if exist "backend\venv" goto SKIP_VENV
echo "[INFO] Creating Python virtual environment in backend\venv ..."
python -m venv backend\venv
if errorlevel 1 (
    echo "[ERROR] Failed to create virtual environment."
    popd & exit /b 1
)
echo "[OK] Virtual environment created."
goto AFTER_VENV
:SKIP_VENV
echo "[SKIP] backend\venv already exists."
:AFTER_VENV

:: ---- Activate venv ----
call backend\venv\Scripts\activate.bat

:: ---- Install Python dependencies ----
echo "[INFO] Installing Python dependencies from backend\requirements.txt ..."
python -m pip install -r backend\requirements.txt
if errorlevel 1 (
    echo "[ERROR] Failed to install Python dependencies."
    popd & exit /b 1
)
echo "[OK] Python dependencies installed."

:: ---- Create required directories ----
if not exist "shared" mkdir shared
if not exist "backups" mkdir backups
if not exist "gateways" mkdir gateways
if not exist "frontend" mkdir frontend
echo "[OK] Project directories verified."

:: ---- Verify gateways.json exists ----
if exist "gateways.json" goto GW_JSON_OK
echo "[WARN] gateways.json not found. Hub will start with no gateways configured."
echo "       You can add gateways via the web UI after startup."
:GW_JSON_OK
echo "[OK] gateways.json present."

echo.
echo "[OK] Phase 2 complete."
echo.

:: ============================================================
::  Phase 3: Credential Configuration
:: ============================================================
echo "============================================================"
echo "  Phase 3: Configuration"
echo "============================================================"
echo.
echo "[INFO] Gateway credentials (Telegram Bot Tokens) are configured"
echo "       per-gateway via the web UI or hub.ps1 set-token command."
echo "       No global credentials are required at this stage."
echo.
echo "  To configure a gateway token after startup:"
echo "    Option A: Open http://localhost:61000 -> click gateway -> Token button"
echo "    Option B: .\hub.ps1 set-token <gateway-id> <bot_token>"
echo.
echo "[OK] Phase 3 complete. No global credentials required."
echo.

:: ============================================================
::  Done
:: ============================================================
echo "============================================================"
echo "  Initialization complete!"
echo "============================================================"
echo.
echo "  Next steps:"
echo "    1. Start the hub manager:  start_backend.cmd"
echo "    2. Open the UI:            http://localhost:61000"
echo "    3. Set gateway tokens:     UI -> gateway -> Token button"
echo "    4. Start gateways:         UI -> Start All  (or hub.ps1 start)"
echo.

set /p START_NOW="  Start the hub manager now? (Y/N): "
if /i "!START_NOW!"=="Y" (
    echo "[INFO] Starting hub manager..."
    start "OpenClaw Hub Manager" cmd /k "call backend\venv\Scripts\activate.bat && python backend\app.py"
    timeout /t 2 /nobreak >nul
    start http://localhost:61000
)

popd
endlocal
