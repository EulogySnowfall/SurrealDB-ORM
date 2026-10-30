# SurrealDB ORM Documentation

A Django-style ORM for SurrealDB with async support and Pydantic validation.

## Table of Contents

- [Installation](#installation)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Defining Models](#defining-models)
- [CRUD Operations](#crud-operations)
- [QuerySet API](#queryset-api)
- [Filtering](#filtering)
- [Ordering, Limit & Offset](#ordering-limit--offset)
- [Transactions](#transactions)
- [Aggregations](#aggregations)
- [Bulk Operations](#bulk-operations)
- [Relations & Graph Traversal](#relations--graph-traversal)
- [Live Models (Real-time)](#live-models-real-time)
- [Change Feed Integration](#change-feed-integration)
- [Custom Queries](#custom-queries)
- [Error Handling](#error-handling)

---

## Installation

```bash
pip install surrealdb-orm
```

---

## Quick Start

```python
from pydantic import Field
from surreal_orm import BaseSurrealModel, SurrealDBConnectionManager

# 1. Configure connection
SurrealDBConnectionManager.set_connection(
    url="http://localhost:8000",
    user="root",
    password="root",
    namespace="myns",
    database="mydb"
)

# 2. Define a model
class User(BaseSurrealModel):
    id: str | None = None
    name: str = Field(..., max_length=100)
    email: str = Field(..., max_length=255)
    age: int = Field(..., ge=0, le=150)

# 3. Use the model
async def main():
    # Create
    user = User(name="Alice", email="alice@example.com", age=30)
    await user.save()
    print(f"Created user with ID: {user.id}")

    # Read
    alice = await User.objects().get(user.id)
    print(f"Found: {alice.name}")

    # Update
    alice.age = 31
    await alice.update()

    # Delete
    await alice.delete()
```

---

## Configuration

### Setting Up Connection

```python
from surreal_orm import SurrealDBConnectionManager

# Set connection parameters
SurrealDBConnectionManager.set_connection(
    url="http://localhost:8000",  # or https:// for secure
    user="root",
    password="root",
    namespace="myns",
    database="mydb"
)
```

### Using Context Manager

```python
async with SurrealDBConnectionManager() as client:
    # client is the underlying HTTPConnection
    result = await client.query("INFO FOR DB")
```

### Connection Management

```python
# Check if connected
if SurrealDBConnectionManager.is_connected():
    print("Connected!")

# Get current settings
settings = SurrealDBConnectionManager.get_connection_kwargs()

# Reconnect: the new client is signed in and swapped in first; requests still
# running on the old client, and transactions still open on it, finish before
# it is closed (waits at most 30 s). Cancelling reconnect(), e.g. with
# asyncio.wait_for(), does not cut them off: the last of them closes the old
# client. Call get_client() again after an await rather than keeping a client:
# a kept one is closed by the next reconnect().
# Not needed to renew an expired token: the HTTP client signs in again on a 401.
await SurrealDBConnectionManager.reconnect()

# Close connection
await SurrealDBConnectionManager.close_connection()

# Change settings at runtime
await SurrealDBConnectionManager.set_namespace("other_ns", reconnect=True)
await SurrealDBConnectionManager.set_database("other_db", reconnect=True)
```

---

## Defining Models

### Basic Model

```python
from pydantic import Field
from surreal_orm import BaseSurrealModel

class User(BaseSurrealModel):
    id: str | None = None  # Auto-generated if not provided
    name: str = Field(..., max_length=100)
    email: str
    age: int = Field(default=0, ge=0)
```

### With Custom Primary Key

```python
from surreal_orm import BaseSurrealModel, SurrealConfigDict

class Product(BaseSurrealModel):
    model_config = SurrealConfigDict(primary_key="sku")

    sku: str  # This is the primary key
    name: str
    price: float
```

### With Pydantic Validators

```python
from pydantic import Field, field_validator
from surreal_orm import BaseSurrealModel

class User(BaseSurrealModel):
    id: str | None = None
    email: str
    username: str = Field(..., min_length=3, max_length=50)

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        if "@" not in v:
            raise ValueError("Invalid email format")
        return v.lower()
```

### Custom Table Name

```python
class User(BaseSurrealModel):
    id: str | None = None
    name: str

    @classmethod
    def get_table_name(cls) -> str:
        return "app_users"  # Table name in SurrealDB
```

---

## CRUD Operations

### Create (Save)

```python
# Create with auto-generated ID
user = User(name="Alice", email="alice@example.com")
await user.save()
print(f"ID: {user.id}")  # Auto-generated

# Create with specific ID
user = User(id="alice", name="Alice", email="alice@example.com")
await user.save()
```

### Read (Get)

```python
# Get by ID
user = await User.objects().get("alice")

# Get with filters
user = await User.objects().filter(email="alice@example.com").get()
```

### Update

```python
# Modify and update all fields
user = await User.objects().get("alice")
user.name = "Alice Smith"
user.age = 31
await user.update()
```

### Merge (Partial Update)

```python
# Update only specific fields
user = await User.objects().get("alice")
await user.merge(age=32, status="active")
```

### Refresh

```python
# Reload data from database
user = await User.objects().get("alice")
# ... some time passes, data might have changed ...
await user.refresh()
print(f"Current age: {user.age}")
```

### Delete

```python
user = await User.objects().get("alice")
await user.delete()
```

---

## QuerySet API

### Get All Records

```python
users = await User.objects().all()
for user in users:
    print(user.name)
```

### Get First Match

```python
user = await User.objects().filter(status="active").first()
```

### Execute Query

```python
# Returns list of model instances
users = await User.objects().filter(age__gte=18).exec()
```

### Select Specific Fields

```python
# Returns list of dicts (not model instances)
data = await User.objects().select("name", "email").exec()
for item in data:
    print(item["name"])
```

### Delete Table

```python
# ⚠️ Deletes ALL records in the table!
await User.objects().delete_table()
```

---

## Filtering

### Basic Filters

```python
# Exact match
users = await User.objects().filter(name="Alice").exec()

# Multiple conditions (AND)
users = await User.objects().filter(status="active", age=30).exec()
```

### Lookup Operators

| Lookup         | Operator      | Example                                         |
| -------------- | ------------- | ----------------------------------------------- |
| `exact`        | `=`           | `filter(name="Alice")`                          |
| `gt`           | `>`           | `filter(age__gt=18)`                            |
| `gte`          | `>=`          | `filter(age__gte=18)`                           |
| `lt`           | `<`           | `filter(age__lt=65)`                            |
| `lte`          | `<=`          | `filter(age__lte=65)`                           |
| `in`           | `IN`          | `filter(status__in=["active", "pending"])`      |
| `not_in`       | `NOT IN`      | `filter(status__not_in=["banned", "deleted"])`  |
| `contains`     | `CONTAINS`    | `filter(tags__contains="python")`               |
| `not_contains` | `CONTAINSNOT` | `filter(tags__not_contains="deprecated")`       |
| `containsall`  | `CONTAINSALL` | `filter(tags__containsall=["python", "async"])` |
| `containsany`  | `CONTAINSANY` | `filter(tags__containsany=["python", "rust"])`  |
| `like`         | `LIKE`        | `filter(name__like="Ali%")`                     |
| `ilike`        | `ILIKE`       | `filter(name__ilike="ali%")` (case-insensitive) |
| `startswith`   | `STARTSWITH`  | `filter(name__startswith="Ali")`                |
| `endswith`     | `ENDSWITH`    | `filter(name__endswith="ce")`                   |
| `isnull`       | `IS NULL`     | `filter(deleted_at__isnull=True)`               |
| `regex`        | `~`           | `filter(email__regex=r".*@gmail\.com")`         |

### Examples

```python
# Greater than
adults = await User.objects().filter(age__gt=18).exec()

# Less than or equal
seniors = await User.objects().filter(age__lte=65).exec()

# In list
active_users = await User.objects().filter(status__in=["active", "verified"]).exec()

# String contains
python_devs = await User.objects().filter(skills__contains="python").exec()

# Pattern matching
gmail_users = await User.objects().filter(email__like="%@gmail.com").exec()

# Case-insensitive
alice_variants = await User.objects().filter(name__ilike="alice").exec()
```

---

## Ordering, Limit & Offset

### Q Objects (OR/AND/NOT Queries)

```python
from surreal_orm import Q

# OR query
users = await User.objects().filter(
    Q(name__contains="alice") | Q(email__contains="alice"),
).exec()

# AND with OR
users = await User.objects().filter(
    Q(role="admin") & (Q(age__gte=18) | Q(is_verified=True)),
).exec()

# NOT
users = await User.objects().filter(~Q(status="banned")).exec()

# Q objects + regular kwargs (mixed)
users = await User.objects().filter(
    Q(id__contains=search) | Q(email__contains=search),
    role="admin",
).order_by("-created_at").limit(10).exec()
```

### Order By

```python
from surreal_orm import OrderBy

# Ascending (default)
users = await User.objects().order_by("name").exec()

# Descending (Django-style shorthand)
users = await User.objects().order_by("-created_at").exec()

# Descending (explicit enum)
users = await User.objects().order_by("created_at", OrderBy.DESC).exec()
```

### Limit

```python
# Get first 10 users
users = await User.objects().limit(10).exec()
```

### Offset

```python
# Skip first 20, get next 10 (pagination)
users = await User.objects().offset(20).limit(10).exec()
```

### Chaining

```python
# Complex query
users = await (
    User.objects()
    .filter(status="active")
    .order_by("-created_at")
    .limit(10)
    .offset(0)
    .exec()
)
```

---

## Transactions

All CRUD operations support transactions via the `tx` parameter.

### Using Transactions

```python
from surreal_orm import SurrealDBConnectionManager

# Via ConnectionManager
async with await SurrealDBConnectionManager.transaction() as tx:
    user = User(name="Alice", balance=1000)
    await user.save(tx=tx)

    order = Order(user_id=user.id, total=100)
    await order.save(tx=tx)

    user.balance -= 100
    await user.update(tx=tx)
    # All-or-nothing: commit on success, rollback on exception
```

### Model Transaction Shortcut

```python
# Via Model class method
async with User.transaction() as tx:
    await user1.save(tx=tx)
    await user2.delete(tx=tx)
```

### Transaction Methods

All model methods accept a `tx` parameter:

```python
await model.save(tx=tx)
await model.update(tx=tx)
await model.merge(tx=tx, **data)
await model.delete(tx=tx)
```

---

## Aggregations

Django-style aggregation methods for computing values across records.

### Simple Aggregations

```python
# Count all records
count = await User.objects().count()

# Count with filter
active_count = await User.objects().filter(active=True).count()

# Field aggregations
avg_age = await User.objects().avg("age")
total = await Order.objects().filter(status="paid").sum("amount")
min_val = await Product.objects().min("price")
max_val = await Product.objects().max("price")
```

### GROUP BY with values() and annotate()

```python
from surreal_orm import Count, Sum, Avg, Min, Max

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

---

## Bulk Operations

Efficient batch operations with optional transaction support.

### Bulk Create

```python
users = [User(name=f"User{i}") for i in range(1000)]

# Simple bulk create
created = await User.objects().bulk_create(users)

# Atomic bulk create (all-or-nothing)
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

## Relations & Graph Traversal

SurrealDB is a graph database. The ORM provides declarative relation definitions and graph traversal.

### Relation Field Types

```python
from surreal_orm import BaseSurrealModel, ForeignKey, ManyToMany, Relation

class User(BaseSurrealModel):
    id: str | None = None
    name: str

    # Graph relations (SurrealDB edges)
    followers: Relation("follows", "User", reverse=True)
    following: Relation("follows", "User")

    # Traditional relations
    profile: ForeignKey("Profile", on_delete="CASCADE")
    groups: ManyToMany("Group", through="membership")

class Post(BaseSurrealModel):
    id: str | None = None
    title: str
    author: ForeignKey("User", related_name="posts")
```

### Creating Relations with relate()

```python
# Create a graph edge
await alice.relate("follows", bob)

# With edge data
await alice.relate("follows", bob, since="2025-01-01", strength="strong")

# Reverse direction: bob -> follows -> alice (instead of alice -> follows -> bob)
# Useful when the schema expects a specific direction for the edge table
await alice.relate("follows", bob, reverse=True)

# Within a transaction
async with await SurrealDBConnectionManager.transaction() as tx:
    await alice.relate("follows", bob, tx=tx)
    await alice.relate("follows", charlie, tx=tx)
```

### Querying Relations with get_related()

```python
# Get outgoing relations (who alice follows)
following = await alice.get_related("follows", direction="out", model_class=User)

# Get incoming relations (who follows alice)
followers = await alice.get_related("follows", direction="in", model_class=User)

# Without model_class (returns dicts)
following_data = await alice.get_related("follows", direction="out")
```

### Recursive Traversal, Paths and Shortest Path

`get_related()` follows one hop. Three `QuerySet` methods walk an edge recursively, with SurrealDB's recursive
idioms (`{min..max+collect}`, `+path`, `+shortest`), and each has an instance shortcut:

| QuerySet method | Instance shortcut | Returns |
| --- | --- | --- |
| `reachable_from(source, edge, ...)` | `get_reachable(edge, ...)` | the QuerySet (chainable); `exec()` gives each reachable record once |
| `paths_from(source, edge, *, max_depth, ...)` | `get_paths(edge, *, max_depth, ...)` | `list[list[T]]`, every walk from the source |
| `shortest_path(source, target, edge, ...)` | `get_shortest_path(target, edge, ...)` | `list[T]` (fewest hops), or `None` when there is no path |

The examples below use this graph of `follows` edges between `users`:

```python
# alice -> bob -> charlie -> dave
# alice -> erin -> dave
await alice.relate("follows", bob)
await bob.relate("follows", charlie)
await charlie.relate("follows", dave)
await alice.relate("follows", erin)
await erin.relate("follows", dave)
```

#### Reachable records — `reachable_from()` / `get_reachable()`

The QuerySet's table is the table of the nodes walked; the source may belong to any table. The result is a normal
QuerySet: `filter()`, `Q`, `order_by()`, `limit()`, `offset()`, `select()`, `fetch()`, `prefetch_related()`,
`count()` and the aggregations all apply to it. Calling `reachable_from()` again replaces the previous traversal.

```python
from surreal_orm import Q

users = await User.objects().reachable_from(alice, "follows", max_depth=3).order_by("name").exec()
# [bob, charlie, dave, erin]

n = await User.objects().reachable_from(alice, "follows", max_depth=3).count()
# 4

# The source itself, first in the walk
users = await User.objects().reachable_from(alice, "follows", max_depth=3, include_source=True).exec()
# [alice, bob, charlie, dave, erin] (in some order)

# Only records 2 or 3 hops away; follow the edges backwards; or both ways
far = await User.objects().reachable_from(alice, "follows", min_depth=2, max_depth=3).exec()  # [charlie, dave]
fans = await User.objects().reachable_from(dave, "follows", max_depth=3, direction="in").exec()  # 4 users
around = await User.objects().reachable_from(bob, "follows", max_depth=1, direction="both").exec()  # [alice, charlie]

# One hop into another table
posts = await Post.objects().reachable_from(alice, "wrote", max_depth=1).exec()

# The edge as a model class (a model with table_type=TableType.RELATION and table_name="follows")
users = await User.objects().reachable_from(alice, Follows, max_depth=1).exec()

# Instance shortcut: model_class is the node model (default: the instance's own class)
reach = await alice.get_reachable("follows", max_depth=3)  # list[User]
posts = await alice.get_reachable("wrote", max_depth=1, model_class=Post)  # list[Post]
```

**`filter()` vs `node_filter=` / `edge_filter=`.** `filter()` and `Q` on the QuerySet filter the **result** (the
`WHERE` of the outer `SELECT`): the walk still goes through a record that fails them. `node_filter=` and
`edge_filter=` **prune the walk**: a node failing `node_filter`, or an edge failing `edge_filter`, is not
traversed, so nothing beyond it is reached through it. On a graph `a -> b -> c -> d` (edge weight `w = 1`) plus
`a -> e -> d` (`a -> e` has `w = 5`), where `c` is the only inactive record:

```python
walk = Person.objects().reachable_from(a, "knows", max_depth=3, edge_filter=Q(w__lt=5))
# {b, c, d}: the heavy edge a -> e is not walked

await walk.filter(active=True).exec()
# {b, d}: c is dropped from the result, but d is still reached through it

await Person.objects().reachable_from(
    a, "knows", max_depth=3, edge_filter=Q(w__lt=5), node_filter=Q(active=True)
).exec()
# {b}: the walk stops at c, so d is never reached
```

`node_filter` values are converted like `filter()` values (`ForeignKey` columns become records). A `Subquery` is
not accepted inside `node_filter` / `edge_filter`.

**Filtering on the edge's endpoints.** On an edge table, `in` and `out` are always records, and a string never
equals a record. `edge_filter` therefore converts the values of `in` / `out` (lookups `exact`, `in`, `not_in`) to
records: a model instance, a `RecordId` or a `"table:id"` string. A bare id such as `"bob"` has no table to resolve
against and raises `ValueError` (`edge_filter on 'out' compares records: ...`). `in` is a Python keyword, so write
`Q(**{"in": alice})` or `Q(in__exact=alice)` — never `Q(in=…)`, which is a syntax error:

```python
# Walk only the edges that leave alice
await User.objects().reachable_from(alice, "follows", max_depth=3, edge_filter=Q(**{"in": alice})).exec()
await User.objects().reachable_from(alice, "follows", max_depth=3, edge_filter=Q(in__exact=alice)).exec()
# both: [bob, erin]

# Walk only the edges that arrive at bob or erin
await User.objects().reachable_from(alice, "follows", max_depth=3, edge_filter=Q(out__in=[bob, erin])).exec()
# [bob, erin]
```

#### Every walk — `paths_from()` / `get_paths()`

Each path is a list of model instances in walk order, the source first (`include_source=True` is the default
here). A walk stops at `max_depth` or at a dead end; walks shorter than `min_depth` are dropped. `max_depth` is
required, and `direction` is `"out"` or `"in"` (`"both"` raises). `paths_from()` is terminal: it runs on a bare
QuerySet (`filter()`, `order_by()`, `limit()` and the other read clauses raise `ValueError`; constrain the walk with
`node_filter=` / `edge_filter=`).

```python
paths = await User.objects().paths_from(alice, "follows", max_depth=3)
for path in paths:
    print(" -> ".join(u.name for u in path))
# Alice -> Bob -> Charlie -> Dave
# Alice -> Erin -> Dave   (paths come in no particular order)

paths = await alice.get_paths("follows", max_depth=3)  # list[list[User]]
```

> **Cost.** `paths_from()` does not cut cycles: a record can appear several times in one path, and the number of
> paths grows with the branching factor to the power of `max_depth` (a cyclic graph with two out-edges per node
> gives about 2^`max_depth` paths). Keep `max_depth` small on dense or cyclic graphs, and make it an **application
> constant — never a request parameter**. The cap of `MAX_PATH_DEPTH` (32) only stops an accidental large depth;
> it does not bound the work the server does below it.

#### Shortest path — `shortest_path()` / `get_shortest_path()`

The path with the fewest hops (unweighted; one of them when several tie), as `[source, ..., target]`, or `None`
when no path exists within `max_depth`. The target must be a record of the QuerySet's table. `source == target`
gives `[source]` when the record exists. `node_filter` applies to every node after the source, the target
included. Like `paths_from()`, it is terminal.

```python
path = await User.objects().shortest_path(alice, dave, "follows")
if path is None:
    print("not connected")
else:
    print(len(path) - 1, "hops:", [u.name for u in path])
# 2 hops: ['Alice', 'Erin', 'Dave']

path = await alice.get_shortest_path(dave, "follows")  # list[User] | None, same path
path = await User.objects().shortest_path(dave, alice, "follows")  # None: no edge leaves dave
path = await User.objects().shortest_path(alice, dave, "follows", include_source=False)  # [erin, dave]
```

A path whose records the session cannot read (for example a node hidden by table permissions) also gives `None`,
and logs a warning so it can be told apart from "no path"; `paths_from()` drops such a path and logs a warning too.

#### Rules shared by the three methods

- **Depths.** `max_depth` and `min_depth` are `int`s from 1 to 256 (`MAX_GRAPH_DEPTH`, SurrealDB's recursion limit),
  `min_depth <= max_depth`, checked at the call. `max_depth=None` (the default of `reachable_from()` and
  `shortest_path()`) walks up to the server's limit; a graph deeper than that makes the server reject the query, raised
  as `QueryError` (`Exceeded the idiom recursion limit of 256.`). `paths_from()` needs `max_depth`, at most 32
  (`MAX_PATH_DEPTH`).
- **Order.** The order of the results is unspecified: use `order_by()` (and sort the output of `paths_from()`).
- **`include_source`.** `False` (default for `reachable_from()`) never returns the source, even when a cycle comes
  back to it; `True` (default for `paths_from()` and `shortest_path()`) returns it, and it is not subject to
  `node_filter`. With `True`, the source must be a record of the QuerySet's table, because it is returned as one; a
  source from another table needs `include_source=False`:

  ```python
  # ValueError: include_source=True needs a source in 'posts', got 'users:alice': the source would be
  # returned as a 'posts' record; pass include_source=False
  await Post.objects().paths_from(alice, "wrote", max_depth=1)

  paths = await Post.objects().paths_from(alice, "wrote", max_depth=1, include_source=False)
  ```

- **Sources and targets.** A saved model instance, a `RecordId`, a `"table:id"` string or a bare id (read in the
  QuerySet's table). A string is read the way `get()` reads it, so its key is always a string: `"users:1"` is the
  record with the key `"1"`, not the integer `1` — pass `RecordId("users", 1)` for an integer key. A key that is not a
  plain identifier is escaped with backticks (``"users:`7x`"``); the `⟨…⟩` spelling raises `ValueError` (`The ⟨…⟩
  record-id spelling is not supported; ...`) instead of silently matching nothing:

  ```python
  from surreal_sdk import RecordId

  await User.objects().reachable_from("users:alice", "follows", max_depth=3).exec()
  await User.objects().reachable_from(RecordId("users", 1), "follows", max_depth=3).exec()
  await User.objects().reachable_from("users:`7x`", "follows", max_depth=3).exec()
  ```

- **Edges.** `edge` is the edge table's name or its model class, an identifier either way.
- **Not combinable.** A traversal QuerySet refuses `cache()` (edge writes do not invalidate cached results),
  `live()`, `changes()`, `query()`, `graph_query()`, `hybrid_search()`, `similar_to()` / `search()` / `nearby()`,
  `Subquery(...)`, `Prefetch(queryset=...)` and the bulk writes (`bulk_update()`, `bulk_delete()`, `upsert()`,
  `bulk_upsert()`, `delete_table()`) with a `ValueError`, rather than silently ignoring the traversal.
- **Bound values.** The source, the target and every filter value are bound as query variables (`$_g_src`,
  `$_g_dst`, `$_gN`); a `$name` filter value refers to a variable declared with `.variables()`, as in `filter()`.
- **CBOR protocol.** The traversals compare records. Under `protocol="json"` record values are sent as strings, which
  never equal a record, so the traversals return nothing there; use the default CBOR protocol.

### Removing Relations

```python
# Remove a relation
await alice.remove_relation("follows", bob)

# Remove a reverse relation (matches the direction used when creating)
await alice.remove_relation("follows", bob, reverse=True)

# Within a transaction
async with await SurrealDBConnectionManager.transaction() as tx:
    await alice.remove_relation("follows", bob, tx=tx)
```

### Graph Traversal with QuerySet

```python
# Raw graph query
result = await User.objects().filter(id="alice").graph_query(
    "->follows->User WHERE active = true"
)

# Using traverse()
qs = User.objects().filter(id="alice").traverse("->follows->User")
```

### Eager Loading (N+1 Prevention)

```python
# Select related - loads in same query
users = await User.objects().select_related("profile").all()

# Prefetch related - separate optimized queries
users = await User.objects().prefetch_related("followers", "posts").all()

for user in users:
    print(user.followers)  # Already loaded, no extra query
```

---

## Atomic Array Operations

Server-side array mutations that avoid read-modify-write conflicts.
Ideal for multi-pod deployments where concurrent workers update the same array field.
`atomic_append` and `atomic_set_add` start from an empty array on a record without the field.
When SurrealDB rejects the statement, the helpers raise instead of returning `[]`:
`TransactionConflictError` for a write conflict, `QueryError` otherwise (both
`SurrealDBError`, from `surreal_sdk`).

### atomic_append (allows duplicates)

```python
# Append a value to an array (duplicates allowed)
await Event.atomic_append(event_id, "processed_by", pod_id)
```

### atomic_set_add (no duplicates)

```python
# Add a value only if not already present (like set.add);
# an array field stays an array, a set<T> field stays a set
await Event.atomic_set_add(event_id, "processed_by", pod_id)
```

### atomic_remove

```python
# Remove all occurrences of a value from an array
await Event.atomic_remove(event_id, "tags", "deprecated")
```

### Retry on Conflict

For operations that may still encounter transaction conflicts, use the retry decorator:

```python
from surreal_orm import retry_on_conflict

@retry_on_conflict(max_retries=5, base_delay=0.05)
async def claim_event(event_id: str, pod_id: str):
    await Event.atomic_set_add(event_id, "processed_by", pod_id)
```

---

## Server-Side Functions (SurrealFunc)

Embed raw SurrealQL expressions in save/update operations for using server-side
functions like `time::now()` or `crypto::argon2::generate()`.

### In save()

```python
from surreal_orm import SurrealFunc

# Pass server-side expressions via server_values parameter
player = Player(seat_position=1)
await player.save(server_values={
    "joined_at": SurrealFunc("time::now()"),
    "password_hash": SurrealFunc("crypto::argon2::generate('secret')"),
})
# Generates: UPSERT players:... SET seat_position = $_sv_seat_position, joined_at = time::now(), ...
```

### In merge()

```python
# SurrealFunc values are detected automatically in merge()
await player.merge(last_ping=SurrealFunc("time::now()"))
# Generates: UPDATE players:... SET last_ping = time::now()

# Skip the extra SELECT after UPDATE (fire-and-forget)
await player.merge(last_seen=SurrealFunc("time::now()"), refresh=False)
# Generates only: UPDATE players:... SET last_seen = time::now()
# No subsequent SELECT — useful for high-frequency ops like presence pings
```

### With Bound Parameters (`extra_vars`)

When a `SurrealFunc` expression references a bound parameter, pass additional
variables via `extra_vars`:

```python
# The $password variable is bound separately
await user.save(
    server_values={
        "password_hash": SurrealFunc("crypto::argon2::generate($password)"),
        "created_at": SurrealFunc("time::now()"),
    },
    extra_vars={"password": raw_password},
)

# Also works with merge()
await user.merge(
    password_hash=SurrealFunc("crypto::argon2::generate($password)"),
    extra_vars={"password": new_password},
)
```

> **Warning:** The expression is inserted directly into the query string.
> Only use with developer-controlled values, **never** with user input.

---

## Bulk Relation Deletion

### remove_all_relations()

Remove all edges of a given type from a model instance. Accepts a single
relation name or a list of names.

```python
# Remove all outgoing "has_player" edges
await table.remove_all_relations("has_player", direction="out")

# Remove multiple relation types at once
await table.remove_all_relations(
    ["has_player", "has_action", "has_state"],
    direction="out",
)

# Remove all incoming "follows" edges (who follows this user)
await user.remove_all_relations("follows", direction="in")

# Remove both directions at once
await user.remove_all_relations("follows", direction="both")

# Within a transaction
async with await SurrealDBConnectionManager.transaction() as tx:
    await table.remove_all_relations(
        ["has_player", "has_action", "has_state"], direction="out", tx=tx,
    )
```

---

## Calling Stored Functions

Invoke custom SurrealDB functions defined with `DEFINE FUNCTION fn::...`.

```python
from surreal_orm import SurrealDBConnectionManager

# Via the connection manager
result = await SurrealDBConnectionManager.call_function(
    "acquire_game_lock",
    params={"table_id": table_id, "pod_id": pod_id, "ttl": 30},
)

# Via any model class (convenience shortcut)
result = await GameTable.call_function(
    "release_game_lock",
    params={"table_id": table_id, "pod_id": pod_id},
)

# With a typed return value (Pydantic model or dataclass)
from pydantic import BaseModel

class LockResult(BaseModel):
    acquired: bool
    expires_at: str

result = await SurrealDBConnectionManager.call_function(
    "acquire_game_lock",
    params={"table_id": "t1", "pod_id": "p1", "ttl": 30},
    return_type=LockResult,
)
```

---

## FETCH Clause (Resolving Record Links)

Use `fetch()` to resolve record link fields inline, avoiding N+1 queries.
Maps to SurrealDB's `FETCH` clause.

```python
# Resolve the 'author' record link in a single query
posts = await Post.objects().fetch("author").exec()
# Generates: SELECT * FROM posts FETCH author;

# Multiple fields
orders = await Order.objects().fetch("customer", "items").limit(50).exec()
# Generates: SELECT * FROM orders LIMIT 50 FETCH customer, items;

# select_related() also maps to FETCH
stats = await PlayerStats.objects().select_related("user").order_by("-wins").limit(100).exec()
# Generates: SELECT * FROM player_stats ORDER BY wins DESC LIMIT 100 FETCH user;
```

---

## Live Models (Real-time)

Subscribe to real-time model changes via WebSocket. The ORM wraps the SDK's
`LiveSelectStream` and yields typed `ModelChangeEvent[T]` instances with full
Pydantic model conversion.

### Basic Usage

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

### ModelChangeEvent Properties

| Property         | Type         | Description                                  |
| ---------------- | ------------ | -------------------------------------------- |
| `action`         | `LiveAction` | CREATE, UPDATE, or DELETE                    |
| `instance`       | `T`          | Full Pydantic model instance via `from_db()` |
| `record_id`      | `str`        | Affected record ID (e.g., `"users:abc123"`)  |
| `changed_fields` | `list[str]`  | Changed field names (only in DIFF mode)      |
| `raw`            | `dict`       | Original raw data from the database          |

### With Filters

All QuerySet filters translate to the live query WHERE clause:

```python
# Subscribe only to active admins over 18
async with User.objects().filter(
    role="admin", age__gte=18, is_active=True,
).live() as stream:
    async for event in stream:
        process(event)

# Q objects also work
from surreal_orm import Q

async with User.objects().filter(
    Q(role="admin") | Q(role="moderator"),
).live() as stream:
    async for event in stream:
        process(event)
```

### Options

```python
async with User.objects().live(
    auto_resubscribe=True,   # Reconnect after WebSocket drop (default: True)
    diff=True,               # Receive only changed fields
    on_reconnect=callback,   # Called with (old_id, new_id) on reconnect
) as stream:
    async for event in stream:
        print(event.changed_fields)  # Only populated in diff mode
```

### post_live_change Signal

A dedicated signal fires for external database changes detected via live queries
(separate from local CRUD signals like `post_save`/`post_update`):

```python
from surreal_orm import post_live_change, LiveAction

@post_live_change.connect(Player)
async def on_player_change(sender, instance, action, record_id, **kwargs):
    if action == LiveAction.CREATE:
        await ws_manager.broadcast({"type": "player_joined", "name": instance.name})
    elif action == LiveAction.DELETE:
        await ws_manager.broadcast({"type": "player_left", "id": record_id})
```

---

## Change Feed Integration

HTTP-based change data capture (CDC) for event-driven architectures. Unlike
live models which require WebSocket, change feeds work over HTTP and are
stateless and resumable.

### Basic Usage

```python
async for event in User.objects().changes(since="2026-01-01"):
    print(event.action, event.instance.name)
```

### Event-Driven Microservices

```python
# Publish model changes to a message queue
async for event in Order.objects().changes(since="2026-01-01"):
    await publish_to_queue({
        "type": f"order.{event.action.value.lower()}",
        "record_id": event.record_id,
        "data": event.raw,
    })
```

### Options

```python
stream = User.objects().changes(
    since="2026-02-01T00:00:00Z",  # Start from this timestamp
    poll_interval=0.1,              # Seconds between polls (default: 0.1)
    batch_size=100,                 # Max records per poll (default: 100)
)

async for event in stream:
    process(event)

# Access cursor for resumability
print(stream.cursor)  # Use this to resume later
```

---

## Custom Queries

### Using Variables

```python
# Safe parameterized query
users = await (
    User.objects()
    .filter(age__gte="$min_age")
    .variables(min_age=18)
    .exec()
)
```

A filter value is a variable reference only when it is exactly `$name` and `name`
is passed to `.variables()`. Any other value, including `"$name"` for a name that
is not declared, is bound as a parameter and compared as text:

```python
# Matches a user whose nickname is literally "$min_age"
await User.objects().filter(nickname="$min_age").exec()
```

### Raw SurrealQL

```python
# Execute custom query (must include correct FROM clause)
users = await User.objects().query(
    "SELECT * FROM User WHERE age > $age ORDER BY name",
    {"age": 21}
)
```

---

## Error Handling

### DoesNotExist

```python
from surreal_orm.model_base import SurrealDbError

try:
    user = await User.objects().get("nonexistent")
except User.DoesNotExist:
    print("User not found!")
```

### Multiple Results

```python
try:
    # get() expects exactly one result
    user = await User.objects().filter(status="active").get()
except SurrealDbError as e:
    if "More than one result" in str(e):
        print("Multiple users found, use first() or exec() instead")
```

### Connection Errors

```python
from surreal_orm.connection_manager import SurrealDbConnectionError

try:
    await SurrealDBConnectionManager.get_client()
except SurrealDbConnectionError as e:
    print(f"Cannot connect: {e}")
```

---

## Full Example

```python
import asyncio
from pydantic import Field
from surreal_orm import BaseSurrealModel, SurrealDBConnectionManager, OrderBy


class User(BaseSurrealModel):
    id: str | None = None
    name: str = Field(..., max_length=100)
    email: str
    age: int = Field(default=0, ge=0)
    is_active: bool = True


async def main():
    # Configure
    SurrealDBConnectionManager.set_connection(
        url="http://localhost:8000",
        user="root",
        password="root",
        namespace="test",
        database="test"
    )

    # Create users
    users_data = [
        {"name": "Alice", "email": "alice@example.com", "age": 30},
        {"name": "Bob", "email": "bob@example.com", "age": 25},
        {"name": "Charlie", "email": "charlie@example.com", "age": 35},
    ]

    for data in users_data:
        user = User(**data)
        await user.save()
        print(f"Created: {user.name} (ID: {user.id})")

    # Query
    active_adults = await (
        User.objects()
        .filter(is_active=True, age__gte=18)
        .order_by("age", OrderBy.DESC)
        .exec()
    )

    print(f"\nActive adults ({len(active_adults)}):")
    for user in active_adults:
        print(f"  - {user.name}, age {user.age}")

    # Update
    alice = await User.objects().filter(name="Alice").first()
    alice.age = 31
    await alice.update()
    print(f"\nUpdated Alice's age to {alice.age}")

    # Cleanup
    await User.objects().delete_table()
    print("\nAll users deleted")

    # Close connection
    await SurrealDBConnectionManager.close_connection()


if __name__ == "__main__":
    asyncio.run(main())
```
