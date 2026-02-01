#!/bin/bash
# Wait for SurrealDB to be healthy
# Usage: ./wait-for-healthy.sh [host] [port] [max_attempts]

set -e

HOST="${1:-localhost}"
PORT="${2:-8000}"
MAX_ATTEMPTS="${3:-30}"
INTERVAL=1

echo "Waiting for SurrealDB at ${HOST}:${PORT}..."

for i in $(seq 1 $MAX_ATTEMPTS); do
    if curl -sf "http://${HOST}:${PORT}/health" > /dev/null 2>&1; then
        echo "SurrealDB is healthy! (attempt $i/$MAX_ATTEMPTS)"
        exit 0
    fi
    echo "Attempt $i/$MAX_ATTEMPTS - SurrealDB not ready yet..."
    sleep $INTERVAL
done

echo "ERROR: SurrealDB failed to become healthy after $MAX_ATTEMPTS attempts"
exit 1
