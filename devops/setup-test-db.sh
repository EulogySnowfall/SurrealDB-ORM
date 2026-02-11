#!/bin/bash
# Setup test database with initial schema and data
# Usage: ./setup-test-db.sh [host] [port]

set -e

HOST="${1:-localhost}"
PORT="${2:-8000}"
URL="http://${HOST}:${PORT}"

echo "Setting up test database at ${URL}..."

# Wait for SurrealDB to be ready
./devops/wait-for-healthy.sh "$HOST" "$PORT"

# Create test namespace and database
curl -sf -X POST "${URL}/sql" \
    -H "Accept: application/json" \
    -H "Content-Type: text/plain" \
    -H "Surreal-NS: test" \
    -H "Surreal-DB: test" \
    -u "root:root" \
    --data-raw "
        -- Setup test schema
        DEFINE NAMESPACE IF NOT EXISTS test;
        USE NS test;
        DEFINE DATABASE IF NOT EXISTS test;
        USE DB test;

        -- Define tables for testing
        DEFINE TABLE IF NOT EXISTS users SCHEMAFULL;
        DEFINE FIELD name ON users TYPE string;
        DEFINE FIELD email ON users TYPE string;
        DEFINE FIELD age ON users TYPE int;

        -- Enable change feeds for streaming tests
        DEFINE TABLE IF NOT EXISTS orders CHANGEFEED 1h;
        DEFINE FIELD product ON orders TYPE string;
        DEFINE FIELD quantity ON orders TYPE int;
        DEFINE FIELD status ON orders TYPE string;
    "

echo "Test database setup complete!"
