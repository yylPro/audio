@echo off
setlocal
cd /d "%~dp0"

if exist "C:\Users\%USERNAME%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" (
  "C:\Users\%USERNAME%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" "%~dp0install_yuanbao_native_at_patch.py" %*
  goto :done
)

where py >nul 2>nul
if not errorlevel 1 (
  py -3 "%~dp0install_yuanbao_native_at_patch.py" %*
  goto :done
)

where python >nul 2>nul
if not errorlevel 1 (
  python "%~dp0install_yuanbao_native_at_patch.py" %*
  goto :done
)

echo Python 3 was not found. Run install_yuanbao_native_at_patch.ps1 instead.
exit /b 1

:done
if errorlevel 1 exit /b %errorlevel%
echo.
echo Patch operation completed.
endlocal
