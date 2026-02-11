# SurrealDB-ORM Roadmap

> Planning document for future ORM features - Last updated: February 2026

---

## Version History

| Version | Status   | Focus                                                     |
| ------- | -------- | --------------------------------------------------------- |
| 0.1.x   | Released | Basic ORM (Models, QuerySet, CRUD)                        |
| 0.2.x   | Released | Custom SDK, Migrations, JWT Auth, CLI                     |
| 0.3.0   | Released | ORM Transactions + Aggregations                           |
| 0.3.1   | Released | Bulk Operations + Bug Fixes                               |
| 0.4.0   | Released | Relations & Graph Traversal                               |
| 0.5.0   | Released | SDK Real-time: Live Select, Auto-Resubscribe, Typed Calls |
| 0.5.1   | Released | Security Workflows (Dependabot, SurrealDB monitoring)     |
| 0.5.2   | Released | Bug Fixes & FieldType Improvements                        |
| 0.5.3   | Released | ORM Improvements: Upsert, server_fields, merge() fix      |
| 0.5.5.1 | Released | Critical Bug Fixes: ID escaping, CBOR HTTP, get_related   |
| 0.5.7   | Released | Django-style Model Signals                                |
| 0.5.8   | Released | Around Signals (Generator-based middleware)               |
| 0.5.9   | Released | Atomic Array Ops, Relation Direction, Array Filtering     |
| 0.6.0   | Released | Q Objects, Parameterized Filters, SurrealFunc             |
| 0.7.0   | Released | Performance & DX: refresh, call_function, FETCH, extras   |
| 0.8.0   | Released | Auth Module Fixes + Computed Fields                       |
| 0.9.0   | Released | ORM Live Models + Change Feed Integration                 |
| 0.10.0  | Released | Schema Introspection & Multi-DB                           |
| 0.11.0  | Released | Advanced Queries & Caching                                |
| 0.12.0  | Released | Vector Search & Full-Text Search                          |
| 0.13.0  | Released | Events, Geospatial & Materialized Views                   |
| 0.14.0  | Planned  | Testing & Developer Experience                            |

---

## v0.5.5.1 - Critical Bug Fixes (Released)

**Goal:** Fix production-reported bugs affecting record ID handling and protocol issues.

**Status:** Implemented and released.

### Record ID Escaping (Issue #8 - Critical)

SurrealDB interprets unquoted IDs starting with digits as number tokens, causing parse errors:

```python
# Before: Generated invalid SurrealQL
await Player.objects().get("7abc123")
# SELECT * FROM players:7abc123  ← Parse error!

# After: Properly escaped with backticks
await Player.objects().get("7abc123")
# SELECT * FROM players:`7abc123`  ← Works!
```

**Implementation:**

- New `escape_record_id()` utility escapes IDs with backticks when needed
- New `format_thing()` generates correct thing references
- All CRUD methods (`get()`, `save()`, `update()`, `merge()`, `delete()`) use proper escaping

### CBOR Protocol for HTTP (Issue #3 - High)

HTTP connections now support and default to CBOR protocol, fixing `data:` prefix strings:

```python
# CBOR properly handles strings that look like record links
SurrealDBConnectionManager.set_connection(
    url="http://localhost:8000",
    user="root",
    password="root",
    namespace="ns",
    database="db",
    protocol="cbor",  # Default, can also use "json"
)
```

### Full Record ID Format (Issue #1 - High)

`QuerySet.get()` now correctly handles both ID formats:

```python
# Both formats now work
player = await Player.objects().get("abc123")
player = await Player.objects().get("players:abc123")
```

### get_related() Direction Fix (Issue #7 - Medium)

Fixed `get_related()` with `direction="in"` returning empty results:

```python
# Now correctly returns related records for both directions
await user.get_related("follows", direction="out")  # Users this user follows
await user.get_related("follows", direction="in")   # Users following this user
```

---

## v0.3.0 - Transactions & Aggregations (Released)

**Goal:** Integrate SDK transactions into ORM and add Django-style aggregations.

**Status:** Implemented and released.

### 1. Model-level Transactions

Allow atomic operations across multiple model instances:

```python
# Via ConnectionManager
async with SurrealDBConnectionManager.transaction() as tx:
    user = User(name="Alice", balance=1000)
    await user.save(tx=tx)

    order = Order(user_id=user.id, total=100)
    await order.save(tx=tx)

    user.balance -= 100
    await user.save(tx=tx)
    # All-or-nothing: commit on success, rollback on exception

# Via Model class method
async with User.transaction() as tx:
    await user1.save(tx=tx)
    await user2.delete(tx=tx)
```

**Implementation:**

- Add `tx` parameter to `save()`, `delete()`, `merge()`, `update()`
- Add `transaction()` context manager to `SurrealDBConnectionManager`
- Add `Model.transaction()` class method shortcut

### 2. QuerySet Aggregations

Django-style aggregation methods using SDK functions:

```python
# Simple aggregations
count = await User.objects().count()
count = await User.objects().filter(active=True).count()

# Field aggregations
avg_age = await User.objects().avg("age")
total = await Order.objects().filter(status="paid").sum("amount")
min_val = await Product.objects().min("price")
max_val = await Product.objects().max("price")

# With filters
avg = await User.objects().filter(role="premium").avg("lifetime_value")
```

**Implementation:**

- Add `count()`, `sum()`, `avg()`, `min()`, `max()` to QuerySet
- Use SDK's typed functions internally (`fn.count()`, `fn.math.sum()`, etc.)

