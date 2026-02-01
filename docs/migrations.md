# Migrations Documentation

Django-style migration system for SurrealDB schema versioning.

## Table of Contents

- [Installation](#installation)
- [Quick Start](#quick-start)
- [CLI Commands](#cli-commands)
- [Table Types](#table-types)
- [Schema Modes](#schema-modes)
- [Migration Operations](#migration-operations)
- [Writing Custom Migrations](#writing-custom-migrations)
- [Programmatic API](#programmatic-api)

---

## Installation

The CLI requires the `cli` extra:

```bash
pip install surrealdb-orm[cli]
```

---

## Quick Start

### 1. Define Your Models

```python
from surreal_orm import BaseSurrealModel, SurrealConfigDict
from surreal_orm.types import TableType, SchemaMode

class User(BaseSurrealModel):
    model_config = SurrealConfigDict(
        table_name="users",
        schema_mode=SchemaMode.SCHEMAFULL,
    )

    id: str | None = None
    name: str
    email: str
    age: int = 0
```

### 2. Generate Migrations

```bash
surreal-orm makemigrations --name initial
```

This creates a migration file in `migrations/0001_initial.py`:

```python
from surreal_orm.migrations import Migration
from surreal_orm.migrations.operations import CreateTable, AddField

migration = Migration(
    name="0001_initial",
    dependencies=[],
    operations=[
        CreateTable(name="users", schema_mode="SCHEMAFULL"),
        AddField(table="users", name="name", field_type="string"),
        AddField(table="users", name="email", field_type="string"),
        AddField(table="users", name="age", field_type="int", default=0),
    ],
)
```

### 3. Apply Migrations

```bash
surreal-orm migrate --url http://localhost:8000 --namespace myns --database mydb
```

---

## CLI Commands

### makemigrations

Generate migration files from model changes.

```bash
# Generate migration with name
surreal-orm makemigrations --name add_users

# Create empty migration for manual editing
surreal-orm makemigrations --name custom_changes --empty

# Specify models module
surreal-orm makemigrations --name initial --models myapp.models
```

### migrate

Apply pending schema migrations (DDL: DEFINE TABLE, DEFINE FIELD, etc.).

```bash
# Apply all pending migrations
surreal-orm migrate --url http://localhost:8000 -n myns -d mydb

# Apply up to specific migration
surreal-orm migrate --target 0002_add_email

# Mark as applied without executing (fake)
surreal-orm migrate --fake
```

### upgrade

Apply data migrations (DML: UPDATE, record transformations).

```bash
surreal-orm upgrade --url http://localhost:8000 -n myns -d mydb
```

### rollback

Rollback migrations to a specific point.

```bash
# Rollback to specific migration (keeps that migration applied)
surreal-orm rollback 0001_initial

# Rollback all migrations
surreal-orm rollback ""
```

### status

Show migration status.

```bash
surreal-orm status --url http://localhost:8000 -n myns -d mydb
```

Output:

```text
Migration status:
------------------------------------------------------------
[X] 0001_initial (3 ops) [R-]
[ ] 0002_add_email (1 ops) [R-]
------------------------------------------------------------
Legend: [X]=applied, R=reversible, D=has data migrations
```

### sqlmigrate

Show SQL for a migration without executing.

```bash
surreal-orm sqlmigrate 0001_initial
```

Output:

```sql
DEFINE TABLE users SCHEMAFULL;
DEFINE FIELD name ON users TYPE string;
DEFINE FIELD email ON users TYPE string;
DEFINE FIELD age ON users TYPE int DEFAULT 0;
```

### shell

Interactive SurrealQL shell.

```bash
surreal-orm shell --url http://localhost:8000 -n myns -d mydb
```

---

## Table Types

Configure table behavior using `TableType`:

```python
from surreal_orm.types import TableType

class User(BaseSurrealModel):
    model_config = SurrealConfigDict(table_type=TableType.USER)
    # ...

class Order(BaseSurrealModel):
    model_config = SurrealConfigDict(table_type=TableType.STREAM)
    # ...
```

| Type     | Description              | Schema Mode         | Use Case                         |
| -------- | ------------------------ | ------------------- | -------------------------------- |
| `NORMAL` | Standard table (default) | Configurable        | General data                     |
| `USER`   | Authentication table     | Enforced SCHEMAFULL | User accounts with signup/signin |
| `STREAM` | Real-time table          | Configurable        | Live feeds, notifications        |
| `HASH`   | Lookup/cache table       | Defaults SCHEMALESS | Session data, caching            |

### USER Tables

USER tables automatically:

- Enforce SCHEMAFULL schema mode
- Generate `DEFINE ACCESS ... TYPE RECORD` for JWT authentication
- Support `signup()` and `signin()` methods via `AuthenticatedUserMixin`

### STREAM Tables

STREAM tables support CHANGEFEED for real-time updates:

```python
class Notification(BaseSurrealModel):
    model_config = SurrealConfigDict(
        table_type=TableType.STREAM,
        changefeed="7d",  # Keep changes for 7 days
    )
    # ...
```

Generated SQL:

```sql
DEFINE TABLE Notification SCHEMAFULL CHANGEFEED 7d;
```

---

## Schema Modes

```python
from surreal_orm.types import SchemaMode

class StrictModel(BaseSurrealModel):
    model_config = SurrealConfigDict(schema_mode=SchemaMode.SCHEMAFULL)
    # Only defined fields allowed

class FlexibleModel(BaseSurrealModel):
    model_config = SurrealConfigDict(schema_mode=SchemaMode.SCHEMALESS)
    # Any fields allowed
```

---

## Migration Operations

### CreateTable

```python
CreateTable(
    name="users",
    schema_mode="SCHEMAFULL",  # or "SCHEMALESS"
    table_type="normal",       # normal, user, stream, hash
    changefeed="7d",           # optional
)
```

### DropTable

```python
DropTable(name="old_table")
```

### AddField

```python
AddField(
    table="users",
    name="email",
    field_type="string",
    default="user@example.com",  # optional
    assertion="is::email($value)",  # optional validation
    encrypted=False,  # True for password fields
)
```

### DropField

```python
DropField(table="users", name="old_field")
```

### AlterField

```python
AlterField(
    table="users",
    name="email",
    field_type="string",
    assertion="is::email($value)",
    # Store previous values for rollback
    previous_type="string",
    previous_assertion=None,
)
```

### CreateIndex

```python
CreateIndex(
    table="users",
    name="email_unique",
    fields=["email"],
    unique=True,
)
```

### DropIndex

```python
DropIndex(table="users", name="old_index")
```

### DefineAccess

```python
DefineAccess(
    name="user_auth",
    table="User",
    signup_fields={
        "email": "$email",
        "password": "crypto::argon2::generate($password)",
        "name": "$name",
    },
    signin_where="email = $email AND crypto::argon2::compare(password, $password)",
    duration_token="15m",
    duration_session="12h",
)
```

### RemoveAccess

```python
RemoveAccess(name="user_auth")
```

### DataMigration

For record transformations:

```python
DataMigration(
    forwards="""
        UPDATE User SET full_name = string::concat(first_name, ' ', last_name);
    """,
    backwards="""
        UPDATE User SET first_name = string::split(full_name, ' ')[0];
    """,
)
```

### RawSQL

For custom SQL:

```python
RawSQL(
    sql="DEFINE EVENT user_created ON User WHEN $event = 'CREATE' THEN ...;",
    reverse_sql="REMOVE EVENT user_created ON User;",
)
```

---

## Writing Custom Migrations

Create an empty migration:

```bash
surreal-orm makemigrations --name custom_changes --empty
```

Edit the generated file:

```python
from surreal_orm.migrations import Migration
from surreal_orm.migrations.operations import RawSQL, DataMigration

migration = Migration(
    name="0003_custom_changes",
    dependencies=["0002_add_email"],
    operations=[
        # Add custom index
        RawSQL(
            sql="DEFINE INDEX user_email_idx ON User FIELDS email UNIQUE;",
            reverse_sql="REMOVE INDEX user_email_idx ON User;",
        ),

        # Migrate data
        DataMigration(
            forwards="UPDATE User SET verified = false WHERE verified IS NONE;",
            backwards=None,  # Irreversible
        ),
    ],
)
```

---

## Programmatic API

### Generate Migrations Programmatically

```python
from pathlib import Path
from surreal_orm.migrations.generator import MigrationGenerator
from surreal_orm.migrations.operations import CreateTable, AddField

generator = MigrationGenerator(Path("migrations"))

filepath = generator.generate(
    name="add_products",
    operations=[
        CreateTable(name="products", schema_mode="SCHEMAFULL"),
        AddField(table="products", name="name", field_type="string"),
        AddField(table="products", name="price", field_type="float"),
    ],
    dependencies=["0001_initial"],
)

print(f"Generated: {filepath}")
```

### Execute Migrations Programmatically

```python
from pathlib import Path
from surreal_orm import SurrealDBConnectionManager
from surreal_orm.migrations.executor import MigrationExecutor

# Setup connection
SurrealDBConnectionManager.set_connection(
    url="http://localhost:8000",
    user="root",
    password="root",
    namespace="myns",
    database="mydb",
)

async def run_migrations():
    executor = MigrationExecutor(Path("migrations"))

    # Apply all pending
    applied = await executor.migrate()
    print(f"Applied: {applied}")

    # Check status
    status = await executor.get_migration_status()
    for name, info in status.items():
        print(f"{name}: {'applied' if info['applied'] else 'pending'}")

import asyncio
asyncio.run(run_migrations())
```

### Introspect Models

```python
from surreal_orm.migrations.introspector import introspect_models, ModelIntrospector

# Introspect all registered models
state = introspect_models()

for table_name, table in state.tables.items():
    print(f"Table: {table_name}")
    print(f"  Schema: {table.schema_mode}")
    print(f"  Type: {table.table_type}")
    for field_name, field in table.fields.items():
        print(f"  Field: {field_name} ({field.field_type})")
```

---

## Environment Variables

The CLI supports environment variables:

| Variable            | Description   |
| ------------------- | ------------- |
| `SURREAL_URL`       | SurrealDB URL |
| `SURREAL_NAMESPACE` | Namespace     |
| `SURREAL_DATABASE`  | Database      |
| `SURREAL_USER`      | Username      |
| `SURREAL_PASSWORD`  | Password      |

Example:

```bash
export SURREAL_URL=http://localhost:8000
export SURREAL_NAMESPACE=myns
export SURREAL_DATABASE=mydb
export SURREAL_USER=root
export SURREAL_PASSWORD=root

surreal-orm migrate  # Uses env vars
```

---

## Best Practices

1. **Always review generated migrations** before applying
2. **Test migrations** on a staging database first
3. **Use `--fake`** for manual schema changes already applied
4. **Keep migrations small** - one logical change per migration
5. **Use `DataMigration`** for record transformations, not schema changes
6. **Back up your database** before running migrations in production
