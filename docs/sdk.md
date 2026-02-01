# Surreal SDK Documentation

A custom Python SDK for SurrealDB with HTTP and WebSocket support.

## Table of Contents

- [Installation](#installation)
- [Quick Start](#quick-start)
- [Connection Types](#connection-types)
  - [HTTP Connection](#http-connection)
  - [WebSocket Connection](#websocket-connection)
  - [Connection Pool](#connection-pool)
- [Authentication](#authentication)
- [CRUD Operations](#crud-operations)
- [Queries](#queries)
- [Live Queries](#live-queries)
- [Change Feeds](#change-feeds)
- [Typed Responses](#typed-responses)
- [Error Handling](#error-handling)

---

## Installation

```bash
# Install the full package (ORM + SDK)
pip install surrealdb-orm

# Or just the SDK (when published separately)
pip install surreal-sdk
```

---

## Quick Start

```python
from surreal_sdk import SurrealDB

async def main():
    # HTTP connection (stateless)
    async with SurrealDB.http("http://localhost:8000", "namespace", "database") as db:
        await db.signin("root", "root")

        # Create a record
        user = await db.create("users", {"name": "Alice", "age": 30})
        print(f"Created: {user.record}")

        # Query records
        result = await db.query("SELECT * FROM users WHERE age > 18")
        print(f"Found: {result.all_records}")
```

---

## Connection Types

### HTTP Connection

Stateless connection ideal for microservices, serverless functions, and REST APIs.

```python
from surreal_sdk import HTTPConnection

# Create connection
conn = HTTPConnection("http://localhost:8000", "namespace", "database")

# Using context manager (recommended)
async with conn:
    await conn.signin("root", "root")
    result = await conn.query("SELECT * FROM users")

# Or manually manage connection
await conn.connect()
await conn.signin("root", "root")
# ... do work ...
await conn.close()
```

**Configuration options:**

```python
conn = HTTPConnection(
    url="http://localhost:8000",
    namespace="ns",
    database="db",
    timeout=30.0,  # Request timeout in seconds
)
```

### WebSocket Connection

Stateful connection for real-time features and Live Queries.

```python
from surreal_sdk import WebSocketConnection

conn = WebSocketConnection(
    url="ws://localhost:8000",  # or wss:// for secure
    namespace="ns",
    database="db",
    auto_reconnect=True,
    reconnect_interval=1.0,
    max_reconnect_attempts=5,
)

async with conn:
    await conn.signin("root", "root")

    # Subscribe to live queries
    async def on_change(data):
        print(f"Change: {data}")

    live_id = await conn.live("users", on_change)
    # ... application runs ...
    await conn.kill(live_id)
```

### Connection Pool

For high-throughput scenarios with connection reuse.

```python
from surreal_sdk import SurrealDB

async with SurrealDB.pool("http://localhost:8000", "ns", "db", size=10) as pool:
    await pool.set_credentials("root", "root")

    # Acquire a connection from the pool
    async with pool.acquire() as conn:
        result = await conn.query("SELECT * FROM users")

    # Connection is automatically returned to the pool
```

---

## Authentication

### Root Authentication

```python
await conn.signin("root", "root")
```

### Namespace/Database Scoped

```python
await conn.signin(
    user="admin",
    password="secret",
    namespace="myns",
    database="mydb"
)
```

### User Signup

```python
response = await conn.signup(
    namespace="myns",
    database="mydb",
    access="account",  # Access method defined in SurrealDB
    email="user@example.com",
    password="secret123"
)
print(f"Token: {response.token}")
```

---

## CRUD Operations

### Create

```python
# Create with auto-generated ID
response = await conn.create("users", {"name": "Alice", "age": 30})
print(f"Created: {response.record}")
print(f"ID: {response.id}")

# Create with specific ID
response = await conn.create("users:alice", {"name": "Alice", "age": 30})
```

### Select

```python
# Select all from table
response = await conn.select("users")
for record in response.records:
    print(record)

# Select specific record
response = await conn.select("users:alice")
print(response.first)
```

### Update (Replace)

```python
# Replace all fields
response = await conn.update("users:alice", {"name": "Alice", "age": 31})
```

### Merge (Partial Update)

```python
# Update only specified fields
response = await conn.merge("users:alice", {"age": 32})
```

### Delete

```python
# Delete specific record
response = await conn.delete("users:alice")
print(f"Deleted: {response.success}")
print(f"Count: {response.count}")

# Delete all from table
response = await conn.delete("users")
```

### Insert (Bulk)

```python
# Insert multiple records
response = await conn.insert("users", [
    {"name": "Bob", "age": 25},
    {"name": "Charlie", "age": 35},
])
print(f"Inserted: {response.count} records")
```

---

## Queries

### Basic Query

```python
response = await conn.query("SELECT * FROM users")
print(response.all_records)
```

### Parameterized Query

```python
response = await conn.query(
    "SELECT * FROM users WHERE age > $min_age AND status = $status",
    {"min_age": 18, "status": "active"}
)
```

### Multiple Statements

```python
response = await conn.query("""
    LET $user = (SELECT * FROM users:alice);
    RETURN $user.name;
""")
# Access individual results
for result in response.results:
    print(result.result)
```

---

## Live Queries

Live Queries provide real-time updates when data changes (WebSocket only).

```python
from surreal_sdk import WebSocketConnection

async with WebSocketConnection("ws://localhost:8000", "ns", "db") as conn:
    await conn.signin("root", "root")

    # Define callback for changes
    async def handle_change(data):
        action = data.get("action")  # CREATE, UPDATE, DELETE
        record = data.get("result")
        print(f"{action}: {record}")

    # Subscribe to table
    live_id = await conn.live("users", handle_change)

    # Keep connection alive
    # Changes to 'users' table will trigger handle_change

    # Unsubscribe when done
    await conn.kill(live_id)
```

### Live Query Manager

For managing multiple subscriptions:

```python
from surreal_sdk import LiveQuery

live = LiveQuery(conn, "users")
await live.subscribe(handle_change)
# ...
await live.unsubscribe()
```

---

## Change Feeds

Change Feeds provide a stateless alternative to Live Queries for CDC patterns (HTTP).

```python
from surreal_sdk import ChangeFeedStream

stream = ChangeFeedStream(conn, "users")

# Stream changes
async for change in stream.stream():
    print(f"Change: {change}")
```

### Multi-Table Change Feed

```python
from surreal_sdk import MultiTableChangeFeed

feed = MultiTableChangeFeed(conn, ["users", "orders", "products"])
async for table, changes in feed.stream():
    print(f"Table {table}: {changes}")
```

---

## Typed Responses

The SDK provides strongly-typed response classes:

### QueryResponse

```python
response = await conn.query("SELECT * FROM users")

# Properties
response.is_ok           # bool - All statements succeeded
response.results         # list[QueryResult] - Individual statement results
response.all_records     # list[dict] - All records from all statements
response.first_result    # QueryResult | None - First statement result
```

### RecordResponse

```python
response = await conn.create("users", {"name": "Alice"})

# Properties
response.record   # dict | None - The record
response.exists   # bool - Record exists
response.id       # str | None - Record ID
response.raw      # Any - Raw response
```

### RecordsResponse

```python
response = await conn.select("users")

# Properties
response.records   # list[dict] - All records
response.count     # int - Number of records
response.is_empty  # bool - No records
response.first     # dict | None - First record

# Iteration
for record in response:
    print(record)
```

### DeleteResponse

```python
response = await conn.delete("users:alice")

# Properties
response.deleted  # list[dict] - Deleted records
response.count    # int - Number deleted
response.success  # bool - Any records deleted
```

---

## Error Handling

```python
from surreal_sdk.exceptions import (
    SurrealDBError,      # Base exception
    ConnectionError,     # Connection failed
    AuthenticationError, # Auth failed
    QueryError,          # Query execution failed
    TimeoutError,        # Request timed out
)

try:
    await conn.signin("wrong", "credentials")
except AuthenticationError as e:
    print(f"Auth failed: {e}")

try:
    await conn.query("INVALID SQL")
except QueryError as e:
    print(f"Query error: {e.message}")
    print(f"Error code: {e.code}")
```

---

## URL Formats

The SDK automatically handles URL conversion:

| Input               | HTTP Connection         | WebSocket Connection  |
| ------------------- | ----------------------- | --------------------- |
| `http://host:8000`  | `http://host:8000/sql`  | `ws://host:8000/rpc`  |
| `https://host:8000` | `https://host:8000/sql` | `wss://host:8000/rpc` |
| `ws://host:8000`    | `http://host:8000/sql`  | `ws://host:8000/rpc`  |
| `wss://host:8000`   | `https://host:8000/sql` | `wss://host:8000/rpc` |