### 3. GROUP BY Support

Aggregations with grouping:

```python
# Group by single field
stats = await Order.objects().values("status").annotate(
    count=Count(),
    total=Sum("amount"),
)
# Result: [{"status": "paid", "count": 42, "total": 5000}, ...]

# Group by multiple fields
monthly = await Order.objects().values("status", "month").annotate(
    count=Count(),
)
```

**Implementation:**

- Add `values(*fields)` for grouping
- Add `annotate(**aggregations)` for computed values
- Create `Count`, `Sum`, `Avg`, `Min`, `Max` aggregation classes

---

## v0.3.1 - Bulk Operations (Released)

**Goal:** Efficient batch operations with transaction support.

**Status:** Implemented and released. Also includes bug fixes for ORDER BY position and error message typos.

### Bulk Create

```python
users = [User(name=f"User{i}") for i in range(1000)]

# Simple bulk create
created = await User.objects().bulk_create(users)

# Atomic bulk create
created = await User.objects().bulk_create(users, atomic=True)

# With batch size (for large datasets)
created = await User.objects().bulk_create(users, batch_size=100)
```

### Bulk Update

```python
# Update all matching records
updated = await User.objects().filter(
    last_login__lt="2025-01-01"
).bulk_update({"status": "inactive"})

# Atomic update
updated = await User.objects().filter(role="guest").bulk_update(
    {"verified": True},
    atomic=True
)
```

### Bulk Delete

```python
# Delete all matching records
deleted = await User.objects().filter(status="deleted").bulk_delete()

# Atomic delete
deleted = await Order.objects().filter(
    created_at__lt="2024-01-01"
).bulk_delete(atomic=True)
```

---

## v0.4.0 - Relations & Graph Traversal (Released)

**Goal:** Leverage SurrealDB's graph capabilities with declarative relations.

**Status:** Implemented and released.

### Relation Field Types

```python
from surreal_orm import BaseSurrealModel, ForeignKey, ManyToMany, Relation

class User(BaseSurrealModel):
    name: str

    # Graph relations (SurrealDB edges)
    followers = Relation("follows", "User", reverse=True)
    following = Relation("follows", "User")

    # Traditional relations
    profile = ForeignKey("Profile", on_delete="CASCADE")
    groups = ManyToMany("Group", through="membership")

class Post(BaseSurrealModel):
    title: str
    author = ForeignKey("User", related_name="posts")
    tags = ManyToMany("Tag")
```

### Relation Operations

```python
# Add/remove relations
await alice.following.add(bob)
await alice.following.remove(bob)
await alice.following.set([bob, charlie])
await alice.following.clear()

# Query relations
followers = await alice.followers.all()
followers = await alice.followers.filter(active=True)
count = await alice.followers.count()

# Check membership
is_following = await alice.following.contains(bob)
```

### Graph Traversal

```python
# Multi-hop traversal
friends_of_friends = await alice.following.following.all()

# With filters at each level
active_fof = await alice.following.filter(active=True).following.filter(active=True).all()

# Raw graph query
result = await User.objects().graph_query(
    "->follows->User->follows->User WHERE active = true"
)
```

### Prefetch Related

```python
# Eager loading to avoid N+1
users = await User.objects().prefetch_related("followers", "posts").all()

for user in users:
    print(user.followers)  # Already loaded, no extra query
    print(user.posts)      # Already loaded
```

---

## v0.5.0 - SDK Real-time Enhancements (Released)

**Goal:** Enhanced real-time capabilities at the SDK level for game backends and real-time apps.

**Status:** Implemented and released.

### Live Select Stream

Async iterator pattern for consuming Live Query changes:

```python
from surreal_sdk import SurrealDB, LiveAction

async with SurrealDB.ws("ws://localhost:8000", "ns", "db") as db:
    await db.signin("root", "root")

    # Async context manager + async iterator
    async with db.live_select(
        "players",
        where="table_id = $id",
        params={"id": "game_tables:xyz"}
    ) as stream:
        async for change in stream:
            match change.action:
                case LiveAction.CREATE:
                    print(f"Player joined: {change.result['name']}")
                case LiveAction.UPDATE:
                    print(f"Player updated: {change.record_id}")
                case LiveAction.DELETE:
                    print(f"Player left: {change.record_id}")
```

### Auto-Resubscribe

Automatic reconnection after WebSocket disconnect (K8s pod restarts, network issues):

```python
async with db.live_select(
    "players",
    where="table_id = $id",
    params={"id": table_id},
    auto_resubscribe=True,
    on_reconnect=lambda old, new: print(f"Reconnected: {old} -> {new}")
) as stream:
    async for change in stream:
        process_change(change)  # Stream resumes after reconnect
```

### Typed Function Calls

Pydantic/dataclass return type support for SurrealDB functions:

```python
from pydantic import BaseModel

class VoteResult(BaseModel):
    success: bool
    new_count: int
    total_votes: int

result = await db.call(
    "cast_vote",
    params={"user_id": "alice", "table_id": "game:123"},
    return_type=VoteResult
)
# result is VoteResult instance, not dict
```

### LiveChange Dataclass

Rich change notification with:

- `record_id` - Affected record ID
- `action` - CREATE, UPDATE, DELETE
- `result` - Full record after change
- `changed_fields` - List of modified fields (DIFF mode)

---

## v0.5.1 - Security Workflows (Released)

