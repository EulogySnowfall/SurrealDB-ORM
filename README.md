# SurrealDB-ORM

![Python](https://img.shields.io/badge/python-3.12%2B-blue)
![CI](https://github.com/EulogySnowfall/SurrealDB-ORM/actions/workflows/ci.yml/badge.svg)
[![codecov](https://codecov.io/gh/EulogySnowfall/SurrealDB-ORM/graph/badge.svg?token=XUONTG2M6Z)](https://codecov.io/gh/EulogySnowfall/SurrealDB-ORM)
![GitHub License](https://img.shields.io/github/license/EulogySnowfall/SurrealDB-ORM)

> **Alpha Software** - APIs may change. Use in non-production environments.

**SurrealDB-ORM** is a Django-style ORM for [SurrealDB](https://surrealdb.com/) with async support, Pydantic validation, and JWT authentication.

**Includes a custom SDK (`surreal_sdk`)** - Zero dependency on the official `surrealdb` package!

---

## What's New in 0.3.0

- **ORM Transactions** - Transaction support for Model CRUD operations (`save()`, `update()`, `merge()`, `delete()`)
- **QuerySet Aggregations** - `count()`, `sum()`, `avg()`, `min()`, `max()` methods
- **GROUP BY Support** - Django-style `values()` and `annotate()` for grouped aggregations

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
# Basic installation
pip install surrealdb-orm

# With CLI support
pip install surrealdb-orm[cli]

# Full installation (CLI + CBOR)
pip install surrealdb-orm[all]
```

**Requirements:** Python 3.12+ | SurrealDB 2.6.0+

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
from surreal_sdk import LiveQuery, LiveNotification, LiveAction

async def on_change(notification: LiveNotification):
    if notification.action == LiveAction.CREATE:
        print(f"New record: {notification.result}")
    elif notification.action == LiveAction.UPDATE:
        print(f"Updated: {notification.result}")
    elif notification.action == LiveAction.DELETE:
        print(f"Deleted: {notification.result}")

live = LiveQuery(ws_conn, "orders")
await live.subscribe(on_change)
# ... records changes trigger callbacks ...
await live.unsubscribe()
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

## License

MIT License - See [LICENSE](LICENSE) file.

---

**Author:** Yannick Croteau | **GitHub:** [EulogySnowfall](https://github.com/EulogySnowfall)
