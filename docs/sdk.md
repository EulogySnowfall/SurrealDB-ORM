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
- [Data Import/Export](#data-importexport)
  - [JSON Lines Format](#json-lines-format)
  - [Reading and Exporting a Table](#reading-and-exporting-a-table)
  - [Importing JSON Lines](#importing-json-lines)
  - [Database Dump and Restore](#database-dump-and-restore)
- [Durations](#durations)
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

**Message size.** `max_message_size` is the largest message the client accepts, in bytes. The default, `None`,
sets no client-side cap, like the HTTP transport: a large `SELECT` reply arrives whatever its size (the server
still limits the size of a *request*). Pass an `int` (at least 1) to cap replies; a reply above it then fails at
once instead of being read.

```python
conn = WebSocketConnection("ws://localhost:8000", "ns", "db", max_message_size=16 * 1024 * 1024)
# or: SurrealDB.ws("ws://localhost:8000", "ns", "db", max_message_size=16 * 1024 * 1024)
```

**Lost connection.** When the socket drops for any reason other than `close()` (a reply above
`max_message_size`, a close from the server, a network error), every request still waiting for its reply raises
`ConnectionError("WebSocket connection lost: <reason>")` at once, before the automatic reconnect starts. A reply
to those requests can never arrive on the new socket, so they no longer wait for their full `timeout`.

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

### Record references

A `table:id` string is read the way SurrealQL reads it inline, by the connection methods below and by both transaction
types alike, so one string names one record on every path (CBOR protocol; under `protocol="json"` the string is sent
as it is):

| Reference | Record ID |
| --- | --- |
| `"users:alice"` | the string `"alice"` |
| `"users:1"`, `"users:-5"` | the integers `1` and `-5` (signed 64-bit range) |
| `"users:05"`, `"users:-05"` | the integers `5` and `-5`: leading zeros are dropped, as SurrealQL drops them |
| `"users:9223372036854775808"` | the string `"9223372036854775808"`: beyond the 64-bit range the digits are kept as written |
| ``"users:`1`"``, `"users:⟨1⟩"` | the string `"1"` (read back once; `format_thing()` writes the backtick form) |
| `"users"` | the whole table |

A malformed reference (unclosed delimiter, empty ID, array or object ID) raises `ValueError` before any request.
`HTTPTransaction` writes the table into its statement, so there (and in `WebSocketTransaction`, which checks the same
way) the table must be an identifier. The connection methods send the table as data in the `RecordId`: any non-empty
name is accepted (`"user-profiles:abc"`), and a delimited one (``"`user-profiles`:abc"``, `"⟨user-profiles⟩:abc"`) is
read back once. `rand()`, `ulid()` and `uuid()` are SurrealQL, not record IDs: `HTTPTransaction` writes them into its
statement, while the connection methods (and so `WebSocketTransaction`) refuse them with `ValueError`; pass the table
name for a random ID. A `RecordId` value is passed through unchanged.

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

## Data Import/Export

Read a table in batches, export it to [JSON Lines](https://jsonlines.org/) (one JSON object per line) and import
such a file into a table. The three methods are on every connection (HTTP and WebSocket) and need the **CBOR**
protocol (the default): under JSON, record ids, datetimes and decimals arrive as strings and would not round-trip,
so they raise `ValueError` on a `protocol="json"` connection.

The database must exist. A query on an undefined namespace or database fails, and a raw SDK connection does not
create it (the ORM's connections do, on connect):

```python
await conn.query("DEFINE DATABASE IF NOT EXISTS app;")
```

### JSON Lines Format

The codec lives in `surreal_sdk.jsonl` (`encode_value()`, `decode_value()`, `dumps_record()`, `loads_record()`,
`JSONL_TAGS`). By default (`typed=True`) a value that JSON cannot carry is written as an object with **exactly one
key**, its tag, and read back as the same Python value:

| Python value | JSON line | Read back as |
| --- | --- | --- |
| `None`, `bool`, `str`, `int`, finite `float` | native | same (`None` is sent as `NONE`) |
| `nan`, `inf`, `-inf` | `{"$float": "NaN"}`, `{"$float": "Infinity"}`, `{"$float": "-Infinity"}` | `float` |
| `Decimal` | `{"$decimal": "1.10"}` | `Decimal` |
| `datetime` | `{"$datetime": "2020-01-02T03:04:05.123456Z"}` (UTC, 6 fraction digits; naive read as UTC) | aware UTC `datetime` |
| `date` | as the UTC-midnight `datetime` | `datetime` |
| `UUID` | `{"$uuid": "0190f3b2-..."}` | `UUID` |
| `Duration` | `{"$duration": "1h30m"}`, or `{"$duration": [5400, 1]}` for the compact form | `Duration` |
| `bytes` | `{"$bytes": "<base64>"}` | `bytes` |
| `RecordId` | `{"$record": ["person", "alice"]}` (an array or object key as JSON) | `RecordId` |
| `Table` | `{"$table": "person"}` | `Table` |
| `dict`, `list` | object, array | `dict`, `list` |

A user object with a single key starting with `$` is wrapped as `{"$object": {...}}`, so it is never read as a
tag. On import, a `$` key that is not a known tag, a malformed tag payload (an invalid duration, bad base64, ...) or
a line that is not a JSON object raises `DataFormatError` naming the line number, before that line's batch is
sent.

`typed=False` (export only) writes plain JSON for external tools such as pandas: `"person:alice"`, ISO strings,
`"1.10"`, `"NaN"`. Such a file loads back as plain JSON: its values stay strings.

**Not supported** — use a database dump for a lossless copy:

- geometry values, ranges and datetimes outside Python's range (the SDK decodes them as tuples): the export raises
  `DataFormatError` naming the field and the record;
- nanosecond digits of a datetime (Python keeps microseconds);
- `NULL`: it is read as `None` and written back as `NONE`.

### Reading and Exporting a Table

```python
from surreal_sdk import SurrealDB

async with SurrealDB.http("http://localhost:8000", "ns", "db") as conn:
    await conn.signin("root", "root")

    # Batches of decoded records (plain dicts), in id order
    async for batch in conn.iter_records("person", batch_size=500):
        for record in batch:
            print(record["id"], record["name"])

    # Filter with a raw SurrealQL condition; every value goes in vars (bound)
    n = await conn.export_jsonl(
        "person",
        "adults.jsonl",                 # a path (new file: mode 0o600, UTF-8) or a text stream
        where="age >= $min_age",
        vars={"min_age": 18},
        limit=10_000,
    )
    print(f"{n} records written")
```

- The table is read by **keyset on `id`** (`... WHERE id > $_x_after ORDER BY id LIMIT n`), one statement per
  batch, so only one batch is held in memory. Lower `batch_size` for wide rows. The server does not seek to the
  cursor: each batch scans the table from its start, so a whole-table export slows down as the table grows (on
  3.3, about 6 ms per 1000-row batch at the start, about 200 ms at row 300,000). For a full copy of a large table,
  use a dump (`export_database()` / `surreal-orm dump`). Over live data, rows written during
  the export may or may not be included.
- `where` is written as `({where})` in the statement: it is SurrealQL, never user input. Put values in `vars`.
  Variable names starting with `_x_` are reserved.
- A **new** file is created with mode `0o600` (readable by its owner only), as for a dump: an export holds the
  table's data as stored. An existing file is truncated and keeps its mode.
- File writes (the open and the close included) run in a worker thread, once per batch. On an error, the lines
  already written stay in the file; nothing is retried.

### Importing JSON Lines

```python
from surreal_sdk import DataImportError, ImportReport

report: ImportReport = await conn.import_jsonl(
    "person",
    "adults.jsonl",          # a path, a text stream, or an (async) iterable of lines
    batch_size=1000,
    max_batch_bytes=1_048_576,
    on_conflict="ignore",    # "error" (default) or "ignore" (INSERT IGNORE: existing ids are skipped)
)
print(report.lines, report.rows, report.batches)
```

- Lines are sent in batches of at most `batch_size` rows and `max_batch_bytes` bytes (1 MiB by default, under the
  server's 4 MiB limit on an RPC request). A single line larger than `max_batch_bytes` is sent alone.
- Each batch is **one `INSERT` statement, so it is atomic**; the file is not. When a batch fails,
  `DataImportError` is raised with `.report` (what the batches before it wrote) and `.errors`. A rejected
  statement (a duplicate id, a schema or `ASSERT` violation) is reported with the server's message; a request that
  fails (a dropped connection, a timeout, an HTTP error) keeps the original exception as `__cause__`, and whether
  that batch reached the database is then unknown. `report.lines` counts the non-blank lines decoded: equal to
  `report.rows` after a successful import, larger when an import fails partway (the failed batch's lines included).
- Edge tables: `relation=None` (default) reads the table's type once (`TYPE RELATION` → `INSERT RELATION`,
  `TYPE NORMAL` → `INSERT`); on a `TYPE ANY` or undefined table a batch is a relation insert only when every row
  has record `in` and `out` fields. Pass `relation=True` or `False` to decide yourself.
- `raw=True` prefixes each batch with `OPTION IMPORT;`, for that call only: no type checks, no `VALUE`, `ASSERT` or
  `DEFAULT`, no events. It needs a system user (root, namespace or database), as the server refuses it to a record
  user.
- Files and text streams are read in a worker thread, never on the event loop.

### Database Dump and Restore

`HTTPConnection` dumps a whole database through the server's `/export` endpoint and restores one through
`/import`, in the server's own SurrealQL format (the one `surreal export` / `surreal import` use): `OPTION IMPORT;`,
the definitions, then the rows as `INSERT` statements. Unlike JSON Lines, a dump keeps every value as the server
stores it (geometry, ranges, nanosecond datetimes, `NULL`). These methods are **HTTP only** (there is no such
endpoint over WebSocket); pooled connections are `HTTPConnection`s and have them too.

```python
async with SurrealDB.http("http://localhost:8000", "app", "prod") as conn:
    await conn.signin("root", "root")

    n = await conn.export_database("prod.surql")                     # whole database, returns bytes written
    await conn.export_database("posts.surql", tables=["post"], users=False, accesses=False)
    await conn.export_database("schema.surql", records=False)        # definitions only

    async with conn.export_database_stream(tables=["post"]) as chunks:   # stream to any sink
        async for chunk in chunks:
            await sink.write(chunk)

async with SurrealDB.http("http://localhost:8000", "app", "copy") as target:
    await target.signin("root", "root")
    await target.query("DEFINE DATABASE IF NOT EXISTS copy;")       # the database must exist
    await target.import_database("prod.surql")   # a path, bytes, a binary stream or an async iterable of bytes
```

> **A dump contains user password hashes and access definitions (including JWT keys)** unless users and accesses
> are excluded: `export_database(path, users=False, accesses=False)`, or
> `surreal-orm dump --no-users --no-accesses -o app.surql`. Every section is included by default, as the server
> does. A **new** dump file is created with mode `0o600` (readable by its owner only); an existing file is
> truncated and keeps its mode. JSON Lines exports keep the process umask. A `VIEWER` user can dump: that is the
> server's permission.

Options of `export_database()` / `export_database_stream()`: `tables` (names; `None` = every table), `users`,
`accesses`, `params`, `functions`, `analyzers`, `records` (`False` = definitions only), all `True` by default.
Errors: a non-200 answer raises `QueryError` (`Export failed: HTTP {code} - ...`, `code` set); a transfer failure
raises `ConnectionError` and leaves the bytes already written in the file.

**What a restore does** (measured on SurrealDB 3.2 and 3.3):

- It runs under the dump's `OPTION IMPORT;`: no field processing, no `ASSERT`, `VALUE` or events. The SDK never
  adds `OPTION IMPORT;` itself, and a body without it is refused (HTTP 400). It needs a system user (root,
  namespace or database `OWNER`); others get HTTP 403.
- A definition of the same name (table, field, index, event, function, param) is **replaced** by the dump's; a
  definition that is only in the target **stays**.
- It is **not transactional**: a row that already exists makes its whole `INSERT` statement fail, and the other
  statements, other tables included, are applied. Restore into a database whose dumped tables hold no rows.
- **The database must exist**: otherwise the server reports `The database '<db>' does not exist`, even after
  the dump's statements created it and wrote rows. `import_database()` runs no DDL; create the database first
  (the ORM's connections create it on connect).
- Errors raise `DataImportError`: `Import failed: {k} statement(s) rejected; first: ...` with every message in
  `.errors` and the HTTP status in `.code` (422 on 3.3, 200 on 3.2); `Import failed: HTTP {code} - ...` for another
  status; and, with `.code=None`, when the connection closes during the upload, which is how the server answers a
  large body it rejects up front.
- Both transfers have no client timeout and count as in flight, so `reconnect()` waits for them (up to its drain
  timeout). There is no transient-`401` re-mint: a rejected token is raised with `code=401`. The server accepts an
  import body up to 4 GiB.

---

## Durations

`Duration` (from `surreal_sdk`) holds a SurrealDB duration in one of two forms, and both can be bound to a query:

```python
from surreal_sdk import Duration

await conn.query("UPDATE session:1 SET ttl = $ttl;", {"ttl": Duration("1h30m")})      # a duration literal
await conn.query("UPDATE session:1 SET ttl = $ttl;", {"ttl": Duration((5400, 1))})     # (seconds, nanoseconds)

result = await conn.query("RETURN 1h30m;")
d = result.first_result.result              # Duration((5400,)): the server sends the compact form
str(d)                                      # "5400s", a literal the server reads back as 1h30m
```

- A **string** value (`"1h30m"`, `"2d"`) is sent as a duration literal; the server checks it, so an invalid string
  fails the request.
- A **tuple** value is the compact form the server sends back: `(seconds,)`, `(seconds, nanoseconds)` or `()` for
  zero. `str()` gives a literal for it: `"5400s"`, `"5ns"`, `"1s1ns"`, `"0ns"`.

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

| Namespace  | Description             | Examples                                |
| ---------- | ----------------------- | --------------------------------------- |
| `array`    | Array operations        | `len`, `concat`, `distinct`, `flatten`  |
| `crypto`   | Cryptographic functions | `sha256`, `sha512`, `md5`, `argon2`     |
| `duration` | Duration operations     | `days`, `hours`, `mins`, `secs`         |
| `geo`      | Geospatial functions    | `distance`, `area`, `bearing`           |
| `http`     | HTTP requests           | `get`, `post`, `put`, `delete`          |
| `math`     | Mathematical functions  | `sqrt`, `pow`, `abs`, `round`, `floor`  |
| `meta`     | Metadata functions      | `id`, `table`, `tb`                     |
| `object`   | Object operations       | `keys`, `values`, `entries`             |
| `parse`    | Parsing functions       | `email`, `url`, `domain`                |
| `rand`     | Random generation       | `int`, `float`, `string`, `uuid`        |
| `session`  | Session information     | `db`, `id`, `ip`, `ns`                  |
| `string`   | String operations       | `len`, `lowercase`, `uppercase`, `trim` |
| `time`     | Time functions          | `now`, `floor`, `round`, `format`       |
| `type`     | Type conversion         | `bool`, `int`, `float`, `string`        |
| `vector`   | Vector operations       | `add`, `magnitude`, `normalize`         |

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

The subscription survives a reconnect: once the session is restored, the
connection re-creates the live query and `handle_change` keeps receiving its
notifications, now under a new id (`conn.live_queries` lists the current ids).
`conn.kill()` accepts the id `live()` returned or the current one, and a killed
query is never re-created. `LiveQuery.subscribe()` behaves the same way; its
`live_id` follows the re-created query, and `unsubscribe()` ends it.

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

| Property         | Type           | Description                                  |
| ---------------- | -------------- | -------------------------------------------- |
| `id`             | `str`          | Live query UUID                              |
| `action`         | `LiveAction`   | CREATE, UPDATE, or DELETE                    |
| `record_id`      | `str`          | The affected record ID (e.g., "users:alice") |
| `result`         | `dict`         | The full record after the change             |
| `before`         | `dict \| None` | Record before change (DIFF mode)             |
| `changed_fields` | `list[str]`    | Changed field names (DIFF mode)              |

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

After a reconnect each stream follows its re-created live query:
`active_streams` lists the current ids, and `stop()` accepts either the id
`watch()` returned or the current one (the `new_id` passed to `on_reconnect`).
`LiveQueryManager.unwatch()` and `active_queries` work the same way.

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
   (`conn.live()` and `LiveQuery.subscribe()` always do)
2. On WebSocket disconnect, the connection attempts to reconnect
3. After reconnect, all stored subscriptions are recreated
4. `on_reconnect` callback is invoked with old and new subscription IDs
5. The async iterator continues seamlessly

A subscription whose `LIVE SELECT` fails during the resubscribe (an error from
the server, a timeout, the socket lost again) stays registered under its
previous id and is retried by the next reconnect; until then it receives
nothing. Stopping it (`stop()`, `kill()`, `unsubscribe()`) unregisters it, so
it is never re-created after that. A notification that arrives in the same read
as the `LIVE SELECT` reply, before the subscription knows its id, is held
briefly and delivered once the id is registered.

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
The table needs a change feed (`DEFINE TABLE users CHANGEFEED 7d`).

```python
from surreal_sdk import ChangeFeedStream

stream = ChangeFeedStream(conn, "users")

# Stream changes written from now on
async for change in stream.stream():
    print(change["versionstamp"], change["changes"])
    checkpoint = stream.cursor  # pass back as since= to resume after this change
```

Each entry is `{"versionstamp": int, "changes": [...]}` and is delivered once. A
change is `{"update": record}`, `{"delete": {...}}` or `{"define_table": {...}}`;
the change feed records a **create as an update**. On a table defined with
`CHANGEFEED ... INCLUDE ORIGINAL`, an update is `{"current": record, "update": [<JSON patch>]}`.

`since` (on `stream()`, `stream_batch()`, `get_changes()`) accepts:

| `since` | Meaning |
| --- | --- |
| `None` (default) | after the last entry already in the feed: only new changes (found with at most 130 `SHOW CHANGES … LIMIT 1` probes, whatever the size of the retained feed) |
| `int` or decimal string (`"117515092609728513"`) | a versionstamp, inclusive; `stream.cursor` is one |
| `datetime` | that instant; a naive one is read as UTC (with a `RuntimeWarning`) |
| ISO 8601 string (`"2026-01-01"`, `"2026-10-27T12:00:00Z"`) | that instant, UTC when it has no offset |

Anything else raises `ValueError` before a query is sent. A failed request or a
statement the server rejects raises `ChangeFeedError` from the iteration; it is not
retried. Whether a datetime start selects anything depends on the storage engine
(measured on SurrealDB 3.3.5): RocksDB selects the entries written from that instant,
in-memory storage and SurrealKV select nothing for any instant after the epoch. A
stream started from a datetime there emits one `RuntimeWarning` when it misses a
change written after its start, and keeps polling. Prefer a versionstamp
(`since=0` reads the whole retained feed).

An entry holds every change one transaction made to the table. `QuerySet.changes()`
yields one event per change, so its `cursor` stays on the entry until the entry's
last event is delivered: a resume from a cursor taken mid-transaction delivers that
transaction again, whole (at-least-once).

### Multi-Table Change Feed

```python
from surreal_sdk.streaming.change_feed import MultiTableChangeFeed

feed = MultiTableChangeFeed(conn, ["users", "orders", "products"])
async for table, change in feed.stream():  # one entry at a time
    print(f"Table {table}: {change['changes']}")
```

Each table keeps its own cursor; `since` applies to every table. Each poll is one
request that reads the database feed once, from the smallest cursor, and advances
every table's cursor from that window.

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
    SurrealDBError,              # Base exception
    ConnectionError,             # Connection failed
    AuthenticationError,         # Auth failed
    QueryError,                  # Query execution failed
    TimeoutError,                # Request timed out
    TransactionError,            # Transaction failed
    TransactionConflictError,    # Retryable transaction conflict (v0.5.9)
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

# Detect retryable conflicts (v0.5.9)
try:
    await conn.merge("events:1", {"processed_by": ["pod-a"]})
except Exception as e:
    if TransactionConflictError.is_conflict_error(e):
        print("Conflict detected - safe to retry")
    else:
        raise
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
