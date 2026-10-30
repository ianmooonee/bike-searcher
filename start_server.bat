@echo off
setlocal
rem keep PORT in sync with DEFAULT_PORT in scraper.py
set "PORT=8777"

rem already running? (LISTENING lines have foreign addr 0.0.0.0:0, so ":PORT "
rem can only be the local address; trailing space avoids matching e.g. :87770)
netstat -ano | findstr "LISTENING" | findstr /C:":%PORT% " >nul
if %errorlevel%==0 (
    rem something listens there - is it actually the report server?
    curl -s -f -o nul --max-time 3 "http://127.0.0.1:%PORT%/status"
    if not errorlevel 1 (
        echo Server already running on port %PORT% - opening the report.
        start "" "http://127.0.0.1:%PORT%/"
    ) else (
        echo Port %PORT% is already in use by another program.
        echo Close it, or change PORT here and DEFAULT_PORT in scraper.py.
        pause
    )
    exit /b 0
)

cd /d "%~dp0"
echo Starting bike-searcher server on port %PORT% (close this window or run stop_server.bat to stop)...
python scraper.py --serve --port %PORT%

echo.
echo Server stopped.
pause