**Goal:** Automated security update management for dependencies and SurrealDB.

**Status:** Implemented and released.

### Dependabot Integration

- Auto-merge for Dependabot PRs after CI passes
- Patch version tagging (x.x.x.1, x.x.x.2, etc.) for security updates
- Automatic GitHub releases for security patches

### SurrealDB Security Monitoring

- Daily checks for new SurrealDB releases
- Automatic integration tests with new DB versions
- Auto-update of DB requirements on security patches
- Issue creation for compatibility failures

---

## v0.5.2 - Bug Fixes & FieldType Improvements (Released)

**Goal:** Critical bug fixes and enhanced migration type system.

**Status:** Implemented and released.

### FieldType Enum Improvements

- Added `NUMBER`, `SET`, `REGEX` types
- `generic(inner_type)` method for parameterized types (`array<string>`, `record<users>`)
- `from_python_type(type)` class method for automatic type mapping
- Comprehensive docstrings with SurrealDB type documentation

### Bug Fixes

- **datetime Serialization** - Custom JSON encoder for RPC requests
- **NULL Values** - `exclude_unset=True` prevents None from overriding DB defaults
- **Fluent API** - `connect()` returns `self` for method chaining
- **Session Cleanup** - WebSocket callback tasks properly tracked and cancelled
- **Parameter Alias** - `username` parameter alias for `user` in ConnectionManager

---

## v0.5.3 - ORM Improvements (Released)

**Goal:** Better save/update behavior with upsert support and server field handling.

**Status:** Implemented and released.

### Upsert Behavior

- `save()` now uses `upsert` for existing records (idempotent, Django-like)
- No more "record already exists" errors on duplicate saves
- SDK `upsert()` method added to connections and transactions

### Server Fields Config

```python
class MyModel(BaseSurrealModel):
    model_config = SurrealConfigDict(
        server_fields=["created_at", "updated_at"],  # Excluded from save/update
    )

    name: str
    created_at: datetime | None = None  # Server-generated
    updated_at: datetime | None = None  # Server-generated
```

### Bug Fixes

- **merge() returns self** - Now returns the updated model instance
- **save() updates self** - Updates original instance instead of returning new object
- **NULL values fix** - `_update_from_db()` preserves `__pydantic_fields_set__`

---

## v0.6.0 - Query Power, Security & Server-Side Functions (Released)

**Goal:** Eliminate raw queries in ORM consumers by adding complex query composition, parameterized filters, and server-side function support.

**Status:** Implemented and released.

### Q Objects for Complex Queries

```python
from surreal_orm import Q

# OR query
users = await User.objects().filter(
    Q(name__contains="alice") | Q(email__contains="alice"),
).exec()

# AND with OR + NOT
users = await User.objects().filter(
    ~Q(status="banned"),
    Q(role="admin") & (Q(age__gte=18) | Q(is_verified=True)),
).exec()

# Mix Q objects with regular kwargs
users = await User.objects().filter(
    Q(id__contains=search) | Q(email__contains=search),
    role="admin",
).order_by("-created_at").limit(10).exec()
```

### Parameterized Filters (Security)

All filter values are now automatically bound as query variables (`$_fN`), preventing injection:

```python
# Generates: WHERE age > $_f0  with {"_f0": 18}
users = await User.objects().filter(age__gt=18).exec()
```

### SurrealFunc for Server-Side Functions

```python
from surreal_orm import SurrealFunc

await player.save(server_values={"joined_at": SurrealFunc("time::now()")})
await player.merge(last_ping=SurrealFunc("time::now()"))
```

### Other Improvements

- **`remove_all_relations()`** - Bulk edge deletion with direction support (`out`, `in`, `both`)
- **Django-style `-field` ordering** - `order_by("-created_at")` shorthand
- **Bug fix: `isnull` lookup** - Now generates `IS NULL` instead of `IS True`

---

## v0.7.0 - Performance & Developer Experience (Released)

**Goal:** Eliminate more raw queries and improve performance for high-frequency operations, based on community feedback from the Games'n'Cards project.

### FR1: `merge(refresh=False)` — Skip Extra SELECT

```python
# Fire-and-forget: only UPDATE, no SELECT round-trip
await user.merge(last_seen=SurrealFunc("time::now()"), refresh=False)
```

### FR2: `call_function()` — Invoke Custom Stored Functions

```python
# On connection manager
result = await SurrealDBConnectionManager.call_function(
    "acquire_game_lock",
    params={"table_id": table_id, "pod_id": pod_id, "ttl": 30},
)

# On any model
result = await GameTable.call_function("release_game_lock", params={"table_id": tid})
```

### FR3: `extra_vars` — Bound Parameters in SurrealFunc

```python
await user.save(
    server_values={"password_hash": SurrealFunc("crypto::argon2::generate($password)")},
    extra_vars={"password": raw_password},
)
```

### FR4: `fetch()` — FETCH Clause for N+1 Prevention

```python
# Resolve record links inline (single query)
posts = await Post.objects().fetch("author", "tags").exec()
# select_related() also maps to FETCH
stats = await PlayerStats.objects().select_related("user").exec()
```

### FR5: `remove_all_relations()` — List Support

```python
# Remove multiple relation types in one call
await table.remove_all_relations(["has_player", "has_action", "has_state"], direction="out")
```

---

## v0.8.0 - Auth Module Fixes (Released)

