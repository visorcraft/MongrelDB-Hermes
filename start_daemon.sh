#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
python3 "$SCRIPT_DIR/install_mongreldb.py"

HERMES_DIR="${HERMES_HOME:-$HOME/.hermes}"
DATA_DIR="$HERMES_DIR/mongreldb_hermes_data"
PORT=8453
PIDFILE="/tmp/mongreldb-hermes.pid"
LOG="/tmp/mongreldb-hermes.log"
VERSION="$(PYTHONPATH="$SCRIPT_DIR" python3 -c 'from install_mongreldb import VERSION; print(VERSION)')"
BIN="$SCRIPT_DIR/vendor/$VERSION/mongreldb-server"
ENCRYPTION_ARGS=()
if [ "${MONGRELDB_ENCRYPTION:-enabled}" != "disabled" ]; then
    PASSPHRASE="${MONGRELDB_PASSPHRASE:-$(PYTHONPATH="$SCRIPT_DIR" python3 -c 'from install_mongreldb import load_or_create_passphrase; print(load_or_create_passphrase())')}"
    ENCRYPTION_ARGS=(--passphrase "$PASSPHRASE")
fi

mkdir -p "$DATA_DIR"

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "Daemon already running on pid $(cat "$PIDFILE")"
    exit 0
fi

echo "Starting MongrelDB daemon on port $PORT..."
# Detach from the terminal so the daemon keeps running after the script exits.
setsid "$BIN" "$DATA_DIR" --port "$PORT" "${ENCRYPTION_ARGS[@]}" --daemon --pidfile "$PIDFILE" > "$LOG" 2>&1 &
unset PASSPHRASE

for _ in $(seq 1 50); do
    if curl -s "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
        echo "Daemon ready"
        exit 0
    fi
    sleep 0.1
done

echo "Daemon did not become ready in time"
exit 1
