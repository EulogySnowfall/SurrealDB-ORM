# SurrealDB-ORM

![Python](https://img.shields.io/badge/python-3.12%2B-blue)
![CI](https://github.com/EulogySnowfall/SurrealDB-ORM/actions/workflows/ci.yml/badge.svg)
[![codecov](https://codecov.io/gh/EulogySnowfall/SurrealDB-ORM/graph/badge.svg?token=XUONTG2M6Z)](https://codecov.io/gh/EulogySnowfall/SurrealDB-ORM)
![GitHub License](https://img.shields.io/github/license/EulogySnowfall/SurrealDB-ORM)

> **Alpha Software** - APIs may change. Use in non-production environments.

**SurrealDB-ORM** is a Django-style ORM for [SurrealDB](https://surrealdb.com/) with async support, Pydantic validation, and JWT authentication.

**Includes a custom SDK (`surreal_sdk`)** - Zero dependency on the official `surrealdb` package!

---

## What's New in 0.13.0

### Events, Geospatial, Materialized Views & TYPE RELATION

- **DEFINE EVENT** — Server-side triggers in migrations

  ```python
  from surreal_orm import DefineEvent

  DefineEvent(
      name="email_audit", table="users",
      when="$before.email != $after.email",
      then="CREATE audit_log SET table = 'user', record = $value.id, action = $event",
  )
  ```

- **Geospatial Fields** — Typed geometry fields and proximity queries

  ```python
  from surreal_orm.fields import PointField, PolygonField
  from surreal_orm.geo import GeoDistance

  class Store(BaseSurrealModel):
      name: str
      location: PointField          # geometry<point>
      delivery_area: PolygonField   # geometry<polygon>

  # Proximity search: stores within 5km
  nearby = await Store.objects().nearby(
      "location", (-73.98, 40.74), max_distance=5000
  ).exec()

  # Distance annotation
  stores = await Store.objects().annotate(
      dist=GeoDistance("location", (-73.98, 40.74)),
  ).order_by("dist").limit(10).exec()
  ```

- **Materialized Views** — Read-only models backed by `DEFINE TABLE ... AS SELECT`

  ```python
  class OrderStats(BaseSurrealModel):
      model_config = SurrealConfigDict(
          table_name="order_stats",
          view_query="SELECT status, count() AS total, math::sum(amount) AS revenue FROM orders GROUP BY status",
      )
      status: str
      total: int
      revenue: float

  # Auto-maintained by SurrealDB — read-only queries only
  stats = await OrderStats.objects().all()
  await stats[0].save()  # TypeError: Cannot modify materialized view
  ```

- **TYPE RELATION** — Enforce graph edge constraints in migrations

  ```python
  class Likes(BaseSurrealModel):
      model_config = SurrealConfigDict(
          table_type=TableType.RELATION,
          relation_in="person",
          relation_out=["blog_post", "book"],
          enforced=True,
      )
  ```

---

## What's New in 0.12.0

### Vector Search & Full-Text Search

- **Vector Similarity Search** — KNN search with HNSW indexes for AI/RAG pipelines

  ```python
  from surreal_orm.fields import VectorField

  class Document(BaseSurrealModel):
      title: str
      embedding: VectorField[1536]

  # KNN similarity search (top 10 nearest neighbours)
  docs = await Document.objects().similar_to(
      "embedding", query_vector, limit=10
  ).exec()

  # Combined with filters
  docs = await Document.objects().filter(
      category="science"
  ).similar_to("embedding", query_vector, limit=5).exec()
  ```

- **Full-Text Search** — BM25 scoring, highlighting, and multi-field search

  ```python
  from surreal_orm import SearchScore, SearchHighlight

  results = await Post.objects().search(title="quantum").annotate(
      relevance=SearchScore(0),
      snippet=SearchHighlight("<b>", "</b>", 0),
  ).exec()
  ```

- **Hybrid Search** — Reciprocal Rank Fusion combining vector + FTS

  ```python
  results = await Document.objects().hybrid_search(
      vector_field="embedding", vector=query_vec, vector_limit=20,
      text_field="content", text_query="machine learning", text_limit=20,
  )
  ```

- **Analyzer & Index Operations** — `DefineAnalyzer`, HNSW and BM25 index support in migrations

---

## What's New in 0.11.0

### Advanced Queries & Caching

- **Subqueries** — Embed a QuerySet as a filter value in another QuerySet
- **Query Cache** — TTL-based caching with automatic invalidation on writes
- **Prefetch Objects** — Fine-grained control over related data prefetching

---

## What's New in 0.10.0

### Schema Introspection & Multi-Database Support

- **Schema Introspection** - Generate Python model code from an existing SurrealDB database

  ```python
  from surreal_orm import generate_models_from_db, schema_diff

  # Generate Python model code from existing database
  code = await generate_models_from_db(output_path="models.py")

  # Compare Python models against live database schema
  operations = await schema_diff(models=[User, Order, Product])
  for op in operations:
      print(op)  # Migration operations needed to sync
  ```

  - `DatabaseIntrospector` parses `INFO FOR DB` / `INFO FOR TABLE` into `SchemaState`
  - `ModelCodeGenerator` converts `SchemaState` to fully-typed Python model source code
  - Handles generic types (`array<string>`, `option<int>`, `record<users>`), VALUE/ASSERT expressions, encrypted fields, FLEXIBLE, READONLY
  - CLI: `surreal-orm inspectdb` and `surreal-orm schemadiff`

- **Multi-Database Support** - Named connection registry for routing models to different databases

  ```python
  from surreal_orm import SurrealDBConnectionManager

  # Register named connections
  SurrealDBConnectionManager.add_connection("default", url=..., ns=..., db=...)
  SurrealDBConnectionManager.add_connection("analytics", url=..., ns=..., db=...)

  # Model-level routing
  class AnalyticsEvent(BaseSurrealModel):
      model_config = SurrealConfigDict(connection="analytics")

  # Context manager override (async-safe)
  async with SurrealDBConnectionManager.using("analytics"):
      events = await AnalyticsEvent.objects().all()
  ```

  - `ConnectionConfig` frozen dataclass for immutable connection settings
  - `using()` async context manager with `contextvars` for async safety
  - Full backward compatibility: `set_connection()` delegates to `add_connection("default", ...)`
  - `list_connections()`, `get_config()`, `remove_connection()` registry management

---

## What's New in 0.9.0

### ORM Real-time Features: Live Models + Change Feed

- **Live Models** - Real-time subscriptions at the ORM level yielding typed Pydantic model instances

  ```python
  from surreal_orm import LiveAction

  async with User.objects().filter(role="admin").live() as stream:
      async for event in stream:
          match event.action:
              case LiveAction.CREATE:
                  print(f"New admin: {event.instance.name}")
              case LiveAction.UPDATE:
                  print(f"Updated: {event.instance.email}")
              case LiveAction.DELETE:
                  print(f"Removed: {event.record_id}")
  ```

  - `ModelChangeEvent[T]` with typed `instance`, `action`, `record_id`, `changed_fields`
  - Full QuerySet filter integration (WHERE clause + parameterized variables)
  - `auto_resubscribe=True` for seamless WebSocket reconnect recovery
  - `diff=True` for receiving only changed fields

- **Change Feed Integration** - HTTP-based CDC for event-driven microservices

  ```python
  async for event in User.objects().changes(since="2026-01-01"):
      await publish_to_queue({
          "type": f"user.{event.action.value.lower()}",
          "data": event.raw,
      })
  ```

  - Stateless, resumable with cursor tracking
  - Configurable `poll_interval` and `batch_size`
  - No WebSocket required (works over HTTP)

- **`post_live_change` signal** - Fires for external database changes (separate from local CRUD signals)

  ```python
  from surreal_orm import post_live_change, LiveAction

  @post_live_change.connect(Player)
  async def on_player_change(sender, instance, action, **kwargs):
      if action == LiveAction.CREATE:
          await ws_manager.broadcast({"type": "player_joined", "name": instance.name})
  ```

- **WebSocket Connection Manager** - `get_ws_client()` creates a lazy WebSocket connection alongside HTTP

---

## What's New in 0.8.0

### Auth Module Fixes + Computed Fields

- **Ephemeral Auth Connections** (Critical) - `signup()`, `signin()`, and `authenticate_token()` no longer corrupt the singleton connection. They use isolated ephemeral connections.

- **Configurable Access Name** - Access name is configurable via `access_name` in `SurrealConfigDict` (was hardcoded to `{table}_auth`)

- **`signup()` Returns Token** - Now returns `tuple[Self, str]` (user + JWT token), matching `signin()`

  ```python
  user, token = await User.signup(email="alice@example.com", password="secret", name="Alice")
  ```

- **`authenticate_token()` Fixed + `validate_token()`** - Fixed token validation with new `validate_token()` lightweight method

  ```python
  result = await User.authenticate_token(token)  # Full: (user, record_id)
  record_id = await User.validate_token(token)    # Lightweight: just record_id
  ```

- **Computed Fields** - Server-side computed fields using SurrealDB's `DEFINE FIELD ... VALUE <expression>`

  ```python
  from surreal_orm import Computed

  class User(BaseSurrealModel):
      first_name: str
      last_name: str
      full_name: Computed[str] = Computed("string::concat(first_name, ' ', last_name)")

  class Order(BaseSurrealModel):
      items: list[dict]
      discount: float = 0.0
      subtotal: Computed[float] = Computed("math::sum(items.*.price * items.*.qty)")
      total: Computed[float] = Computed("subtotal * (1 - discount)")
  ```

  - `Computed[T]` defaults to `None` (server computes the value)
  - Auto-excluded from `save()`/`merge()` via `get_server_fields()`
  - Migration introspector auto-generates `DEFINE FIELD ... VALUE <expression>`

---

## What's New in 0.7.0

### Performance & Developer Experience

- **`merge(refresh=False)`** - Skip the extra SELECT round-trip for fire-and-forget updates

  ```python
  await user.merge(last_seen=SurrealFunc("time::now()"), refresh=False)
  ```

- **`call_function()`** - Invoke custom SurrealDB stored functions from the ORM

  ```python
  result = await SurrealDBConnectionManager.call_function(
      "acquire_game_lock", params={"table_id": tid, "pod_id": pid},
  )
  result = await GameTable.call_function("release_game_lock", params={...})
  ```

- **`extra_vars` on `save()`** - Bind additional query variables for SurrealFunc expressions

  ```python
  await user.save(
      server_values={"password_hash": SurrealFunc("crypto::argon2::generate($password)")},
      extra_vars={"password": raw_password},
  )
  ```

- **`fetch()` / FETCH clause** - Resolve record links inline to prevent N+1 queries

  ```python
  posts = await Post.objects().fetch("author", "tags").exec()
  # Generates: SELECT * FROM posts FETCH author, tags;
  ```

- **`remove_all_relations()` list support** - Remove multiple relation types in one call

  ```python
  await table.remove_all_relations(["has_player", "has_action"], direction="out")
  ```

---

## What's New in 0.6.0

### Query Power, Security & Server-Side Functions

- **Q Objects for Complex Queries** - Django-style composable query expressions with OR/AND/NOT

  ```python
  from surreal_orm import Q

  # OR query
  users = await User.objects().filter(
      Q(name__contains="alice") | Q(email__contains="alice"),
  ).exec()

  # NOT + mixed with regular kwargs
  users = await User.objects().filter(
      ~Q(status="banned"), role="admin",
  ).order_by("-created_at").exec()
  ```

- **Parameterized Filters (Security)** - All filter values are now query variables (`$_fN`)
  - Prevents SQL injection by never embedding values in query strings
  - Existing `$variable` references via `.variables()` still work

- **SurrealFunc for Server-Side Functions** - Embed SurrealQL expressions in save/update

  ```python
  from surreal_orm import SurrealFunc

  await player.save(server_values={"joined_at": SurrealFunc("time::now()")})
  await player.merge(last_ping=SurrealFunc("time::now()"))
  ```

- **`remove_all_relations()`** - Bulk relation deletion with direction support

  ```python
  await table.remove_all_relations("has_player", direction="out")
  await user.remove_all_relations("follows", direction="both")
  ```

- **Django-style `-field` Ordering** - Shorthand for descending order

  ```python
  users = await User.objects().order_by("-created_at").exec()
  ```

- **Bug Fix: `isnull` Lookup** - `filter(field__isnull=True)` now generates `IS NULL` instead of `IS True`

---

## What's New in 0.5.x

### v0.5.9 - Concurrent Safety, Relation Direction & Array Filtering

- **Atomic Array Operations** - Server-side array mutations avoiding read-modify-write conflicts
  - `atomic_append()`, `atomic_remove()`, `atomic_set_add()` class methods
  - Ideal for multi-pod K8s deployments with concurrent workers

  ```python
  # No more transaction conflicts on concurrent array updates:
  await Event.atomic_set_add(event_id, "processed_by", pod_id)
  ```

- **Transaction Conflict Retry** - `retry_on_conflict()` decorator with exponential backoff + jitter
  - `TransactionConflictError` exception for conflict detection

  ```python
  from surreal_orm import retry_on_conflict

  @retry_on_conflict(max_retries=5)
  async def process_event(event_id, pod_id):
      await Event.atomic_set_add(event_id, "processed_by", pod_id)
  ```

- **Relation Direction Control** - `reverse` parameter on `relate()` and `remove_relation()`

  ```python
  # Reverse: users:xyz -> created -> game_tables:abc
  await table.relate("created", creator, reverse=True)
  ```

- **New Query Lookup Operators** - Server-side array filtering
  - `not_contains` (`CONTAINSNOT`), `containsall` (`CONTAINSALL`), `containsany` (`CONTAINSANY`), `not_in` (`NOT IN`)

  ```python
  events = await Event.objects().filter(processed_by__not_contains=pod_id).exec()
  ```

### v0.5.8 - Around Signals (Generator-based middleware)

- **Around Signals** - Generator-based middleware pattern for wrapping DB operations
  - `around_save`, `around_delete`, `around_update`
  - Shared state between before/after phases (local variables)
  - Guaranteed cleanup with `try/finally`

  ```python
  from surreal_orm import around_save

  @around_save.connect(Player)
  async def time_save(sender, instance, created, **kwargs):
      start = time.time()
      yield  # save happens here
      print(f"Saved {instance.id} in {time.time() - start:.3f}s")

  @around_delete.connect(Player)
  async def delete_with_lock(sender, instance, **kwargs):
      lock = await acquire_lock(instance.id)
      try:
          yield  # delete happens while lock is held
      finally:
          await release_lock(lock)  # Always runs
  ```

  **Execution order:** `pre_* → around(before) → DB → around(after) → post_*`

### v0.5.7 - Model Signals

- **Django-style Model Signals** - Event hooks for model lifecycle operations
  - `pre_save`, `post_save` - Before/after save operations
  - `pre_delete`, `post_delete` - Before/after delete operations
  - `pre_update`, `post_update` - Before/after update/merge operations

  ```python
  from surreal_orm import post_save, Player

  @post_save.connect(Player)
  async def on_player_saved(sender, instance, created, **kwargs):
      if instance.is_ready:
          await ws_manager.broadcast({"type": "player_ready", "id": instance.id})
  ```

### v0.5.6 - Relation Query ID Escaping Fix

- **Fixed ID escaping in relation queries** - When using `get_related()`, `RelationQuerySet`, or graph traversal with IDs starting with digits, queries now properly escape the IDs with backticks, preventing parse errors.

### v0.5.5.3 - RecordId Conversion Fix

- **Fixed RecordId objects in foreign key fields** - When using CBOR protocol, fields like `user_id`, `table_id` are now properly converted to `"table:id"` strings instead of raw RecordId objects, preventing Pydantic validation errors.

### v0.5.5.2 - Datetime Regression Fix

- **Fixed datetime_type Pydantic validation error** - v0.5.5.1 introduced a regression where records with datetime fields failed validation, causing `from_db()` to return dicts instead of model instances
- **New `_preprocess_db_record()` method** - Properly handles datetime parsing and RecordId conversion before Pydantic validation

### v0.5.5.1 - Critical Bug Fixes

- **Record ID escaping** - IDs starting with digits (e.g., `7abc123`) now properly escaped with backticks
- **CBOR for HTTP connections** - HTTP connections now default to CBOR protocol, fixing `data:` prefix issues
- **`get()` full ID format** - `QuerySet.get("table:id")` now correctly parses and queries
- **`get_related()` direction="in"** - Fixed to return actual related records instead of empty results
- **`update()` table name** - Fixed bug where custom `table_name` was ignored

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

### Live Models (Real-time at ORM Level)

```python
from surreal_orm import LiveAction

# Subscribe to model changes with full Pydantic instances
async with User.objects().filter(role="admin").live() as stream:
    async for event in stream:
        print(event.action, event.instance.name, event.record_id)

# Change Feed (HTTP, no WebSocket needed)
async for event in Order.objects().changes(since="2026-01-01"):
    print(event.action, event.instance.total)
```

### QuerySet with Django-style Lookups

```python
# Filter with lookups
users = await User.objects().filter(age__gte=18, name__startswith="A").exec()

# Supported lookups
# exact, gt, gte, lt, lte, in, not_in, like, ilike,
# contains, icontains, not_contains, containsall, containsany,
# startswith, istartswith, endswith, iendswith, match, regex, isnull

# Q objects for complex OR/AND/NOT queries
from surreal_orm import Q
users = await User.objects().filter(
    Q(name__contains="alice") | Q(email__contains="alice"),
    role="admin",
).order_by("-created_at").limit(10).exec()
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

| Command             | Description                            |
| ------------------- | -------------------------------------- |
| `makemigrations`    | Generate migration files               |
| `migrate`           | Apply schema migrations                |
| `rollback <target>` | Rollback to migration                  |
| `status`            | Show migration status                  |
| `shell`             | Interactive SurrealQL shell            |
| `inspectdb`         | Generate models from existing database |
| `schemadiff`        | Compare models against live schema     |

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
| [CHANGELOG](CHANGELOG.md)              | Version history          |

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
