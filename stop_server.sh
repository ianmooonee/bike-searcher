#!/usr/bin/env bash
# keep PORT in sync with DEFAULT_PORT in scraper.py
PORT=8777

# PIDs of processes with a listening TCP socket on PORT (IPv4 and IPv6)
PIDS=$(lsof -t -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | sort -u)

if [ -z "$PIDS" ]; then
    # lsof only sees our own processes without sudo; ss still shows the socket
    if [ -n "$(ss -Hltn "sport = :$PORT" 2>/dev/null)" ]; then
        echo "Found a process on port $PORT but could not stop it - try: sudo $0"
        exit 1
    fi
    echo "No server was listening on port $PORT."
    exit 0
fi

KILLED=0
for pid in $PIDS; do
    echo "Stopping PID $pid ..."
    kill "$pid" 2>/dev/null && KILLED=1
done

# give it a moment to exit cleanly, then force it
for _ in 1 2 3 4 5; do
    lsof -t -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 || break
    sleep 1
done
for pid in $(lsof -t -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null); do
    kill -9 "$pid" 2>/dev/null
done

if [ "$KILLED" = 1 ]; then
    echo "Server on port $PORT stopped."
else
    echo "Found a process on port $PORT but could not stop it - try: sudo $0"
    exit 1
fi
