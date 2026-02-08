# SurrealDB-ORM Roadmap

> Planning document for future ORM features - Last updated: February 2026

---

## Version History

| Version     | Status       | Focus                                                       |
| ----------- | ------------ | ----------------------------------------------------------- |
| 0.1.x       | Released     | Basic ORM (Models, QuerySet, CRUD)                          |
| 0.2.x       | Released     | Custom SDK, Migrations, JWT Auth, CLI                       |
| 0.3.0       | Released     | ORM Transactions + Aggregations                             |
| 0.3.1       | Released     | Bulk Operations + Bug Fixes                                 |
| 0.4.0       | Released     | Relations & Graph Traversal                                 |
| 0.5.0       | Released     | SDK Real-time: Live Select, Auto-Resubscribe, Typed Calls   |
| 0.5.1       | Released     | Security Workflows (Dependabot, SurrealDB monitoring)       |
| 0.5.2       | Released     | Bug Fixes & FieldType Improvements                          |
| **0.5.3**   | **Released** | **ORM Improvements: Upsert, server_fields, merge() fix**    |
| **0.5.5.1** | **Released** | **Critical Bug Fixes: ID escaping, CBOR HTTP, get_related** |
| **0.5.7**   | **Released** | **Django-style Model Signals**                              |
| **0.5.8**   | **Released** | **Around Signals (Generator-based middleware)**             |
| **0.5.9**   | **Released** | **Atomic Array Ops, Relation Direction, Array Filtering**   |
| **0.6.0**   | **Released** | **Q Objects, Parameterized Filters, SurrealFunc**           |
| **0.7.0**   | **Released** | **Performance & DX: refresh, call_function, FETCH, extras** |
| **0.8.0**   | **Released** | **Auth Module Fixes + Computed Fields**                     |
| 0.9.x       | Planned      | ORM Live Models                                             |

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

## v0.9.0 - ORM Real-time Features (Planned)

**Goal:** Live model synchronization and event-driven architecture at the ORM level.

### Live Models

```python
from surreal_orm import LiveAction

# Async iterator for model changes
async for event in User.objects().filter(role="admin").live():
    if event.action == LiveAction.CREATE:
        print(f"New admin: {event.instance.name}")
    elif event.action == LiveAction.UPDATE:
        print(f"Admin updated: {event.instance}")
    elif event.action == LiveAction.DELETE:
        print(f"Admin removed: {event.instance.id}")
```

### Change Feed Integration

```python
# For event-driven microservices
async for change in User.objects().changes(since="2026-01-01"):
    event = {
        "type": f"user.{change.action.lower()}",
        "data": change.record,
        "timestamp": change.timestamp,
    }
    await publish_to_queue(event)
```

---

## v0.10.0 - Advanced Features (Future)

### Schema Introspection

```python
# Generate models from existing database
await generate_models_from_db(
    output_dir="models/",
    tables=["users", "orders", "products"],
)
```

### Multi-Database Support

```python
class User(BaseSurrealModel):
    class Meta:
        database = "users_db"

class Order(BaseSurrealModel):
    class Meta:
        database = "orders_db"

# Or runtime switching
async with SurrealDBConnectionManager.using("analytics_db"):
    stats = await AnalyticsEvent.objects().all()
```

### Query Optimization

```python
# Explain query plan
plan = await User.objects().filter(age__gt=18).explain()
print(plan.indexes_used)
print(plan.estimated_cost)

# Query hints
users = await User.objects().filter(age__gt=18).using_index("idx_age").all()
```

---

## Implementation Priority

| Feature                      | Version | Priority | Status  | Dependencies     |
| ---------------------------- | ------- | -------- | ------- | ---------------- |
| Model Transactions           | 0.3.0   | Critical | Done    | SDK transactions |
| Aggregations (count/sum/avg) | 0.3.0   | High     | Done    | SDK functions    |
| GROUP BY                     | 0.3.0   | High     | Done    | Aggregations     |
| Bulk Operations              | 0.3.1   | Medium   | Done    | Transactions     |
| Relations (ForeignKey)       | 0.4.0   | High     | Done    | -                |
| Graph Traversal              | 0.4.0   | High     | Done    | Relations        |
| Live Select Stream           | 0.5.0   | High     | Done    | SDK WebSocket    |
| Auto-Resubscribe             | 0.5.0   | High     | Done    | Live Select      |
| Typed Function Calls         | 0.5.0   | Medium   | Done    | SDK functions    |
| Security Workflows           | 0.5.1   | High     | Done    | -                |
| FieldType Improvements       | 0.5.2   | Medium   | Done    | -                |
| Upsert & server_fields       | 0.5.3   | High     | Done    | -                |
| Record ID Escaping           | 0.5.5.1 | Critical | Done    | -                |
| CBOR HTTP Protocol           | 0.5.5.1 | High     | Done    | SDK CBOR         |
| get_related() direction fix  | 0.5.5.1 | Medium   | Done    | Relations        |
| Model Signals                | 0.5.7   | High     | Done    | -                |
| Around Signals               | 0.5.8   | Medium   | Done    | Model Signals    |
| Atomic Array Ops             | 0.5.9   | High     | Done    | -                |
| Relation Direction Control   | 0.5.9   | Medium   | Done    | Relations        |
| Array Filtering Operators    | 0.5.9   | Medium   | Done    | -                |
| Transaction Conflict Retry   | 0.5.9   | High     | Done    | -                |
| Q Objects (OR/AND/NOT)       | 0.6.0   | High     | Done    | -                |
| Parameterized Filters        | 0.6.0   | High     | Done    | -                |
| SurrealFunc                  | 0.6.0   | High     | Done    | -                |
| remove_all_relations()       | 0.6.0   | Medium   | Done    | Relations        |
| `-field` ordering            | 0.6.0   | Low      | Done    | -                |
| isnull bug fix               | 0.6.0   | Medium   | Done    | -                |
| merge(refresh=False)         | 0.7.0   | High     | Done    | -                |
| call_function()              | 0.7.0   | High     | Done    | SDK call()       |
| extra_vars for SurrealFunc   | 0.7.0   | Medium   | Done    | SurrealFunc      |
| FETCH clause (N+1 fix)       | 0.7.0   | Medium   | Done    | -                |
| remove_all_relations() list  | 0.7.0   | Low      | Done    | Relations        |
| Auth: Ephemeral connections  | 0.8.0   | Critical | Done    | -                |
| Auth: Configurable access    | 0.8.0   | High     | Done    | -                |
| Auth: signup returns token   | 0.8.0   | High     | Done    | -                |
| Auth: authenticate/validate  | 0.8.0   | Medium   | Done    | SDK authenticate |
| SDK: authenticate() method   | 0.8.0   | Medium   | Done    | -                |
| Computed Fields              | 0.8.0   | Medium   | Done    | SDK functions    |
| ORM Live Models              | 0.9.x   | Medium   | Planned | SDK live queries |

---

## Contributing

Want to help implement these features? Check out:

1. [Contributing Guide](../CONTRIBUTING.md)
2. [GitHub Issues](https://github.com/EulogySnowfall/SurrealDB-ORM/issues)
3. [Discussion Board](https://github.com/EulogySnowfall/SurrealDB-ORM/discussions)

---

_This roadmap is subject to change based on community feedback and SurrealDB updates._
