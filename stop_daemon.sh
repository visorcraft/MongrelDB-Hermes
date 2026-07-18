#!/bin/bash
PIDFILE="/tmp/mongreldb-hermes.pid"
if [ -f "$PIDFILE" ]; then
    PID=$(cat "$PIDFILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "Stopping MongrelDB daemon (pid $PID)..."
        kill "$PID"
    fi
    rm -f "$PIDFILE"
fi
