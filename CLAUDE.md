# SurrealDB-ORM - Development Context

> Context document for Claude AI - Last updated: February 2026

## Project Vision

**Initial goal:** Django-style ORM for SurrealDB using the official Python SDK.

**Current direction:** Complete SDK + ORM solution that connects directly via WebSocket or HTTP to SurrealDB, with zero dependency on the official `surrealdb` package.

---

## Current Version: 0.5.0 (Alpha)

### What's New in 0.5.0

- **Live Select Stream** - Async iterator pattern for real-time change notifications
  - `LiveSelectStream` with `async with` context manager and `async for` iteration
  - `LiveChange` dataclass: `record_id`, `action`, `result`, `changed_fields`
  - `LiveAction` enum: `CREATE`, `UPDATE`, `DELETE`
  - WHERE clause support with parameterized queries
  - DIFF mode for receiving only changed fields

- **Auto-Resubscribe** - Automatic reconnection after WebSocket disconnect
  - `auto_resubscribe=True` parameter on `live_select()`
  - `on_reconnect(old_id, new_id)` callback for tracking ID changes
  - Seamless recovery from K8s pod restarts and network interruptions

- **Typed Function Calls** - Pydantic/dataclass return type support
  - `call(function, params, return_type)` for typed results
  - Automatic conversion to Pydantic models and dataclasses

### What's New in 0.4.0

- **Relations & Graph Traversal** - Declarative relation definitions
  - `ForeignKey`, `ManyToMany`, `Relation` field types
  - `RelationManager` for lazy loading and operations (add, remove, set, clear)
  - Graph traversal with `traverse()` and `graph_query()`
  - `select_related()` and `prefetch_related()` for N+1 prevention
  - Model methods: `relate()`, `remove_relation()`, `get_related()`

### What's New in 0.3.x

- **ORM Transactions** (v0.3.0) - `tx` parameter on save/update/delete, `Model.transaction()` shortcut
- **Aggregations** (v0.3.0) - `Count`, `Sum`, `Avg`, `Min`, `Max` + GROUP BY with `values()`/`annotate()`
- **Bulk Operations** (v0.3.1) - `bulk_create()`, `bulk_update()`, `bulk_delete()` with atomic support

### What's New in 0.2.x

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
│   ├── model_base.py            # BaseSurrealModel with CRUD + relation methods
│   ├── query_set.py             # Fluent query builder + graph traversal
│   ├── relations.py             # RelationManager, RelationQuerySet, RelationDescriptor
│   ├── aggregations.py          # Count, Sum, Avg, Min, Max
│   ├── auth.py                  # JWT authentication mixin
│   ├── fields/                  # Field types
│   │   ├── encrypted.py         # Encrypted field type
│   │   └── relation.py          # ForeignKey, ManyToMany, Relation
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
        ├── live_query.py        # LiveQuery callback-based (WebSocket)
        └── live_select.py       # LiveSelectStream async iterator (WebSocket)
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

**Typed Function Calls (v0.5.0):**

```python
from pydantic import BaseModel

class VoteResult(BaseModel):
    success: bool
    new_count: int
    total_votes: int

# Call with typed return
result = await db.call(
    "cast_vote",
    params={"user_id": "alice", "table_id": "game:123"},
    return_type=VoteResult
)
# result is VoteResult instance, not dict
print(result.success, result.new_count)
```

**Live Select Stream (v0.5.0):**

```python
from surreal_sdk import LiveAction

# Async iterator pattern with WHERE clause and parameters
async with db.live_select(
    "players",
    where="table_id = $id",
    params={"id": "game_tables:xyz"},
    auto_resubscribe=True  # Auto-reconnect on WebSocket drop
) as stream:
    async for change in stream:
        match change.action:
            case LiveAction.CREATE:
                print(f"Player joined: {change.result['name']}")
            case LiveAction.UPDATE:
                if change.result.get("is_ready"):
                    print(f"Player ready: {change.record_id}")
            case LiveAction.DELETE:
                print(f"Player left: {change.record_id}")

# Callback-based multi-stream manager
from surreal_sdk import LiveSelectManager

manager = LiveSelectManager(db)
await manager.watch("players", on_player_change, where="table_id = $id", params={"id": table_id})
await manager.watch("game_tables", on_table_change, where="id = $id", params={"id": table_id})

# Later: cleanup
await manager.stop_all()
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

### Completed (0.3.0) - ORM Transactions & Aggregations

- [x] Model-level transactions (`user.save(tx=tx)`)
- [x] QuerySet aggregations (`count()`, `sum()`, `avg()`, `min()`, `max()`)
- [x] GROUP BY with `values()` and `annotate()`

### Completed (0.3.1) - Bulk Operations

- [x] `bulk_create()` with atomic option
- [x] `bulk_update()` for filtered querysets
- [x] `bulk_delete()` with transaction support

### Completed (0.4.0) - Relations & Graph

- [x] `ForeignKey`, `ManyToMany` fields
- [x] `Relation` for graph edges (SurrealDB's `->edge->`)
- [x] Graph traversal queries (`traverse()`, `graph_query()`)
- [x] `select_related()` and `prefetch_related()` for eager loading
- [x] Model methods: `relate()`, `remove_relation()`, `get_related()`

### Completed (0.5.0) - Real-time SDK Enhancements

- [x] `LiveSelectStream` - Async iterator for Live Queries
- [x] `LiveSelectManager` - Multi-stream callback management
- [x] Auto-resubscribe on WebSocket reconnect
- [x] `LiveChange` dataclass with `record_id`, `action`, `changed_fields`
- [x] Typed function calls with Pydantic/dataclass support

### v0.6.x (Next) - ORM Real-time Integration

- [ ] Live Models (real-time sync at ORM level)
- [ ] Model signals (pre_save, post_save, etc.)
- [ ] Change Feed integration for ORM

---

## Real-World Use Case: Multiplayer Game Backend

The SDK's real-time features are designed for use cases like multiplayer game backends with 2-4 players per table.

### Scenario

A card/board game where players:

- Join tables and mark themselves "ready"
- Cast votes during gameplay
- Disconnect/reconnect during the game
- Need real-time synchronization

### Implementation Pattern

```python
from surreal_sdk import SurrealDB, LiveAction
from pydantic import BaseModel

