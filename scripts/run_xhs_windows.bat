@echo off
setlocal
cd /d "%~dp0\.."
if "%~1"=="" (
  echo Usage: scripts\run_xhs_windows.bat "https://xhslink.cn/o/XXXX"
  exit /b 2
)
python tools\xhs_collector.py "%~1" --pause-login
pause
