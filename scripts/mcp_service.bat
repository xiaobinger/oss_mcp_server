@echo off
setlocal
set "SCRIPT_DIR=%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%mcp_service.ps1" %*
set "EXIT_CODE=%ERRORLEVEL%"

rem Keep the window open when double-clicked (no arguments => default start).
if "%~1"=="" pause

exit /b %EXIT_CODE%
