@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul

set ROOT=%~dp0
pushd "%ROOT%"

set VENV_PY=%ROOT%backend\venv\Scripts\python.exe
if not exist "%VENV_PY%" (
    echo [ERROR] venv not found. Please run init.cmd first.
    popd & exit /b 1
)

set RESTART_DELAY=5
set PORT=61000

:loop
:: Port cleanup ??kill any existing process on the port before starting
for /f "tokens=5" %%a in ('netstat -ano ^| findstr "LISTENING" ^| findstr ":%PORT% " 2^>nul') do (
    if not "%%a"=="0" (
        echo [%date% %time%] Port %PORT% occupied by PID %%a, killing...
        taskkill /PID %%a /F >nul 2>&1
        timeout /t 1 /nobreak >nul
    )
)

echo [%date% %time%] Starting OpenClaw Hub Manager on http://localhost:%PORT% ...
"%VENV_PY%" "%ROOT%backend\app.py"
set "exitcode=!errorlevel!"

echo [%date% %time%] Hub manager exited (code: !exitcode!). Restarting in %RESTART_DELAY%s...
timeout /t %RESTART_DELAY% /nobreak >nul
goto loop
