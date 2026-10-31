#!/usr/bin/env bash
# keep PORT in sync with DEFAULT_PORT in scraper.py
PORT=8777
URL="http://127.0.0.1:$PORT/"

cd "$(dirname "$(readlink -f "$0")")" || exit 1

# under WSL, open the report in the Windows browser (python's webbrowser
# module honours $BROWSER; without it, it tries gio and fails). rundll32 rather
# than explorer.exe, which always exits 1 and so makes python fall back to gio
if [ -z "$BROWSER" ] && grep -qi microsoft /proc/version 2>/dev/null \
        && command -v rundll32.exe >/dev/null 2>&1; then
    export BROWSER="rundll32.exe url.dll,FileProtocolHandler %s"
fi

open_url() {
    if [ -n "$BROWSER" ]; then
        ${BROWSER//%s/$1} >/dev/null 2>&1 &
        return
    fi
    for opener in xdg-open wslview; do
        if command -v "$opener" >/dev/null 2>&1; then
            "$opener" "$1" >/dev/null 2>&1 &
            return
        fi
    done
    echo "Open $1 in your browser."
}

# already running? (sport matches the local port of LISTEN sockets only)
if [ -n "$(ss -Hltn "sport = :$PORT" 2>/dev/null)" ]; then
    # something listens there - is it actually the report server?
    if curl -s -f -o /dev/null --max-time 3 "http://127.0.0.1:$PORT/status"; then
        echo "Server already running on port $PORT - opening the report."
        open_url "$URL"
        exit 0
    fi
    echo "Port $PORT is already in use by another program."
    echo "Close it, or change PORT here and DEFAULT_PORT in scraper.py."
    exit 1
fi

# first run: create a virtualenv with the dependencies and playwright's browser
# (Ubuntu doesn't allow pip installs into the system python)
if [ ! -x .venv/bin/python ]; then
    echo "First run - setting up .venv ..."
    if ! python3 -m venv .venv; then
        rm -rf .venv
        echo "Could not create the virtualenv - install it with: sudo apt install python3-venv"
        exit 1
    fi
    if ! .venv/bin/pip install -r requirements.txt || ! .venv/bin/python -m playwright install chromium; then
        rm -rf .venv
        echo "Dependency install failed - see the errors above."
        exit 1
    fi
    echo "If the browser fails to launch, install its system libraries with:"
    echo "  sudo .venv/bin/python -m playwright install-deps chromium"
fi
PYTHON=.venv/bin/python

echo "Starting bike-searcher server on port $PORT (Ctrl+C or ./stop_server.sh to stop)..."
"$PYTHON" scraper.py --serve --port "$PORT"

echo
echo "Server stopped."
