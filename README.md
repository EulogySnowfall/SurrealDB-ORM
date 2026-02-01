# SurrealDB-ORM

![Python](https://img.shields.io/badge/python-3.12%2B-blue)
![CI](https://github.com/EulogySnowfall/SurrealDB-ORM/actions/workflows/ci.yml/badge.svg)
[![codecov](https://codecov.io/gh/EulogySnowfall/SurrealDB-ORM/graph/badge.svg?token=XUONTG2M6Z)](https://codecov.io/gh/EulogySnowfall/SurrealDB-ORM)
![GitHub License](https://img.shields.io/github/license/EulogySnowfall/SurrealDB-ORM)

> **Alpha Software** - APIs may change. Use in non-production environments.

**SurrealDB-ORM** is a Django-style ORM for [SurrealDB](https://surrealdb.com/) with async support, Pydantic validation, Django-style migrations, and JWT authentication.

**Includes a custom SDK (`surreal_sdk`)** - No dependency on the official `surrealdb` package!

---

## Table of Contents

- [Version](#version)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [Features](#features)
- [CLI Commands](#cli-commands)
- [Authentication](#authentication)
- [Documentation](#documentation)
- [Contributing](#contributing)
- [License](#license)

---

## Version

**0.2.1** (Alpha)

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

**Requirements:**

- Python 3.12+
- SurrealDB 2.6.0+

---

## Quick Start

### 1. Define Models

```python
from surreal_orm import BaseSurrealModel, SurrealConfigDict

class User(BaseSurrealModel):
    id: str | None = None
    name: str
    email: str
    age: int = 0
```

### 2. Configure Connection

```python
from surreal_orm import SurrealDBConnectionManager

SurrealDBConnectionManager.set_connection(
    url="http://localhost:8000",
    user="root",
    password="root",
    namespace="myapp",
    database="main",
)
```

### 3. CRUD Operations

```python
# Create
user = User(name="Alice", email="alice@example.com", age=30)
await user.save()

# Read
users = await User.objects().filter(name="Alice").exec()
user = await User.objects().get(id="user:123")

# Update
user.age = 31
await user.save()

# Delete
await user.delete()
```

### 4. QuerySet

```python
# Filter with lookups
adults = await User.objects().filter(age__gte=18).exec()

# Chaining
results = await User.objects() \
    .filter(age__gte=18) \
    .order_by("name") \
    .limit(10) \
    .exec()

# Supported lookups
# exact, gt, gte, lt, lte, in, like, ilike, contains, icontains,
# startswith, istartswith, endswith, iendswith, match, regex, isnull
```

---

## Features

### Core ORM

- Model definition with Pydantic validation
- QuerySet with Django-style lookups (`age__gte`, `name__in`, etc.)
- Async CRUD operations
- Automatic ID handling for SurrealDB RecordIDs

### Django-Style Migrations

- Generate migrations from model changes
- Apply/rollback schema migrations
- Track migration history in database

```bash
surreal-orm makemigrations --name initial
surreal-orm migrate
surreal-orm status
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
    # ...
```

### Encrypted Fields

```python
from surreal_orm.fields import Encrypted

class User(BaseSurrealModel):
    password: Encrypted  # Auto-hashed with argon2
```

### JWT Authentication

```python
from surreal_orm.auth import AuthenticatedUserMixin

class User(AuthenticatedUserMixin, BaseSurrealModel):
    model_config = SurrealConfigDict(table_type=TableType.USER)
    email: str
    password: Encrypted
    name: str

# Signup
user = await User.signup(email="alice@example.com", password="secret", name="Alice")

# Signin
user, token = await User.signin(email="alice@example.com", password="secret")
```

---

## CLI Commands

Requires `pip install surrealdb-orm[cli]`

| Command             | Description                 |
| ------------------- | --------------------------- |
| `makemigrations`    | Generate migration files    |
| `migrate`           | Apply schema migrations     |
| `upgrade`           | Apply data migrations       |
| `rollback <target>` | Rollback to migration       |
| `status`            | Show migration status       |
| `sqlmigrate <name>` | Show SQL without executing  |
| `shell`             | Interactive SurrealQL shell |

```bash
# Generate and apply migrations
surreal-orm makemigrations --name initial
surreal-orm migrate -u http://localhost:8000 -n myns -d mydb

# Check status
surreal-orm status

# Environment variables supported
export SURREAL_URL=http://localhost:8000
export SURREAL_NAMESPACE=myns
export SURREAL_DATABASE=mydb
surreal-orm migrate
```

---

## Authentication

SurrealDB-ORM uses SurrealDB's native `DEFINE ACCESS ... TYPE RECORD` for JWT authentication:

```python
from surreal_orm import BaseSurrealModel, SurrealConfigDict
from surreal_orm.types import TableType
from surreal_orm.fields import Encrypted
from surreal_orm.auth import AuthenticatedUserMixin

class User(AuthenticatedUserMixin, BaseSurrealModel):
    model_config = SurrealConfigDict(
        table_type=TableType.USER,
        identifier_field="email",
        password_field="password",
        token_duration="1h",
        session_duration="24h",
    )

    id: str | None = None
    email: str
    password: Encrypted
    name: str

# Create user
user = await User.signup(
    email="user@example.com",
    password="secure_password",
    name="John Doe",
)

# Authenticate
user, token = await User.signin(
    email="user@example.com",
    password="secure_password",
)

# Validate token
user = await User.authenticate_token(token)

# Change password
await User.change_password(
    identifier_value="user@example.com",
    old_password="secure_password",
    new_password="new_secure_password",
)
```

---

## Documentation

- [Migration System](docs/migrations.md) - Complete migration guide
- [Authentication](docs/auth.md) - JWT authentication guide
- [CHANGELOG](CHANGELOG) - Version history

---

## Contributing

Contributions are welcome!

1. Fork the repository
2. Create a branch (`git checkout -b feature/new-feature`)
3. Make your changes and commit (`git commit -m "Add new feature"`)
4. Push to your branch (`git push origin feature/new-feature`)
5. Create a Pull Request

### Development

```bash
# Clone and install
git clone https://github.com/EulogySnowfall/SurrealDB-ORM.git
cd SurrealDB-ORM
uv sync

# Run tests
make test              # Unit tests
make test-integration  # Integration tests (requires SurrealDB)

# Start SurrealDB for testing
make db-up             # Test instance (port 8001)
make db-dev            # Dev instance (port 8000)

# Lint
uv run ruff check src/
uv run mypy src/
```

---

## TODO

- [ ] Relations (ForeignKey, ManyToMany, graph traversal)
- [ ] Aggregations (count, sum, avg, GROUP BY)
- [ ] Transaction support
- [ ] Connection pooling improvements
- [x] ~~Migration system~~
- [x] ~~JWT Authentication~~
- [x] ~~CLI commands~~

---

## License

MIT License - See [LICENSE](LICENSE) file.

---

**Author:** Yannick Croteau
**GitHub:** [EulogySnowfall](https://github.com/EulogySnowfall)