**Goal:** Fix 4 critical/high bugs in `AuthenticatedUserMixin` so users can drop the official `surrealdb` SDK and use `surrealdb-orm` as their single database dependency for both CRUD and auth.

**Status:** Implemented and released.

### Bug 1 (Critical): Singleton Connection Corruption

`signup()` and `signin()` mutated the root singleton connection, breaking all concurrent ORM operations. Auth operations now use **ephemeral connections** that are created and closed per-call, leaving the root singleton untouched.

```python
# Internal: auth methods now create isolated connections
client = await cls._create_auth_client()  # ephemeral, never the singleton
try:
    response = await client.signup(...)
finally:
    await client.close()
```

### Bug 2 (High): Access Name Not Configurable

Access name was hardcoded as `{table}_auth`. Now configurable via `access_name` in `SurrealConfigDict`:

```python
class User(AuthenticatedUserMixin, BaseSurrealModel):
    model_config = SurrealConfigDict(
        table_type=TableType.USER,
        access_name="account",  # Custom access name (default: "{table}_auth")
    )
```

### Bug 3 (High): signup() Discards JWT Token

`signup()` now returns `tuple[Self, str]` (user instance + JWT token), matching `signin()`:

```python
# Before (v0.7.0): token was discarded
user = await User.signup(email="alice@example.com", password="secret", name="Alice")

# After (v0.8.0): token is returned
user, token = await User.signup(email="alice@example.com", password="secret", name="Alice")
```

### Bug 4 (Medium): authenticate_token() + Missing SDK Method

`authenticate_token()` called `client.authenticate(token)` but the SDK method didn't exist. Fixed by:

1. Adding `authenticate()` to `BaseSurrealConnection` in the SDK
2. Fixing `authenticate_token()` to use ephemeral connections and return `tuple[Self, str] | None`
3. Adding new `validate_token()` lightweight method (returns just the record ID)

```python
# authenticate_token() - returns full user + record_id
result = await User.authenticate_token(token)
if result:
    user, record_id = result
    print(f"Authenticated: {user.email} ({record_id})")

# validate_token() - lightweight, returns just record_id string
record_id = await User.validate_token(token)
if record_id:
    print(f"Token valid for: {record_id}")
```

---

### Computed Fields

Server-side computed fields using SurrealDB's `DEFINE FIELD ... VALUE <expression>` syntax. Computed fields are auto-excluded from writes and auto-populated by SurrealDB.

```python
from surreal_orm import BaseSurrealModel, Computed

class Order(BaseSurrealModel):
    items: list[dict]  # [{"price": 10, "qty": 2}, ...]
    discount: float = 0.0

    # Computed by SurrealDB on write
    subtotal: Computed[float] = Computed("math::sum(items.*.price * items.*.qty)")
    total: Computed[float] = Computed("subtotal * (1 - discount)")
    item_count: Computed[int] = Computed("array::len(items)")

class User(BaseSurrealModel):
    first_name: str
    last_name: str

    # String computation
    full_name: Computed[str] = Computed("string::concat(first_name, ' ', last_name)")
```

**Key behaviors:**

- `Computed[T]` defaults to `None` in Python (server computes the value)
- Auto-excluded from `save()`/`merge()` via `get_server_fields()`
- Migration introspector auto-generates `DEFINE FIELD ... VALUE <expression>`
- Computed fields skipped in signup_fields for USER tables

---

## v0.9.0 - ORM Real-time Features (Released)

**Goal:** Live model synchronization and event-driven architecture at the ORM level.

**Status:** Implemented and released.

### Live Models

ORM-level live query subscriptions that yield typed Pydantic model instances
instead of raw dicts. Uses WebSocket connections (created lazily by the
connection manager).

```python
from surreal_orm import LiveAction

# Async context manager + iterator for model changes
async with User.objects().filter(role="admin").live() as stream:
    async for event in stream:
        match event.action:
            case LiveAction.CREATE:
                print(f"New admin: {event.instance.name}")
            case LiveAction.UPDATE:
                print(f"Admin updated: {event.instance}")
            case LiveAction.DELETE:
                print(f"Admin removed: {event.record_id}")
```

**Features:**

- `ModelChangeEvent` with typed `instance: T`, `action`, `record_id`, `changed_fields`
- `LiveModelStream` wraps SDK `LiveSelectStream` with automatic `Model.from_db()` conversion
- `auto_resubscribe=True` for seamless WebSocket reconnect recovery
- `diff=True` for receiving only changed fields
- `on_reconnect` callback support
- Full QuerySet filter integration (WHERE clause + parameterized variables)

### Change Feed Integration

HTTP-based change data capture for event-driven microservices. Stateless and
resumable with cursor tracking.

```python
# For event-driven microservices
async for event in User.objects().changes(since="2026-01-01"):
    await publish_to_queue({
        "type": f"user.{event.action.value.lower()}",
        "data": event.raw,
    })
```

**Features:**

- `ChangeModelStream` wraps SDK `ChangeFeedStream` with model conversion
- Cursor tracking via `.cursor` property for resumability
- Configurable `poll_interval` and `batch_size`
- Works over HTTP (no WebSocket required)

### post_live_change Signal

New signal for reacting to external database changes detected via Live Queries:

```python
from surreal_orm import post_live_change, LiveAction

@post_live_change.connect(Player)
async def on_player_change(sender, instance, action, record_id, **kwargs):
    if action == LiveAction.CREATE:
        await ws_manager.broadcast({"type": "player_joined", "name": instance.name})
```

