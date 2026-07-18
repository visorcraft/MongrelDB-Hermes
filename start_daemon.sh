#!/bin/bash
set -e

DATA_DIR="/home/user/.hermes/mongreldb_hermes_data"
PORT=8453
PIDFILE="/tmp/mongreldb-hermes.pid"
LOG="/tmp/mongreldb-hermes.log"
BIN="/path/to/mongreldb-server"

mkdir -p "$DATA_DIR"

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "Daemon already running on pid $(cat "$PIDFILE")"
    exit 0
fi

echo "Starting MongrelDB daemon on port $PORT..."
# Detach from the terminal so the daemon keeps running after the script exits.
setsid "$BIN" "$DATA_DIR" "$PORT" --daemon --pidfile "$PIDFILE" > "$LOG" 2>&1 &

for _ in $(seq 1 50); do
    if curl -s "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
        echo "Daemon ready"
        exit 0
    fi
    sleep 0.1
done

echo "Daemon did not become ready in time"
exit 1
