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
- [Transactions](#transactions)
  - [HTTP Transactions](#http-transactions)
  - [WebSocket Transactions](#websocket-transactions)
  - [Transaction Methods](#transaction-methods)
- [Typed Functions API](#typed-functions-api)
  - [Built-in Functions](#built-in-functions)
  - [Custom Functions](#custom-functions)
  - [Available Namespaces](#available-namespaces)
- [Live Queries](#live-queries)
  - [Callback-Based Live Queries](#callback-based-live-queries)
  - [Live Select Stream (v0.5.0)](#live-select-stream-v050)
  - [Live Select Manager](#live-select-manager)
  - [Auto-Resubscribe](#auto-resubscribe)
- [Typed Function Calls (v0.5.0)](#typed-function-calls-v050)
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

        # Use transactions
        async with db.transaction() as tx:
            await tx.create("orders:1", {"total": 100})
            await tx.create("payments:1", {"amount": 100})

        # Call built-in functions
        sqrt = await db.fn.math.sqrt(16)
        print(f"Square root: {sqrt}")
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

**Protocol:** WebSocket connections use **CBOR** (binary) protocol by default (v0.5.5+).
CBOR properly handles strings like `data:image/png;base64,...` that JSON would incorrectly interpret as record links.

```python
from surreal_sdk import WebSocketConnection

conn = WebSocketConnection(
    url="ws://localhost:8000",  # or wss:// for secure
    namespace="ns",
    database="db",
    auto_reconnect=True,
    reconnect_interval=1.0,
    max_reconnect_attempts=5,
    protocol="cbor",  # Default. Use "json" only for debugging.
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

## Transactions

Transactions allow multiple operations to be executed atomically - either all succeed or all fail.

### HTTP Transactions

HTTP transactions batch statements and execute them atomically at commit time.

```python
async with conn.transaction() as tx:
    # Statements are queued
    await tx.create("orders:1", {"total": 100, "status": "pending"})
    await tx.create("payments:1", {"order_id": "orders:1", "amount": 100})
    await tx.update("inventory:item1", {"stock": 99})
    # All statements executed atomically on context exit
```

**How it works:**
1. Statements are collected in memory
2. On context exit (or explicit `commit()`), all statements are wrapped in `BEGIN TRANSACTION; ... COMMIT TRANSACTION;`
3. If an exception occurs, no statements are sent (rollback is automatic)

### WebSocket Transactions

WebSocket transactions execute statements immediately within a server-side transaction context.

```python
async with ws_conn.transaction() as tx:
    # BEGIN TRANSACTION sent immediately
    await tx.create("orders:1", {"total": 100})  # Executes immediately
    await tx.update("users:alice", {"orders": "+=1"})  # Executes immediately
    # COMMIT TRANSACTION sent on context exit
```

**How it works:**
1. `BEGIN TRANSACTION` is sent when entering the context
2. Each statement executes immediately on the server
3. `COMMIT TRANSACTION` is sent on success, `CANCEL TRANSACTION` on exception

### Transaction Methods

```python
async with conn.transaction() as tx:
    # Raw SurrealQL query
    await tx.query("UPDATE users SET active = true WHERE id = $id", {"id": "users:1"})

    # Create record
    await tx.create("users", {"name": "Alice", "age": 30})
    await tx.create("users:bob", {"name": "Bob", "age": 25})

    # Update record (replace all fields)
    await tx.update("users:alice", {"name": "Alice", "age": 31, "verified": True})

    # Delete record
    await tx.delete("users:old_user")

    # Create graph relation
    await tx.relate("users:alice", "follows", "users:bob")

    # Explicit commit (usually automatic)
    await tx.commit()

    # Or explicit rollback
    await tx.rollback()
```

### Transaction Properties

```python
tx.is_active       # True if transaction is in progress
tx.is_committed    # True if transaction was committed
tx.is_rolled_back  # True if transaction was rolled back
```

### Error Handling in Transactions

```python
from surreal_sdk.exceptions import TransactionError

try:
    async with conn.transaction() as tx:
        await tx.create("orders:1", {"total": 100})
        raise ValueError("Something went wrong")  # Triggers rollback
except ValueError:
    print("Transaction was rolled back")
    print(f"Rolled back: {tx.is_rolled_back}")

# Handle transaction-specific errors
try:
    async with conn.transaction() as tx:
        await tx.query("INVALID SQL")
except TransactionError as e:
    print(f"Transaction error: {e}")
    print(f"Error code: {e.code}")
    print(f"Rollback succeeded: {e.rollback_succeeded}")
```

---

## Typed Functions API

The SDK provides a fluent API for calling SurrealDB functions with full type safety.

### Built-in Functions

Call SurrealDB built-in functions using the `fn` property:

```python
# Math functions
sqrt = await db.fn.math.sqrt(16)           # 4.0
power = await db.fn.math.pow(2, 8)         # 256.0
abs_val = await db.fn.math.abs(-42)        # 42

# String functions
length = await db.fn.string.len("hello")   # 5
lower = await db.fn.string.lowercase("HELLO")  # "hello"
upper = await db.fn.string.uppercase("hello")  # "HELLO"

# Time functions
now = await db.fn.time.now()               # Current datetime
today = await db.fn.time.floor(now, "1d")  # Start of day

# Array functions
arr_len = await db.fn.array.len([1, 2, 3])     # 3
combined = await db.fn.array.concat([1, 2], [3, 4])  # [1, 2, 3, 4]

# Crypto functions
sha = await db.fn.crypto.sha256("data")    # SHA256 hash (64 hex chars)
md5 = await db.fn.crypto.md5("data")       # MD5 hash
```

### Custom Functions

Call user-defined functions (created with `DEFINE FUNCTION`):

```python
# Define a custom function in SurrealDB:
# DEFINE FUNCTION fn::calculate_total($items: array<object>) {
#     RETURN math::sum($items.*.price);
# };

# Call it from Python:
total = await db.fn.calculate_total([
    {"name": "Item 1", "price": 10},
    {"name": "Item 2", "price": 20},
])
# Returns: 30

# Multi-argument custom functions
result = await db.fn.process_order(user_id, order_data, options)
```

### Available Namespaces

The SDK supports all SurrealDB built-in function namespaces:

| Namespace | Description | Examples |
|-----------|-------------|----------|
| `array` | Array operations | `len`, `concat`, `distinct`, `flatten` |
| `crypto` | Cryptographic functions | `sha256`, `sha512`, `md5`, `argon2` |
| `duration` | Duration operations | `days`, `hours`, `mins`, `secs` |
| `geo` | Geospatial functions | `distance`, `area`, `bearing` |
| `http` | HTTP requests | `get`, `post`, `put`, `delete` |
| `math` | Mathematical functions | `sqrt`, `pow`, `abs`, `round`, `floor` |
| `meta` | Metadata functions | `id`, `table`, `tb` |
| `object` | Object operations | `keys`, `values`, `entries` |
| `parse` | Parsing functions | `email`, `url`, `domain` |
| `rand` | Random generation | `int`, `float`, `string`, `uuid` |
| `session` | Session information | `db`, `id`, `ip`, `ns` |
| `string` | String operations | `len`, `lowercase`, `uppercase`, `trim` |
| `time` | Time functions | `now`, `floor`, `round`, `format` |
| `type` | Type conversion | `bool`, `int`, `float`, `string` |
| `vector` | Vector operations | `add`, `magnitude`, `normalize` |

### How It Works

```python
# Built-in function: namespace::function
await db.fn.math.sqrt(16)
# Generates: RETURN math::sqrt($fn_arg_0)
# With vars: {"fn_arg_0": 16}

# Custom function: fn::function_name
await db.fn.my_function(arg1, arg2)
# Generates: RETURN fn::my_function($fn_arg_0, $fn_arg_1)
# With vars: {"fn_arg_0": arg1, "fn_arg_1": arg2}
```

---

## Live Queries

Live Queries provide real-time updates when data changes (WebSocket only).

### Callback-Based Live Queries

The traditional callback approach for receiving change notifications:

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

### Live Select Stream (v0.5.0)

The async iterator pattern provides a more Pythonic way to consume live changes:

```python
from surreal_sdk import SurrealDB, LiveAction

async with SurrealDB.ws("ws://localhost:8000", "ns", "db") as db:
    await db.signin("root", "root")

    # Async context manager + async iterator
    async with db.live_select("users") as stream:
        async for change in stream:
            print(f"ID: {change.record_id}")
            print(f"Action: {change.action}")
            print(f"Data: {change.result}")

            match change.action:
                case LiveAction.CREATE:
                    print(f"User created: {change.result['name']}")
                case LiveAction.UPDATE:
                    print(f"User updated: {change.record_id}")
                case LiveAction.DELETE:
                    print(f"User deleted: {change.record_id}")
```

**With WHERE clause and parameters:**

```python
# Subscribe only to players at a specific game table
async with db.live_select(
    "players",
    where="table_id = $table_id AND active = true",
    params={"table_id": "game_tables:xyz"}
) as stream:
    async for change in stream:
        print(f"Player change: {change.result}")
```

**DIFF mode (receive only changed fields):**

```python
async with db.live_select("users", diff=True) as stream:
    async for change in stream:
        # change.changed_fields contains list of modified field names
        print(f"Changed fields: {change.changed_fields}")
```

**LiveChange dataclass properties:**

| Property | Type | Description |
|----------|------|-------------|
| `id` | `str` | Live query UUID |
| `action` | `LiveAction` | CREATE, UPDATE, or DELETE |
| `record_id` | `str` | The affected record ID (e.g., "users:alice") |
| `result` | `dict` | The full record after the change |
| `before` | `dict \| None` | Record before change (DIFF mode) |
| `changed_fields` | `list[str]` | Changed field names (DIFF mode) |

### Live Select Manager

For callback-based multi-stream management:

```python
from surreal_sdk import LiveSelectManager

manager = LiveSelectManager(db)

# Watch multiple tables with callbacks
await manager.watch("players", on_player_change, where="table_id = $id", params={"id": table_id})
await manager.watch("game_tables", on_table_change, where="id = $id", params={"id": table_id})

print(f"Active streams: {manager.count}")

# Stop specific stream
await manager.stop(live_id)

# Stop all streams
await manager.stop_all()
```

### Auto-Resubscribe

Automatically resubscribe to Live Queries after WebSocket reconnection (network issues, K8s pod restarts):

```python
async def on_reconnect(old_id: str, new_id: str):
    """Called when subscription is recreated after reconnect."""
    print(f"Subscription renewed: {old_id} -> {new_id}")

async with db.live_select(
    "players",
    where="table_id = $id",
    params={"id": table_id},
    auto_resubscribe=True,       # Enable auto-reconnect
    on_reconnect=on_reconnect    # Optional callback
) as stream:
    async for change in stream:
        # Stream automatically resumes after reconnection
        process_change(change)
```

**How it works:**

1. When `auto_resubscribe=True`, the connection stores subscription parameters
2. On WebSocket disconnect, the connection attempts to reconnect
3. After reconnect, all stored subscriptions are recreated
4. `on_reconnect` callback is invoked with old and new subscription IDs
5. The async iterator continues seamlessly

---

## Typed Function Calls (v0.5.0)

Call SurrealDB functions with automatic return type conversion to Pydantic models or dataclasses.

### Basic Usage

```python
from pydantic import BaseModel
from dataclasses import dataclass

# Pydantic model
class VoteResult(BaseModel):
    success: bool
    new_count: int
    total_votes: int

# Or dataclass
@dataclass
class VoteResultDC:
    success: bool
    new_count: int
    total_votes: int

# Call with typed return
result = await db.call(
    "cast_vote",
    params={"user_id": "alice", "table_id": "game:123"},
    return_type=VoteResult
)

# result is a VoteResult instance, not a dict
print(result.success)     # True
print(result.new_count)   # 5
print(result.total_votes) # 10
```

### Function Name Normalization

```python
# These are equivalent - fn:: prefix is added automatically
await db.call("cast_vote", params={...})
await db.call("fn::cast_vote", params={...})

# Other namespaces are preserved
await db.call("math::sqrt", params={"value": 16})  # No fn:: prefix added
```

### Defining Functions in SurrealDB

```sql
DEFINE FUNCTION fn::cast_vote($user_id: string, $table_id: string) {
    LET $player = (SELECT * FROM players WHERE user_id = $user_id AND table_id = $table_id LIMIT 1)[0];

    IF $player.has_voted {
        RETURN { success: false, new_count: 0, total_votes: 0 };
    };

    UPDATE players SET has_voted = true WHERE id = $player.id;

    LET $count = (SELECT count() FROM players WHERE table_id = $table_id AND has_voted = true GROUP ALL).count;
    LET $total = (SELECT count() FROM players WHERE table_id = $table_id GROUP ALL).count;

    RETURN { success: true, new_count: $count, total_votes: $total };
};
```

### Simple Type Conversion

```python
# Convert to simple types
count = await db.call("get_count", return_type=int)
name = await db.call("get_name", return_type=str)
price = await db.call("get_price", return_type=float)
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
    SurrealDBError,       # Base exception
    ConnectionError,      # Connection failed
    AuthenticationError,  # Auth failed
    QueryError,           # Query execution failed
    TimeoutError,         # Request timed out
    TransactionError,     # Transaction failed
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

try:
    async with conn.transaction() as tx:
        await tx.query("INVALID")
except TransactionError as e:
    print(f"Transaction error: {e}")
    print(f"Rollback status: {e.rollback_succeeded}")
```

---

## URL Formats

The SDK automatically handles URL conversion:

| Input | HTTP Connection | WebSocket Connection |
|-------|-----------------|----------------------|
| `http://host:8000` | `http://host:8000/sql` | `ws://host:8000/rpc` |
| `https://host:8000` | `https://host:8000/sql` | `wss://host:8000/rpc` |
| `ws://host:8000` | `http://host:8000/sql` | `ws://host:8000/rpc` |
| `wss://host:8000` | `https://host:8000/sql` | `wss://host:8000/rpc` |