### WebSocket Connection Manager

`SurrealDBConnectionManager` now manages an optional WebSocket connection
alongside the existing HTTP connection:

```python
# WebSocket connection is created lazily on first .live() call
# Uses same URL/credentials as HTTP (http → ws auto-conversion)
ws_conn = await SurrealDBConnectionManager.get_ws_client()
```

---

## v0.10.0 - Schema Introspection & Multi-DB (Released)

**Goal:** Generate Python model code from existing databases and route models to different databases.

**Status:** Implemented and released.

### Schema Introspection

Generate ORM model files from an existing SurrealDB database by parsing `INFO FOR DB` / `INFO FOR TABLE` results:

```python
from surreal_orm import generate_models_from_db, schema_diff

# Generate Python model code from existing database
code = await generate_models_from_db(output_path="models.py")

# Compare Python models against live database schema
operations = await schema_diff(models=[User, Order, Product])
for op in operations:
    print(op)  # Migration operations needed to sync
```

**CLI commands:**

```bash
# Generate models from existing database
surreal-orm inspectdb -u http://localhost:8000 -n myns -d mydb -o models.py

# Compare models against live schema
surreal-orm schemadiff -u http://localhost:8000 -n myns -d mydb
```

**Implementation details:**

- `DatabaseIntrospector` — Parses `INFO FOR DB` / `INFO FOR TABLE` results into `SchemaState`
- `ModelCodeGenerator` — Converts `SchemaState` to Python model source code
- `define_parser` module — Parses DEFINE TABLE, FIELD, INDEX, ACCESS statements
- Handles generic types (`array<string>`, `option<int>`, `record<users>`), VALUE/ASSERT expressions, encrypted fields, FLEXIBLE, READONLY

### Multi-Database Support

Named connection registry for routing models to different databases:

```python
from surreal_orm import SurrealDBConnectionManager, ConnectionConfig

# Register named connections
SurrealDBConnectionManager.add_connection("default", url=..., ns=..., db=...)
SurrealDBConnectionManager.add_connection("analytics", url=..., ns=..., db=...)

# Model-level routing via config
class AnalyticsEvent(BaseSurrealModel):
    model_config = SurrealConfigDict(connection="analytics")

# Runtime context manager override (async-safe via contextvars)
async with SurrealDBConnectionManager.using("analytics"):
    events = await AnalyticsEvent.objects().all()

# Registry management
SurrealDBConnectionManager.list_connections()     # ["default", "analytics"]
SurrealDBConnectionManager.get_config("analytics")  # ConnectionConfig(...)
SurrealDBConnectionManager.remove_connection("analytics")
```

**Key design decisions:**

- `ConnectionConfig` — Frozen dataclass for immutable connection settings
- `contextvars.ContextVar` for async-safe `using()` context manager
- Full backward compatibility: `set_connection()` delegates to `add_connection("default", ...)`
- Connection name priority: context var > model config > `"default"`
- All `get_client()` / `get_ws_client()` calls route through the named connection

---

## v0.11.0 - Advanced Queries & Caching (Released)

**Goal:** Subqueries, query result caching, and `Prefetch` objects for complex data loading.

- [x] `Subquery` class for inline sub-SELECT in filters and annotations
- [x] `QueryCache` with TTL, FIFO eviction, and signal-based auto-invalidation
- [x] `QuerySet.cache(ttl=N)` opt-in caching method
- [x] `Prefetch` objects for fine-grained `prefetch_related()` control
- [x] `_execute_prefetch()` batch-fetching after main query

### Subqueries

Nest QuerySets inside filters for server-side subquery evaluation:

```python
from surreal_orm import Subquery

# Users who placed orders above $100
top_ids = Order.objects().filter(total__gte=100).select("user_id")
users = await User.objects().filter(id__in=Subquery(top_ids)).exec()
# SELECT * FROM users WHERE id IN (SELECT VALUE user_id FROM orders WHERE total >= $_f0);
```

### Query Cache

Transparent caching layer for frequently executed read queries:

```python
from surreal_orm import QueryCache

# Configure cache at startup
QueryCache.configure(default_ttl=120, max_size=500)

# Cached queries
users = await User.objects().filter(role="admin").cache(ttl=30).exec()

# Automatic invalidation on save/update/delete via signals
await user.save()  # Clears all cached entries for this table

# Manual invalidation
QueryCache.invalidate(User)
QueryCache.clear()
```

### Prefetch Objects

Fine-grained control over related data prefetching:

```python
from surreal_orm import Prefetch

# Prefetch with filtering and custom attribute name
users = await User.objects().prefetch_related(
    Prefetch("wrote", queryset=Post.objects().filter(published=True), to_attr="published_posts"),
).exec()
```

---

## v0.12.0 - Vector Search & Full-Text Search (Released)

**Goal:** First-class support for AI/RAG pipelines and search-heavy applications by leveraging SurrealDB's native vector similarity and full-text search capabilities.

**Status:** Implemented and released.

### 1. Vector Fields & HNSW Indexes

New `VectorField` type and HNSW index support for embedding storage and similarity search:

```python
from surreal_orm import BaseSurrealModel, SurrealConfigDict
from surreal_orm.fields import VectorField

class Document(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="documents")

    title: str
    content: str
    embedding: VectorField[1536]
```

**Migration support:**

