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

| Lookup | Operator | Example |
|--------|----------|---------|
| `exact` | `=` | `filter(name="Alice")` |
| `gt` | `>` | `filter(age__gt=18)` |
| `gte` | `>=` | `filter(age__gte=18)` |
| `lt` | `<` | `filter(age__lt=65)` |
| `lte` | `<=` | `filter(age__lte=65)` |
| `in` | `IN` | `filter(status__in=["active", "pending"])` |
| `contains` | `CONTAINS` | `filter(tags__contains="python")` |
| `like` | `LIKE` | `filter(name__like="Ali%")` |
| `ilike` | `ILIKE` | `filter(name__ilike="ali%")` (case-insensitive) |
| `startswith` | `LIKE 'x%'` | `filter(name__startswith="Ali")` |
| `endswith` | `LIKE '%x'` | `filter(name__endswith="ce")` |
| `isnull` | `IS NULL` | `filter(deleted_at__isnull=True)` |
| `regex` | `~` | `filter(email__regex=r".*@gmail\.com")` |

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
