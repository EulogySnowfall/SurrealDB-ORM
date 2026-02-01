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

# Reconnect
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

| Lookup       | Operator    | Example                                         |
| ------------ | ----------- | ----------------------------------------------- |
| `exact`      | `=`         | `filter(name="Alice")`                          |
| `gt`         | `>`         | `filter(age__gt=18)`                            |
| `gte`        | `>=`        | `filter(age__gte=18)`                           |
| `lt`         | `<`         | `filter(age__lt=65)`                            |
| `lte`        | `<=`        | `filter(age__lte=65)`                           |
| `in`         | `IN`        | `filter(status__in=["active", "pending"])`      |
| `contains`   | `CONTAINS`  | `filter(tags__contains="python")`               |
| `like`       | `LIKE`      | `filter(name__like="Ali%")`                     |
| `ilike`      | `ILIKE`     | `filter(name__ilike="ali%")` (case-insensitive) |
| `startswith` | `LIKE 'x%'` | `filter(name__startswith="Ali")`                |
| `endswith`   | `LIKE '%x'` | `filter(name__endswith="ce")`                   |
| `isnull`     | `IS NULL`   | `filter(deleted_at__isnull=True)`               |
| `regex`      | `~`         | `filter(email__regex=r".*@gmail\.com")`         |

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

### Order By

```python
from surreal_orm import OrderBy

# Ascending (default)
users = await User.objects().order_by("name").exec()

# Descending
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
    .order_by("created_at", OrderBy.DESC)
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

### Removing Relations

```python
# Remove a relation
await alice.remove_relation("follows", bob)

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