```python
from surreal_orm.migrations.operations import CreateIndex

# HNSW vector index
CreateIndex(
    table="documents",
    name="idx_embedding",
    fields=["embedding"],
    hnsw=True,
    dimension=1536,
    dist="COSINE",         # COSINE | EUCLIDEAN | MANHATTAN | MINKOWSKI | CHEBYSHEV | HAMMING
    vector_type="F32",     # F32 | F64 | I16 | I32 | I64
    efc=150,
    m=12,
)
```

Generates:

```sql
DEFINE INDEX idx_embedding ON documents FIELDS embedding
    HNSW DIMENSION 1536 DIST COSINE TYPE F32 EFC 150 M 12;
```

### 2. Vector Similarity Search (`similar_to()`)

New QuerySet method for KNN-based similarity search using the `<|N|>` operator:

```python
# Basic similarity search (top 10 nearest neighbours)
results = await Document.objects().similar_to(
    "embedding",
    query_embedding,
    limit=10,
).exec()

# With search effort tuning (ef parameter)
results = await Document.objects().similar_to(
    "embedding",
    query_embedding,
    limit=10,
    ef=40,
).exec()
# SELECT *, vector::distance::knn() AS _distance
# FROM documents WHERE embedding <|10, 40|> $_vec
# ORDER BY _distance;

# Combined with standard filters (pre-filter)
results = await Document.objects().similar_to(
    "embedding",
    query_embedding,
    limit=5,
).filter(category="science").exec()
# WHERE category = $_f0 AND embedding <|5|> $_vec

# Access distance on results
for doc in results:
    print(f"{doc.title}: distance={doc._knn_distance}")
```

### 3. Vector Functions

Expose SurrealDB's 22+ vector functions via QuerySet annotations and `SurrealFunc`:

```python
from surreal_orm import SurrealFunc

# Distance calculations in annotations
docs = await Document.objects().annotate(
    cosine_sim=SurrealFunc("vector::similarity::cosine(embedding, $query_vec)"),
).variables(query_vec=my_vector).order_by("-cosine_sim").limit(10).exec()

# Available vector functions:
# vector::distance::euclidean, manhattan, chebyshev, hamming, minkowski, knn
# vector::similarity::cosine, jaccard, pearson
# vector::add, subtract, multiply, divide, scale
# vector::dot, cross, magnitude, normalize, angle, project
```

### 4. Full-Text Search Analyzers

Define custom text analyzers for full-text search:

```python
from surreal_orm.migrations.operations import DefineAnalyzer

# In migrations
DefineAnalyzer(
    name="english",
    tokenizers=["class", "blank"],
    filters=["lowercase", "ascii", "snowball(english)"],
)

DefineAnalyzer(
    name="autocomplete",
    tokenizers=["class"],
    filters=["lowercase", "edgengram(2, 10)"],
)
```

Generates:

```sql
DEFINE ANALYZER english TOKENIZERS class, blank FILTERS lowercase, ascii, snowball(english);
DEFINE ANALYZER autocomplete TOKENIZERS class FILTERS lowercase, edgengram(2, 10);
```

**Tokenizers:** `blank`, `camel`, `class`, `punct`
**Filters:** `ascii`, `lowercase`, `uppercase`, `edgengram(min, max)`, `ngram(min, max)`, `snowball(lang)`, `mapper(path)`

### 5. Full-Text Search Indexes (BM25 + Highlights)

FULLTEXT index support with BM25 scoring and highlights:

```python
from surreal_orm.migrations.operations import CreateIndex

CreateIndex(
    table="articles",
    name="idx_content_fts",
    fields=["content"],
    search_analyzer="english",
    bm25=(1.2, 0.75),   # (k1, b) parameters
    highlights=True,
)
```

Generates:

```sql
DEFINE INDEX idx_content_fts ON articles FIELDS content
    SEARCH ANALYZER english BM25(1.2, 0.75) HIGHLIGHTS;
```

### 6. Full-Text Search QuerySet (`search()`)

New QuerySet method using the match-reference operator `@N@` with scoring and highlighting:

```python
from surreal_orm.search import SearchScore, SearchHighlight

# Simple full-text search
results = await Article.objects().search(content="machine learning").exec()
# SELECT * FROM articles WHERE content @0@ $_s0;

# Multi-field search with scoring and highlights
results = await Article.objects().search(
    title="quantum computing",       # title @0@ 'quantum computing'
    body="entanglement theory",      # body @1@ 'entanglement theory'
).annotate(
    title_score=SearchScore(0),
    body_score=SearchScore(1),
    title_hl=SearchHighlight("<b>", "</b>", 0),
    body_hl=SearchHighlight("<b>", "</b>", 1),
).order_by("-title_score").exec()
# SELECT *,
#   search::score(0) AS title_score,
#   search::score(1) AS body_score,
#   search::highlight('<b>', '</b>', 0) AS title_hl,
#   search::highlight('<b>', '</b>', 1) AS body_hl
# FROM articles
# WHERE title @0@ 'quantum computing' AND body @1@ 'entanglement theory'
# ORDER BY title_score DESC;

# Access results
for article in results:
    print(f"{article.title_hl} — title_score: {article.title_score}")
```

### 7. Hybrid Search (Vector + Full-Text)

Combine vector similarity with full-text search for RAG pipelines:

```python
# Reciprocal Rank Fusion
results = await Document.objects().hybrid_search(
    vector_field="embedding",
    vector=query_embedding,
    vector_limit=20,
    text_field="content",
    text_query="machine learning transformers",
    text_limit=20,
    rrf_k=60,
)
```

