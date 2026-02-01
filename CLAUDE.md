# SurrealDB-ORM - Development Context

> Context document for Claude AI - Last updated: February 2026

## Project Vision

**Initial goal:** Django-style ORM for SurrealDB using the official Python SDK.

**Current direction:** Complete SDK + ORM solution that connects directly via WebSocket or HTTP to SurrealDB, with zero dependency on the official `surrealdb` package.

---

## Current Version: 0.2.2 (Alpha)

### What's New

- **Atomic Transactions** - Context manager for atomic operations with auto-commit/rollback
- **Typed Functions API** - Fluent API for SurrealDB built-in and custom functions
- **Django-style Migrations** - Full migration system with CLI
- **JWT Authentication** - SurrealDB native authentication support
- **Test Infrastructure** - Automatic container lifecycle management

---

## Architecture

```text
src/
├── surreal_orm/                 # Django-style ORM
│   ├── __init__.py              # Public API exports
│   ├── connection_manager.py    # Connection singleton (uses surreal_sdk)
│   ├── model_base.py            # BaseSurrealModel with CRUD methods
│   ├── query_set.py             # Fluent query builder
│   ├── auth.py                  # JWT authentication mixin
│   ├── fields.py                # Encrypted field type
│   ├── types.py                 # TableType enum, SurrealConfigDict
│   └── migrations/              # Migration system
│       ├── operations.py        # CreateTable, AddField, etc.
│       ├── state.py             # SchemaState with diff algorithm
│       ├── generator.py         # Migration file generator
│       └── executor.py          # Migration executor
│
└── surreal_sdk/                 # Custom SDK (zero external dependencies)
    ├── __init__.py              # SurrealDB.http() / .ws() / .pool()
    ├── exceptions.py            # SurrealDBError, TransactionError, etc.
    ├── types.py                 # QueryResponse, RecordResponse, etc.
    ├── transaction.py           # HTTPTransaction, WebSocketTransaction
    ├── functions.py             # Typed functions API (db.fn.math.sqrt)
    ├── connection/
    │   ├── base.py              # BaseSurrealConnection ABC
    │   ├── http.py              # HTTPConnection (stateless, httpx)
    │   ├── websocket.py         # WebSocketConnection (stateful, aiohttp)
    │   └── pool.py              # ConnectionPool
    └── streaming/
        ├── change_feed.py       # ChangeFeedStream (CDC, HTTP)
        └── live_query.py        # LiveQuery (WebSocket real-time)
```

---

## Key Components

### 1. Custom SDK (`surreal_sdk`)

The SDK provides direct connection to SurrealDB without the official package.

**Connection Types:**

```python
from surreal_sdk import SurrealDB

# HTTP (stateless, microservices)
async with SurrealDB.http("http://localhost:8000", "ns", "db") as db:
    await db.signin("root", "root")
    result = await db.query("SELECT * FROM users")

# WebSocket (stateful, real-time)
async with SurrealDB.ws("ws://localhost:8000", "ns", "db") as db:
    await db.signin("root", "root")
    await db.live("orders", callback=on_change)

# Connection Pool
async with SurrealDB.pool("http://localhost:8000", "ns", "db", size=10) as pool:
    await pool.set_credentials("root", "root")
    async with pool.acquire() as conn:
        await conn.query("SELECT * FROM users")
```

**Transactions:**

```python
# Atomic operations with auto-commit/rollback
async with db.transaction() as tx:
    await tx.create("orders:1", {"total": 100})
    await tx.create("payments:1", {"amount": 100})
    # Auto-commit on success, auto-rollback on exception
```

**Typed Functions:**

```python
# Built-in functions
sqrt = await db.fn.math.sqrt(16)           # 4.0
now = await db.fn.time.now()               # datetime
sha = await db.fn.crypto.sha256("data")    # hash

# Custom user-defined functions
result = await db.fn.my_custom_function(arg1, arg2)
```

### 2. ORM (`surreal_orm`)

Django-style ORM built on top of the SDK.

**Model Definition:**

```python
from surreal_orm import BaseSurrealModel, SurrealConfigDict
from surreal_orm.types import TableType
from surreal_orm.fields import Encrypted

class User(BaseSurrealModel):
    model_config = SurrealConfigDict(
        table_type=TableType.USER,
        permissions={"select": "$auth.id = id"},
    )

    id: str | None = None
    email: str
    password: Encrypted  # Auto-hashed with argon2
    name: str
    age: int = 0
```

**QuerySet:**

