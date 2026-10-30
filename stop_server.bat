@echo off
setlocal
rem keep PORT in sync with DEFAULT_PORT in scraper.py
set "PORT=8777"
set "FOUND=0"
set "KILLED=0"

rem tokens=5 = PID column on TCP lines (IPv4 and IPv6 alike); UDP lines lack a
rem State column but are excluded by the LISTENING filter
for /f "tokens=5" %%P in ('netstat -ano ^| findstr "LISTENING" ^| findstr /C:":%PORT% "') do (
    if not "%%P"=="0" (
        set "FOUND=1"
        echo Stopping PID %%P ...
        taskkill /F /PID %%P >nul 2>&1
        if not errorlevel 1 set "KILLED=1"
    )
)

if "%KILLED%"=="1" (
    echo Server on port %PORT% stopped.
) else if "%FOUND%"=="1" (
    echo Found a process on port %PORT% but could not stop it - try running this file as administrator.
) else (
    echo No server was listening on port %PORT%.
)
pause