### 8. Advanced Index Operations

Expand `CreateIndex` and `IndexState` to support all SurrealDB index variants:

```python
# Count index (v3.0.0+)
CreateIndex(table="metrics", name="idx_count", count=True)

# Non-blocking index creation
CreateIndex(table="documents", name="idx_embed", ..., concurrently=True)

# Deferred consistency (high-volume ingestion)
CreateIndex(table="logs", name="idx_logs", ..., defer=True)
```

### 9. Schema Introspection Updates

- `DatabaseIntrospector` parses HNSW and FULLTEXT index parameters
- `ModelCodeGenerator` generates `VectorField` annotations
- `define_parser` handles DEFINE ANALYZER statements
- `schema_diff()` detects index parameter changes (dimension, distance, BM25)
- CLI `inspectdb` generates vector field annotations from existing DB

---

## v0.13.0 - Events, Geospatial & Materialized Views (Released)

**Goal:** Server-side triggers, location-based queries, and auto-maintained aggregate tables.

### DEFINE EVENT (Server-Side Triggers)

Declarative server-side event triggers on model lifecycle:

```python
from surreal_orm.events import Event

class User(BaseSurrealModel):
    email: str
    name: str

    class Meta:
        events = [
            Event(
                "email_audit",
                when="$before.email != $after.email",
                then="""CREATE audit_log SET
                    table = 'user', record = $value.id,
                    action = $event, changed_at = time::now()""",
            ),
            Event(
                "notify_webhook",
                when="$event = 'CREATE'",
                then="http::post('https://hooks.example.com', { id: $after.id })",
                async_mode=True,
                retry=3,
            ),
        ]
```

Migration operations: `DefineEvent`, `RemoveEvent`

### Geospatial Fields

Typed geometry fields and distance-based queries:

```python
from surreal_orm.fields import PointField, PolygonField

class Store(BaseSurrealModel):
    name: str
    location: PointField          # geometry<point>
    delivery_area: PolygonField   # geometry<polygon>

# Distance-based queries
nearby = await Store.objects().filter(
    location__distance_lt=((-73.98, 40.74), 5000),  # Within 5km
).exec()

# Geo annotations
stores = await Store.objects().annotate(
    dist=GeoFunc.distance("location", (-73.98, 40.74)),
).order_by("dist").limit(10).exec()
```

### Materialized Views

Read-only models backed by `DEFINE TABLE ... AS SELECT`:

```python
from surreal_orm import MaterializedView

class OrderStats(MaterializedView):
    source_query = """
        SELECT status, count() AS total, math::sum(amount) AS revenue
        FROM orders GROUP BY status
    """
    status: str
    total: int
    revenue: float

# Auto-maintained by SurrealDB — read-only queries
stats = await OrderStats.objects().all()
```

### TYPE RELATION Enforcement

Enforce graph edge constraints in migrations:

```python
class Likes(BaseSurrealModel):
    model_config = SurrealConfigDict(
        table_type=TableType.RELATION,
        relation_in=["person"],
        relation_out=["blog_post", "book"],
        enforced=True,
    )
```

---

## v0.14.0 - Testing & Developer Experience (Planned)

**Goal:** First-class testing utilities and developer tooling.

### Test Fixtures

Declarative fixtures for integration tests:

```python
from surreal_orm.testing import SurrealFixture, fixture

@fixture
class UserFixtures(SurrealFixture):
    alice = User(name="Alice", email="alice@example.com", role="admin")
    bob = User(name="Bob", email="bob@example.com", role="player")

# In pytest
@pytest.fixture
async def users(surreal_db):
    async with UserFixtures.load() as fixtures:
        yield fixtures

async def test_admin_query(users):
    admins = await User.objects().filter(role="admin").exec()
    assert len(admins) == 1
    assert admins[0].name == "Alice"
```

### Model Factories

Factory Boy-style model factories for generating test data:

```python
from surreal_orm.testing import ModelFactory, Faker

class UserFactory(ModelFactory):
    class Meta:
        model = User

    name = Faker("name")
    email = Faker("email")
    age = Faker("random_int", min=18, max=80)
    role = "player"

# Generate test data
user = await UserFactory.create()
users = await UserFactory.create_batch(50)
admin = await UserFactory.create(role="admin")
```

### Debug Toolbar

Query inspection and performance profiling:

```python
from surreal_orm.debug import QueryLogger

# Log all queries with timing
async with QueryLogger() as logger:
    users = await User.objects().filter(role="admin").exec()
    orders = await Order.objects().filter(user_id=users[0].id).exec()

for query in logger.queries:
    print(f"{query.sql} — {query.duration_ms:.1f}ms")

print(f"Total: {logger.total_queries} queries, {logger.total_ms:.1f}ms")
```

---

## Future Versions (Planned)

| Version | Focus            | Key Features                                                |
| ------- | ---------------- | ----------------------------------------------------------- |
| 0.15.0  | Graph Power      | Recursive traversal, shortest path, path collection         |
| 0.16.0  | ML & Data        | SurrealML inference, JSONL import/export, DB dump/restore   |
| 0.17.0  | Advanced Queries | Nested path queries (`[WHERE ...]`, `.?`), destructuring    |

---

## Implementation Priority

