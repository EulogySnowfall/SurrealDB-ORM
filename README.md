# SurrealDB-ORM

![Python](https://img.shields.io/badge/python-3.12%2B-blue)
![CI](https://github.com/EulogySnowfall/SurrealDB-ORM/actions/workflows/ci.yml/badge.svg)
[![codecov](https://codecov.io/gh/EulogySnowfall/SurrealDB-ORM/graph/badge.svg?token=XUONTG2M6Z)](https://codecov.io/gh/EulogySnowfall/SurrealDB-ORM)
![GitHub License](https://img.shields.io/github/license/EulogySnowfall/SurrealDB-ORM)

> **Alpha Software** - APIs may change. Use in non-production environments.

**SurrealDB-ORM** is a Django-style ORM for [SurrealDB](https://surrealdb.com/) with async support, Pydantic validation, and JWT authentication.

**Includes a custom SDK (`surreal_sdk`)** - Zero dependency on the official `surrealdb` package!

---

## What's New in 0.5.x

### v0.5.5 - CBOR Protocol & Field Aliases

- **CBOR Protocol (Default)** - Binary protocol for WebSocket connections
  - `cbor2` is now a **required dependency**
  - CBOR is the **default protocol** for WebSocket (fixes `data:` prefix string issues)
  - Aligns with official SurrealDB SDK behavior
- **`unset_connection_sync()`** - Synchronous version for non-async cleanup contexts
- **Field Alias Support** - Map Python field names to different DB column names
  - Use `Field(alias="db_column")` to store under a different name in DB

### v0.5.4 - API Improvements

- **Record ID format handling** - `QuerySet.get()` accepts both `"abc123"` and `"table:abc123"`
- **`remove_relation()` accepts string IDs** - Pass string IDs instead of model instances
- **`raw_query()` class method** - Execute arbitrary SurrealQL from model class

### v0.5.3.3 - Bug Fix

- **`from_db()` fields_set fix** - Fixed bug where DB-loaded fields were incorrectly included in updates via `exclude_unset=True`

### v0.5.3.2 - Critical Bug Fix

- **QuerySet table name fix** - Fixed critical bug where QuerySet used class name instead of `table_name` from config
- **`QuerySet.get()` signature** - Now accepts `id=` keyword argument in addition to positional `id_item`

### v0.5.3.1 - Bug Fixes

- **Partial updates for persisted records** - `save()` now uses `merge()` for already-persisted records, only sending modified fields
- **datetime parsing** - `_update_from_db()` now parses ISO 8601 strings to `datetime` objects automatically
- **`_db_persisted` flag** - Internal tracking to distinguish new vs persisted records

### v0.5.3 - ORM Improvements

- **Upsert save behavior** - `save()` now uses `upsert` for new records with ID (idempotent, Django-like)
- **`server_fields` config** - Exclude server-generated fields (created_at, updated_at) from saves
- **`merge()` returns self** - Now returns the updated model instance instead of None
- **`save()` updates self** - Updates original instance attributes instead of returning new object
- **NULL values fix** - `exclude_unset=True` now works correctly after loading from DB

### v0.5.2 - Bug Fixes & FieldType Improvements

- **FieldType enum** - Enhanced migration type system with `generic()` and `from_python_type()` methods
- **datetime serialization** - Proper JSON encoding for datetime, date, time, Decimal, UUID
- **Fluent API** - `connect()` now returns `self` for method chaining
- **Session cleanup** - WebSocket callback tasks properly tracked and cancelled
- **Optional fields** - `exclude_unset=True` prevents None from overriding DB defaults
- **Parameter alias** - `username` parameter alias for `user` in ConnectionManager

### v0.5.1 - Security Workflows

- **Dependabot integration** - Automatic dependency security updates
- **Auto-merge** - Dependabot PRs merged after CI passes
- **SurrealDB monitoring** - Integration tests on new SurrealDB releases

### v0.5.0 - Real-time SDK Enhancements

- **Live Select Stream** - Async iterator pattern for real-time changes
  - `async with db.live_select("table") as stream: async for change in stream:`
  - `LiveChange` dataclass with `record_id`, `action`, `result`, `changed_fields`
  - WHERE clause support with parameterized queries
- **Auto-Resubscribe** - Automatic reconnection after WebSocket disconnect
  - `auto_resubscribe=True` parameter for seamless K8s pod restart recovery
  - `on_reconnect(old_id, new_id)` callback for tracking ID changes
- **Typed Function Calls** - Pydantic/dataclass return type support
  - `await db.call("fn::my_func", params={...}, return_type=MyModel)`

### v0.4.0 - Relations & Graph

- **Relations & Graph Traversal** - Django-style relation definitions with SurrealDB graph support
  - `ForeignKey`, `ManyToMany`, `Relation` field types
  - Relation operations: `add()`, `remove()`, `set()`, `clear()`, `all()`, `filter()`, `count()`
  - Model methods: `relate()`, `remove_relation()`, `get_related()`
  - QuerySet extensions: `select_related()`, `prefetch_related()`, `traverse()`, `graph_query()`

---

## Table of Contents

- [Installation](#installation)
- [Quick Start](#quick-start)
  - [Using the SDK (Recommended)](#using-the-sdk-recommended)
  - [Using the ORM](#using-the-orm)
- [SDK Features](#sdk-features)
  - [Connections](#connections)
  - [Transactions](#transactions)
  - [Typed Functions](#typed-functions)
  - [Live Queries](#live-queries)
- [ORM Features](#orm-features)
- [CLI Commands](#cli-commands)
- [Documentation](#documentation)
- [Contributing](#contributing)

---

## Installation

```bash
# Basic installation (includes CBOR support)
pip install surrealdb-orm

# With CLI support
pip install surrealdb-orm[cli]
```

**Requirements:** Python 3.12+ | SurrealDB 2.6.0+

**Included:** `pydantic`, `httpx`, `aiohttp`, `cbor2` (CBOR is the default protocol for WebSocket)

---

## Quick Start

### Using the SDK (Recommended)

```python
from surreal_sdk import SurrealDB

async def main():
    # HTTP connection (stateless, ideal for microservices)
    async with SurrealDB.http("http://localhost:8000", "namespace", "database") as db:
        await db.signin("root", "root")

        # CRUD operations
        user = await db.create("users", {"name": "Alice", "age": 30})
        users = await db.query("SELECT * FROM users WHERE age > $min", {"min": 18})

        # Atomic transactions
        async with db.transaction() as tx:
            await tx.create("accounts:alice", {"balance": 1000})
            await tx.create("accounts:bob", {"balance": 500})
            # Auto-commit on success, auto-rollback on exception

        # Built-in functions with typed API
        result = await db.fn.math.sqrt(16)  # Returns 4.0
        now = await db.fn.time.now()        # Current timestamp
```

### Using the ORM

```python
from surreal_orm import BaseSurrealModel, SurrealDBConnectionManager

# 1. Define your model
class User(BaseSurrealModel):
    id: str | None = None
    name: str
    email: str
    age: int = 0

# 2. Configure connection
SurrealDBConnectionManager.set_connection(
    url="http://localhost:8000",
    user="root",
    password="root",
    namespace="myapp",
    database="main",
)

# 3. CRUD operations
user = User(name="Alice", email="alice@example.com", age=30)
await user.save()

users = await User.objects().filter(age__gte=18).order_by("name").limit(10).exec()
```

---

## SDK Features

### Connections

| Type          | Use Case                 | Features                 |
| ------------- | ------------------------ | ------------------------ |
| **HTTP**      | Microservices, REST APIs | Stateless, simple        |
| **WebSocket** | Real-time apps           | Live queries, persistent |
| **Pool**      | High-throughput          | Connection reuse         |

```python
from surreal_sdk import SurrealDB, HTTPConnection, WebSocketConnection

# HTTP (stateless)
async with SurrealDB.http("http://localhost:8000", "ns", "db") as db:
    await db.signin("root", "root")

# WebSocket (stateful, real-time)
async with SurrealDB.ws("ws://localhost:8000", "ns", "db") as db:
    await db.signin("root", "root")
    await db.live("orders", callback=on_order_change)

# Connection Pool
async with SurrealDB.pool("http://localhost:8000", "ns", "db", size=10) as pool:
    await pool.set_credentials("root", "root")
    async with pool.acquire() as conn:
        await conn.query("SELECT * FROM users")
```

### Transactions

Atomic transactions with automatic commit/rollback:

```python
# WebSocket: Immediate execution with server-side transaction
async with db.transaction() as tx:
    await tx.update("players:abc", {"is_ready": True})
    await tx.update("game_tables:xyz", {"ready_count": "+=1"})
    # Statements execute immediately
    # COMMIT on success, CANCEL on exception

# HTTP: Batched execution (all-or-nothing)
async with db.transaction() as tx:
    await tx.create("orders:1", {"total": 100})
    await tx.create("payments:1", {"amount": 100})
    # Statements queued, executed atomically at commit
```

**Transaction Methods:**

- `tx.query(sql, vars)` - Execute raw SurrealQL
- `tx.create(thing, data)` - Create record
- `tx.update(thing, data)` - Replace record
- `tx.delete(thing)` - Delete record
- `tx.relate(from, edge, to)` - Create graph edge
- `tx.commit()` - Explicit commit
- `tx.rollback()` - Explicit rollback

### Typed Functions

Fluent API for SurrealDB functions:

```python
# Built-in functions (namespace::function)
sqrt = await db.fn.math.sqrt(16)           # 4.0
now = await db.fn.time.now()               # datetime
length = await db.fn.string.len("hello")   # 5
sha = await db.fn.crypto.sha256("data")    # hash string

# Custom user-defined functions (fn::function)
result = await db.fn.my_custom_function(arg1, arg2)
# Executes: RETURN fn::my_custom_function($arg0, $arg1)
```

**Available Namespaces:**
`array`, `crypto`, `duration`, `geo`, `http`, `math`, `meta`, `object`, `parse`, `rand`, `session`, `string`, `time`, `type`, `vector`

### Live Queries

Real-time updates via WebSocket:

```python
from surreal_sdk import LiveAction

# Async iterator pattern (recommended)
async with db.live_select(
    "orders",
    where="status = $status",
    params={"status": "pending"},
    auto_resubscribe=True,  # Auto-reconnect on WebSocket drop
) as stream:
    async for change in stream:
        match change.action:
            case LiveAction.CREATE:
                print(f"New order: {change.result}")
            case LiveAction.UPDATE:
                print(f"Updated: {change.record_id}")
            case LiveAction.DELETE:
                print(f"Deleted: {change.record_id}")

# Callback-based pattern
from surreal_sdk import LiveQuery, LiveNotification

async def on_change(notification: LiveNotification):
    print(f"{notification.action}: {notification.result}")

live = LiveQuery(ws_conn, "orders")
await live.subscribe(on_change)
# ... record changes trigger callbacks ...
await live.unsubscribe()
```

**Typed Function Calls:**

```python
from pydantic import BaseModel

class VoteResult(BaseModel):
    success: bool
    count: int

# Call SurrealDB function with typed return
result = await db.call(
    "cast_vote",
    params={"user": "alice", "vote": "yes"},
    return_type=VoteResult
)
print(result.success, result.count)  # Typed access
```

---

## ORM Features

### QuerySet with Django-style Lookups

```python
# Filter with lookups
users = await User.objects().filter(age__gte=18, name__startswith="A").exec()

# Supported lookups
# exact, gt, gte, lt, lte, in, like, ilike, contains, icontains,
# startswith, istartswith, endswith, iendswith, match, regex, isnull
```

### ORM Transactions

```python
from surreal_orm import SurrealDBConnectionManager

# Via ConnectionManager
async with SurrealDBConnectionManager.transaction() as tx:
    user = User(name="Alice", balance=1000)
    await user.save(tx=tx)

    order = Order(user_id=user.id, total=100)
    await order.save(tx=tx)
    # Auto-commit on success, auto-rollback on exception

# Via Model class method
async with User.transaction() as tx:
    await user1.save(tx=tx)
    await user2.delete(tx=tx)
```

### Aggregations

```python
# Simple aggregations
total = await User.objects().count()
total = await User.objects().filter(active=True).count()

# Field aggregations
avg_age = await User.objects().avg("age")
total = await Order.objects().filter(status="paid").sum("amount")
min_val = await Product.objects().min("price")
max_val = await Product.objects().max("price")
```

### GROUP BY with Aggregations

```python
from surreal_orm import Count, Sum, Avg

# Group by single field
stats = await Order.objects().values("status").annotate(
    count=Count(),
    total=Sum("amount"),
).exec()
# Result: [{"status": "paid", "count": 42, "total": 5000}, ...]

# Group by multiple fields
monthly = await Order.objects().values("status", "month").annotate(
    count=Count(),
).exec()
```

### Bulk Operations

```python
# Bulk create
users = [User(name=f"User{i}") for i in range(100)]
created = await User.objects().bulk_create(users)

# Atomic bulk create (all-or-nothing)
created = await User.objects().bulk_create(users, atomic=True)

# Bulk update
updated = await User.objects().filter(status="pending").bulk_update(
    {"status": "active"}
)

# Bulk delete
deleted = await User.objects().filter(status="deleted").bulk_delete()
```

### Table Types

| Type     | Description                 |
| -------- | --------------------------- |
| `NORMAL` | Standard table (default)    |
| `USER`   | Auth table with JWT support |
| `STREAM` | Real-time with CHANGEFEED   |
| `HASH`   | Lookup/cache (SCHEMALESS)   |

```python
from surreal_orm import BaseSurrealModel, SurrealConfigDict
from surreal_orm.types import TableType

class User(BaseSurrealModel):
    model_config = SurrealConfigDict(
        table_type=TableType.USER,
        permissions={"select": "$auth.id = id"},
    )
```

### JWT Authentication

```python
from surreal_orm.auth import AuthenticatedUserMixin
from surreal_orm.fields import Encrypted

class User(AuthenticatedUserMixin, BaseSurrealModel):
    model_config = SurrealConfigDict(table_type=TableType.USER)
    email: str
    password: Encrypted  # Auto-hashed with argon2
    name: str

# Signup
user = await User.signup(email="alice@example.com", password="secret", name="Alice")

# Signin
user, token = await User.signin(email="alice@example.com", password="secret")

# Validate token
user = await User.authenticate_token(token)
```

---

## CLI Commands

Requires `pip install surrealdb-orm[cli]`

| Command             | Description                 |
| ------------------- | --------------------------- |
| `makemigrations`    | Generate migration files    |
| `migrate`           | Apply schema migrations     |
| `rollback <target>` | Rollback to migration       |
| `status`            | Show migration status       |
| `shell`             | Interactive SurrealQL shell |

```bash
# Generate and apply migrations
surreal-orm makemigrations --name initial
surreal-orm migrate -u http://localhost:8000 -n myns -d mydb

# Environment variables supported
export SURREAL_URL=http://localhost:8000
export SURREAL_NAMESPACE=myns
export SURREAL_DATABASE=mydb
surreal-orm migrate
```

---

## Documentation

| Document                               | Description              |
| -------------------------------------- | ------------------------ |
| [SDK Guide](docs/sdk.md)               | Full SDK documentation   |
| [Migration System](docs/migrations.md) | Django-style migrations  |
| [Authentication](docs/auth.md)         | JWT authentication guide |
| [Roadmap](docs/roadmap.md)             | Future features planning |
| [CHANGELOG](CHANGELOG)                 | Version history          |

---

## Contributing

```bash
# Clone and install
git clone https://github.com/EulogySnowfall/SurrealDB-ORM.git
cd SurrealDB-ORM
uv sync

# Run tests (SurrealDB container managed automatically)
make test              # Unit tests only
make test-integration  # With integration tests

# Start SurrealDB manually
make db-up             # Test instance (port 8001)
make db-dev            # Dev instance (port 8000)

# Lint
make ci-lint           # Run all linters
```

---

## Related Projects

### [SurrealDB-ORM-lite](https://github.com/EulogySnowfall/SurrealDB-ORM-lite)

A lightweight Django-style ORM built on the **official SurrealDB Python SDK**.

| Feature         | SurrealDB-ORM          | SurrealDB-ORM-lite   |
| --------------- | ---------------------- | -------------------- |
| SDK             | Custom (`surreal_sdk`) | Official `surrealdb` |
| Live Queries    | Full support           | Limited              |
| CBOR Protocol   | Default                | SDK-dependent        |
| Transactions    | Full support           | Basic                |
| Typed Functions | Yes                    | No                   |

Choose **SurrealDB-ORM-lite** if you prefer to use the official SDK with basic ORM features.

```bash
pip install surreal-orm-lite
```

---

## License

MIT License - See [LICENSE](LICENSE) file.

---

**Author:** Yannick Croteau | **GitHub:** [EulogySnowfall](https://github.com/EulogySnowfall)