# === Models ===
class VoteResult(BaseModel):
    success: bool
    new_count: int
    total_votes: int

# === Real-time Event Handling ===
async def on_player_change(change):
    """Handle player status changes."""
    match change.action:
        case LiveAction.CREATE:
            await notify_table(f"Player joined: {change.result['name']}")
        case LiveAction.UPDATE:
            if change.result.get("is_ready"):
                await check_all_ready(change.result["table_id"])
        case LiveAction.DELETE:
            await handle_player_disconnect(change.record_id)

async def on_table_change(change):
    """Handle game table status changes."""
    if change.action == LiveAction.UPDATE:
        status = change.result.get("status")
        if status == "voting":
            await start_vote_timer()
        elif status == "completed":
            await show_results()

# === Main Game Loop ===
async def run_game(table_id: str):
    async with SurrealDB.ws("ws://localhost:8000", "game", "prod") as db:
        await db.signin("root", "root")

        # Subscribe to players at this table (auto-reconnect enabled)
        async with db.live_select(
            "players",
            where="table_id = $table_id",
            params={"table_id": table_id},
            auto_resubscribe=True,
            on_reconnect=lambda old, new: print(f"Reconnected: {old} -> {new}")
        ) as player_stream:

            # Also watch the table itself
            async with db.live_select(
                "game_tables",
                where="id = $id",
                params={"id": table_id},
                auto_resubscribe=True
            ) as table_stream:

                # Process events from both streams
                async for change in player_stream:
                    await on_player_change(change)

# === Typed Function Calls ===
async def cast_vote(db, user_id: str, table_id: str) -> VoteResult:
    """Cast a vote with typed return."""
    return await db.call(
        "cast_vote",  # SurrealDB user-defined function
        params={"user_id": user_id, "table_id": table_id},
        return_type=VoteResult
    )
```

### Key Features for Game Backends

| Feature | Benefit |
|---------|---------|
| `live_select()` with WHERE | Subscribe only to relevant players/tables |
| `auto_resubscribe=True` | Seamless recovery from K8s pod restarts |
| `on_reconnect` callback | Track subscription ID changes for debugging |
| `LiveChange.action` | Distinguish CREATE/UPDATE/DELETE events |
| `LiveChange.record_id` | Quick access to affected record ID |
| `LiveChange.changed_fields` | (DIFF mode) Know exactly what changed |
| Typed `call()` | Get Pydantic models instead of raw dicts |

### SurrealDB Function Example

Define in SurrealDB for typed function calls:

```sql
DEFINE FUNCTION fn::cast_vote($user_id: string, $table_id: string) {
    LET $player = SELECT * FROM players WHERE user_id = $user_id AND table_id = $table_id;
    IF $player.has_voted {
        RETURN { success: false, new_count: 0, total_votes: 0 };
    };
    UPDATE players SET has_voted = true WHERE id = $player.id;
    LET $count = (SELECT count() FROM players WHERE table_id = $table_id AND has_voted = true GROUP ALL).count;
    LET $total = (SELECT count() FROM players WHERE table_id = $table_id GROUP ALL).count;
    RETURN { success: true, new_count: $count, total_votes: $total };
};
```

---

## Known Issues

*All previously documented issues have been fixed in v0.3.1:*

- ~~`refresh()` in model_base.py doesn't reassign data to instance~~ (Fixed)
- ~~ORDER BY positioned after LIMIT/START in generated SQL~~ (Fixed in v0.3.1)
- ~~Typo "primirary_key" in error message~~ (Fixed in v0.3.1)

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