| Feature                      | Version | Priority | Status | Dependencies     |
| ---------------------------- | ------- | -------- | ------ | ---------------- |
| Model Transactions           | 0.3.0   | Critical | Done   | SDK transactions |
| Aggregations (count/sum/avg) | 0.3.0   | High     | Done   | SDK functions    |
| GROUP BY                     | 0.3.0   | High     | Done   | Aggregations     |
| Bulk Operations              | 0.3.1   | Medium   | Done   | Transactions     |
| Relations (ForeignKey)       | 0.4.0   | High     | Done   | -                |
| Graph Traversal              | 0.4.0   | High     | Done   | Relations        |
| Live Select Stream           | 0.5.0   | High     | Done   | SDK WebSocket    |
| Auto-Resubscribe             | 0.5.0   | High     | Done   | Live Select      |
| Typed Function Calls         | 0.5.0   | Medium   | Done   | SDK functions    |
| Security Workflows           | 0.5.1   | High     | Done   | -                |
| FieldType Improvements       | 0.5.2   | Medium   | Done   | -                |
| Upsert & server_fields       | 0.5.3   | High     | Done   | -                |
| Record ID Escaping           | 0.5.5.1 | Critical | Done   | -                |
| CBOR HTTP Protocol           | 0.5.5.1 | High     | Done   | SDK CBOR         |
| get_related() direction fix  | 0.5.5.1 | Medium   | Done   | Relations        |
| Model Signals                | 0.5.7   | High     | Done   | -                |
| Around Signals               | 0.5.8   | Medium   | Done   | Model Signals    |
| Atomic Array Ops             | 0.5.9   | High     | Done   | -                |
| Relation Direction Control   | 0.5.9   | Medium   | Done   | Relations        |
| Array Filtering Operators    | 0.5.9   | Medium   | Done   | -                |
| Transaction Conflict Retry   | 0.5.9   | High     | Done   | -                |
| Q Objects (OR/AND/NOT)       | 0.6.0   | High     | Done   | -                |
| Parameterized Filters        | 0.6.0   | High     | Done   | -                |
| SurrealFunc                  | 0.6.0   | High     | Done   | -                |
| remove_all_relations()       | 0.6.0   | Medium   | Done   | Relations        |
| `-field` ordering            | 0.6.0   | Low      | Done   | -                |
| isnull bug fix               | 0.6.0   | Medium   | Done   | -                |
| merge(refresh=False)         | 0.7.0   | High     | Done   | -                |
| call_function()              | 0.7.0   | High     | Done   | SDK call()       |
| extra_vars for SurrealFunc   | 0.7.0   | Medium   | Done   | SurrealFunc      |
| FETCH clause (N+1 fix)       | 0.7.0   | Medium   | Done   | -                |
| remove_all_relations() list  | 0.7.0   | Low      | Done   | Relations        |
| Auth: Ephemeral connections  | 0.8.0   | Critical | Done   | -                |
| Auth: Configurable access    | 0.8.0   | High     | Done   | -                |
| Auth: signup returns token   | 0.8.0   | High     | Done   | -                |
| Auth: authenticate/validate  | 0.8.0   | Medium   | Done   | SDK authenticate |
| SDK: authenticate() method   | 0.8.0   | Medium   | Done   | -                |
| Computed Fields              | 0.8.0   | Medium   | Done   | SDK functions    |
| ORM Live Models              | 0.9.0   | Medium   | Done   | SDK live queries |
| Change Feed ORM Integration  | 0.9.0   | Medium   | Done   | SDK change feeds |
| post_live_change signal      | 0.9.0   | Low      | Done   | Live Models      |
| WebSocket ConnectionManager  | 0.9.0   | Medium   | Done   | SDK WebSocket    |
| Schema Introspection         | 0.10.0  | High     | Done   | Migrations       |
| Multi-Database Routing       | 0.10.0  | High     | Done   | ConnectionManager|
| Subqueries                   | 0.11.0  | High     | Done   | QuerySet         |
| Query Cache                  | 0.11.0  | Medium   | Done   | -                |
| Prefetch Objects             | 0.11.0  | Medium   | Done   | Relations        |
| VectorField + HNSW Index     | 0.12.0  | Critical | Done   | Migrations       |
| similar_to() KNN search      | 0.12.0  | Critical | Done   | VectorField      |
| Full-Text Analyzer + Index   | 0.12.0  | High     | Done   | Migrations       |
| search() QuerySet method     | 0.12.0  | High     | Done   | FTS Index        |
| Hybrid Search (Vector + FTS) | 0.12.0  | Medium   | Done   | Vector + FTS     |
| Advanced Index Operations    | 0.12.0  | High     | Done   | Migrations       |
| DEFINE EVENT (triggers)      | 0.13.0  | High     | Done   | Migrations       |
| Geospatial Fields            | 0.13.0  | Medium   | Done   | -                |
| Materialized Views           | 0.13.0  | Medium   | Done   | -                |
| TYPE RELATION enforcement    | 0.13.0  | Low      | Done   | Relations        |
| Test Fixtures                | 0.14.0  | High     | -      | -                |
| Model Factories              | 0.14.0  | High     | -      | -                |
| Debug Toolbar / QueryLogger  | 0.14.0  | Medium   | -      | -                |

---

## Contributing

Want to help implement these features? Check out:

1. [Contributing Guide](../CONTRIBUTING.md)
2. [GitHub Issues](https://github.com/EulogySnowfall/SurrealDB-ORM/issues)
3. [Discussion Board](https://github.com/EulogySnowfall/SurrealDB-ORM/discussions)

---

_This roadmap is subject to change based on community feedback and SurrealDB updates._
