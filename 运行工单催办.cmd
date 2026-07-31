@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

call :find_python
if errorlevel 1 goto :failed

set "TARGET_ARGS="
if not "%~1"=="" (
    if /I not "%~x1"==".xlsx" if /I not "%~x1"==".xlsm" if /I not "%~x1"==".csv" (
        echo [ERROR] Unsupported file: %~1
        goto :failed
    )
    set "TARGET_ARGS=--file "%~f1""
    echo Selected: %~nx1
    echo.
)

echo ========================================
echo        Yuanbao Work Order Reminder
echo ========================================
echo [1] Preview only
echo [2] Send all files to Yuanbao group and keep files
echo [3] Send all files to Yuanbao group and archive files
echo [4] Exit
echo.
set /p "CHOICE=Choose 1, 2, 3 or 4: "

if "%CHOICE%"=="1" goto :preview
if "%CHOICE%"=="2" goto :send
if "%CHOICE%"=="3" goto :send_keep
if "%CHOICE%"=="4" goto :done
echo [ERROR] Invalid option.
goto :failed

:preview
echo.
echo Generating preview...
"%PYTHON_EXE%" ".\work_order_reminder.py" --config ".\config.json" %TARGET_ARGS% --keep
set "RESULT=%ERRORLEVEL%"
goto :result

:send
set "KEEP_ARGS=--keep"
goto :send_start

:send_keep
set "KEEP_ARGS="

:send_start
echo.
call :find_openclaw
if errorlevel 1 goto :failed
echo Checking Gateway...
call "%OPENCLAW_CMD%" gateway status >nul 2>&1
if errorlevel 1 (
    echo Starting Gateway...
    start "" /min cmd.exe /c ""%OPENCLAW_CMD%" gateway run --force"
    timeout /t 5 /nobreak >nul
)
echo Sending. Keep this window open...
"%PYTHON_EXE%" ".\work_order_reminder.py" --config ".\config.json" %TARGET_ARGS% --send --force-resend %KEEP_ARGS%
set "RESULT=%ERRORLEVEL%"
goto :result

:result
echo.
if "%RESULT%"=="0" (
    echo [OK] Completed.
) else (
    echo [FAILED] Exit code: %RESULT%
)
echo.
pause
goto :done

:find_python
set "PYTHON_EXE="
if exist "%~dp0audio_quality_runtime\.venv\Scripts\python.exe" (
    set "PYTHON_EXE=%~dp0audio_quality_runtime\.venv\Scripts\python.exe"
    exit /b 0
)
if exist "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" (
    set "PYTHON_EXE=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
    exit /b 0
)
where py >nul 2>&1
if not errorlevel 1 (
    for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable)"') do set "PYTHON_EXE=%%P"
    if defined PYTHON_EXE exit /b 0
)
where python >nul 2>&1
if not errorlevel 1 (
    for /f "delims=" %%P in ('python -c "import sys; print(sys.executable)"') do set "PYTHON_EXE=%%P"
    if defined PYTHON_EXE exit /b 0
)
echo [ERROR] Python 3 was not found.
exit /b 1

:find_openclaw
set "OPENCLAW_CMD="
for /f "usebackq delims=" %%P in (`powershell.exe -NoProfile -Command "$ErrorActionPreference='SilentlyContinue'; $c=Get-Content -LiteralPath '.\config.json' -Raw -Encoding UTF8 | ConvertFrom-Json; if($c.delivery.openclaw_cmd){[Console]::Write($c.delivery.openclaw_cmd)}"`) do set "OPENCLAW_CMD=%%P"
if defined OPENCLAW_CMD if exist "%OPENCLAW_CMD%" exit /b 0
set "OPENCLAW_CMD="
for /f "delims=" %%P in ('where openclaw.cmd 2^>nul') do if not defined OPENCLAW_CMD set "OPENCLAW_CMD=%%P"
if defined OPENCLAW_CMD exit /b 0
echo [ERROR] OpenClaw was not found. Install it with: npm install -g openclaw@latest
echo [ERROR] Then update delivery.openclaw_cmd in config.json.
exit /b 1

:failed
echo.
pause
exit /b 1

:done
endlocal
exit /b 0