```python
# Filter with Django-style lookups
users = await User.objects().filter(age__gte=18, name__startswith="A").exec()

# Supported lookups:
# exact, gt, gte, lt, lte, in, like, ilike, contains, icontains,
# startswith, istartswith, endswith, iendswith, match, regex, isnull
```

**JWT Authentication:**

```python
from surreal_orm.auth import AuthenticatedUserMixin

class User(AuthenticatedUserMixin, BaseSurrealModel):
    # ...

# Signup
user = await User.signup(email="alice@example.com", password="secret", name="Alice")

# Signin
user, token = await User.signin(email="alice@example.com", password="secret")
```

### 3. Migration System

Django-style migrations with CLI support.

```bash
# Generate migration from model changes
surreal-orm makemigrations --name initial

# Apply migrations
surreal-orm migrate

# Check status
surreal-orm status

# Rollback
surreal-orm rollback 0001_initial
```

---

## Dependencies

**Runtime:**

- `pydantic >= 2.10.5` - Model validation
- `httpx >= 0.27.0` - HTTP client
- `aiohttp >= 3.9.0` - WebSocket client
- `click >= 8.1.0` - CLI (optional, with `[cli]`)

**No dependency on the official `surrealdb` package!**

**Dev:**

- pytest, pytest-asyncio, pytest-cov
- mypy, ruff
- docker (for integration tests)

---

## Test Structure

```text
tests/
├── conftest.py              # Container lifecycle management
├── test_unit.py             # ORM unit tests
├── test_manager.py          # ConnectionManager tests
├── test_e2e.py              # ORM integration tests
└── sdk/
    ├── test_connection.py   # HTTP/WS connection tests
    ├── test_transactions.py # Transaction tests
    ├── test_functions.py    # Typed functions tests
    └── test_streaming.py    # Live query/change feed tests
```

**Running tests:**

```bash
make test              # Unit tests only (no SurrealDB needed)
make test-sdk          # SDK tests only
make test-integration  # Integration tests (auto-starts container)
make test-all          # All tests
make ci-lint           # Run all linters (mypy, ruff)
```

---

## Useful Commands

```bash
# Installation
uv sync

# Tests
make test              # Unit tests (no SurrealDB)
make test-sdk          # SDK tests only
make test-integration  # Integration tests (auto-manages container)
make test-all          # All tests

# Docker/SurrealDB
make db-up             # Start test SurrealDB (port 8001)
make db-dev            # Start dev SurrealDB (port 8000, persistent)
make db-shell          # Interactive SQL shell

# Lint
make ci-lint           # Run all linters
uv run ruff check src/
uv run mypy src/

# Build
uv build
```

---

## TODO / Roadmap

See full roadmap: [docs/roadmap.md](docs/roadmap.md)

### Completed (0.2.x)

- [x] Custom SDK (HTTP + WebSocket)
- [x] Connection pooling
- [x] Atomic transactions (SDK level)
- [x] Typed functions API
- [x] Migration system
- [x] JWT Authentication
- [x] CLI commands
- [x] Live Queries
- [x] Change Feeds

### v0.3.0 (Next) - ORM Transactions & Aggregations

- [ ] Model-level transactions (`user.save(tx=tx)`)
- [ ] QuerySet aggregations (`count()`, `sum()`, `avg()`, `min()`, `max()`)
- [ ] GROUP BY with `values()` and `annotate()`

### v0.3.1 - Bulk Operations

- [ ] `bulk_create()` with atomic option
- [ ] `bulk_update()` for filtered querysets
- [ ] `bulk_delete()` with transaction support

### v0.4.x - Relations & Graph

- [ ] `ForeignKey`, `ManyToMany` fields
- [ ] `Relation` for graph edges (SurrealDB's `->edge->`)
- [ ] Graph traversal queries
- [ ] `prefetch_related()` for eager loading

### v0.5.x - Real-time

- [ ] Live Models (real-time sync)
- [ ] Model signals (pre_save, post_save, etc.)
- [ ] Change Feed integration for ORM

---

## Known Issues

1. `refresh()` in model_base.py doesn't reassign data to instance
2. ORDER BY positioned after LIMIT/START in generated SQL

---

## Code Conventions

- **Language:** English (code, comments, docs)
- **Style:** ruff format + ruff lint
- **Types:** mypy strict
- **Async:** All I/O must be async
- **Tests:** pytest-asyncio for async tests

---

## Contact

**Author:** Yannick Croteau
**GitHub:** EulogySnowfall
