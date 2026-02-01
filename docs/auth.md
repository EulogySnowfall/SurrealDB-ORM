# Authentication Documentation

JWT authentication using SurrealDB's native `DEFINE ACCESS ... TYPE RECORD`.

## Table of Contents

- [Overview](#overview)
- [Quick Start](#quick-start)
- [Encrypted Fields](#encrypted-fields)
- [User Model Configuration](#user-model-configuration)
- [Authentication Methods](#authentication-methods)
- [Access Definition](#access-definition)
- [Permissions](#permissions)
- [Full Example](#full-example)

---

## Overview

SurrealDB-ORM provides built-in JWT authentication that leverages SurrealDB's native features:

- **DEFINE ACCESS TYPE RECORD** - Server-side signup/signin logic
- **crypto::argon2** - Password hashing (also supports bcrypt, pbkdf2, scrypt)
- **JWT tokens** - Stateless authentication
- **$auth variable** - Row-level security with authenticated user context

---

## Quick Start

### 1. Define a User Model

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
    )

    id: str | None = None
    email: str
    password: Encrypted  # Auto-hashed with argon2
    name: str
    is_active: bool = True
```

### 2. Generate and Apply Migration

```bash
surreal-orm makemigrations --name create_users
surreal-orm migrate
```

This generates:

```sql
DEFINE TABLE User SCHEMAFULL;
DEFINE FIELD email ON User TYPE string;
DEFINE FIELD password ON User TYPE string;
DEFINE FIELD name ON User TYPE string;
DEFINE FIELD is_active ON User TYPE bool DEFAULT true;

DEFINE ACCESS user_auth ON DATABASE TYPE RECORD
    SIGNUP (CREATE User SET
        email = $email,
        password = crypto::argon2::generate($password),
        name = $name,
        is_active = true,
        created_at = time::now()
    )
    SIGNIN (SELECT * FROM User WHERE
        email = $email AND
        crypto::argon2::compare(password, $password)
    )
    DURATION FOR TOKEN 15m, FOR SESSION 12h;
```

### 3. Use Authentication

```python
# Signup (creates user with hashed password)
user = await User.signup(
    email="alice@example.com",
    password="secure_password",
    name="Alice"
)

# Signin (returns user and JWT token)
user, token = await User.signin(
    email="alice@example.com",
    password="secure_password"
)

# Use token for authenticated requests
print(f"JWT Token: {token}")
```

---

## Encrypted Fields

The `Encrypted` type marks fields for password hashing:

```python
from surreal_orm.fields import Encrypted, EncryptedField
from surreal_orm.types import EncryptionAlgorithm

class User(BaseSurrealModel):
    # Default: argon2
    password: Encrypted

    # Custom algorithm
    api_key: EncryptedField(EncryptionAlgorithm.BCRYPT)
```

### Supported Algorithms

| Algorithm | Usage                | Security Level |
| --------- | -------------------- | -------------- |
| `ARGON2`  | Default, recommended | Highest        |
| `BCRYPT`  | Legacy compatibility | High           |
| `PBKDF2`  | Wide compatibility   | Medium-High    |
| `SCRYPT`  | Memory-hard          | High           |

### Generated SQL

For `password: Encrypted`:

```sql
DEFINE FIELD password ON User TYPE string
    VALUE crypto::argon2::generate($value);
```

---

## User Model Configuration

### Basic Configuration

```python
class User(AuthenticatedUserMixin, BaseSurrealModel):
    model_config = SurrealConfigDict(
        table_type=TableType.USER,  # Required for auth
    )
    # ... fields
```

### Full Configuration

```python
class User(AuthenticatedUserMixin, BaseSurrealModel):
    model_config = SurrealConfigDict(
        table_type=TableType.USER,
        table_name="app_users",           # Custom table name
        identifier_field="email",          # Field for signin (default: "email")
        password_field="password",         # Password field (default: "password")
        token_duration="30m",              # JWT lifetime (default: "15m")
        session_duration="24h",            # Session lifetime (default: "12h")
        encryption_algorithm="argon2",     # Hash algorithm (default: "argon2")
    )

    id: str | None = None
    email: str
    password: Encrypted
    name: str
```

### Custom Identifier Field

```python
class Admin(AuthenticatedUserMixin, BaseSurrealModel):
    model_config = SurrealConfigDict(
        table_type=TableType.USER,
        identifier_field="username",  # Use username instead of email
    )

    id: str | None = None
    username: str
    password: Encrypted

# Signin with username
admin, token = await Admin.signin(username="admin", password="secret")
```

---

## Authentication Methods

### signup()

Create a new user with hashed password:

```python
user = await User.signup(
    email="user@example.com",
    password="plain_text_password",  # Will be hashed
    name="New User",
)

print(f"Created user: {user.id}")
```

### signin()

Authenticate and get JWT token:

```python
user, token = await User.signin(
    email="user@example.com",
    password="plain_text_password",
)

print(f"User: {user.name}")
print(f"Token: {token}")
# Token format: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
```

### authenticate_token()

Validate an existing JWT token:

```python
# Store token (e.g., in session, cookie, header)
stored_token = "eyJhbGciOiJIUzI1NiIs..."

# Later, validate token
user = await User.authenticate_token(stored_token)

if user:
    print(f"Authenticated as: {user.email}")
else:
    print("Invalid or expired token")
```

### change_password()

Change a user's password (verifies old password first):

```python
success = await User.change_password(
    identifier_value="user@example.com",
    old_password="current_password",
    new_password="new_secure_password",
)

if success:
    print("Password changed successfully")
```

### get_access_name()

Get the access definition name:

```python
access_name = User.get_access_name()
print(access_name)  # "user_auth" or "app_users_auth" if custom table_name
```

---

## Access Definition

### AccessDefinition Class

For advanced control over the DEFINE ACCESS statement:

```python
from surreal_orm.auth import AccessDefinition

access = AccessDefinition(
    name="user_auth",
    table="User",
    identifier_field="email",
    password_field="password",
    signup_fields={
        "email": "$email",
        "password": "crypto::argon2::generate($password)",
        "name": "$name",
        "role": "'user'",
        "created_at": "time::now()",
    },
    signin_where="email = $email AND crypto::argon2::compare(password, $password) AND is_active = true",
    duration_token="30m",
    duration_session="24h",
)

# Generate SQL
sql = access.to_surreal_ql()
print(sql)
```

### AccessGenerator

Generate AccessDefinition from model:

```python
from surreal_orm.auth import AccessGenerator

# From single model
access = AccessGenerator.from_model(User)
if access:
    print(access.to_surreal_ql())

# From all USER models
definitions = AccessGenerator.generate_all([User, Admin, Customer])
for definition in definitions:
    print(definition.name)
```

---

## Permissions

### Table-Level Permissions

```python
class Post(BaseSurrealModel):
    model_config = SurrealConfigDict(
        permissions={
            "select": "true",  # Anyone can read
            "create": "$auth.id IS NOT NONE",  # Authenticated users
            "update": "$auth.id = author_id",  # Only author
            "delete": "$auth.id = author_id OR $auth.role = 'admin'",
        }
    )

    id: str | None = None
    title: str
    content: str
    author_id: str
```

Generated SQL:

```sql
DEFINE TABLE Post SCHEMAFULL
    PERMISSIONS
        FOR select WHERE true
        FOR create WHERE $auth.id IS NOT NONE
        FOR update WHERE $auth.id = author_id
        FOR delete WHERE $auth.id = author_id OR $auth.role = 'admin';
```

### Using $auth Variable

After signin, `$auth` contains the authenticated user:

```sql
-- In queries (automatic with authenticated connection)
SELECT * FROM Post WHERE author_id = $auth.id;

-- $auth fields available:
-- $auth.id - User record ID
-- $auth.* - All user fields
```

---

## Full Example

```python
import asyncio
from surreal_orm import BaseSurrealModel, SurrealConfigDict, SurrealDBConnectionManager
from surreal_orm.types import TableType
from surreal_orm.fields import Encrypted
from surreal_orm.auth import AuthenticatedUserMixin


class User(AuthenticatedUserMixin, BaseSurrealModel):
    model_config = SurrealConfigDict(
        table_type=TableType.USER,
        table_name="users",
        identifier_field="email",
        password_field="password",
        token_duration="1h",
        session_duration="24h",
        permissions={
            "select": "$auth.id = id",  # Users can only see themselves
            "update": "$auth.id = id",  # Users can only update themselves
        },
    )

    id: str | None = None
    email: str
    password: Encrypted
    name: str
    role: str = "user"
    is_active: bool = True


async def main():
    # Configure connection
    SurrealDBConnectionManager.set_connection(
        url="http://localhost:8000",
        user="root",
        password="root",
        namespace="myapp",
        database="production",
    )

    # Note: Run migrations first!
    # surreal-orm makemigrations --name initial
    # surreal-orm migrate

    # === SIGNUP ===
    print("=== Creating new user ===")
    try:
        user = await User.signup(
            email="alice@example.com",
            password="super_secure_123",
            name="Alice Smith",
        )
        print(f"Created user: {user.name} ({user.email})")
        print(f"User ID: {user.id}")
    except Exception as e:
        print(f"Signup failed: {e}")
        # User might already exist

    # === SIGNIN ===
    print("\n=== Signing in ===")
    try:
        user, token = await User.signin(
            email="alice@example.com",
            password="super_secure_123",
        )
        print(f"Signed in as: {user.name}")
        print(f"JWT Token: {token[:50]}...")
    except Exception as e:
        print(f"Signin failed: {e}")
        return

    # === TOKEN VALIDATION ===
    print("\n=== Validating token ===")
    validated_user = await User.authenticate_token(token)
    if validated_user:
        print(f"Token valid for: {validated_user.email}")
    else:
        print("Token invalid or expired")

    # === CHANGE PASSWORD ===
    print("\n=== Changing password ===")
    try:
        success = await User.change_password(
            identifier_value="alice@example.com",
            old_password="super_secure_123",
            new_password="even_more_secure_456",
        )
        if success:
            print("Password changed successfully")

            # Verify new password works
            user, new_token = await User.signin(
                email="alice@example.com",
                password="even_more_secure_456",
            )
            print(f"Signed in with new password: {user.name}")
    except Exception as e:
        print(f"Password change failed: {e}")

    # === CLEANUP ===
    await SurrealDBConnectionManager.close_connection()


if __name__ == "__main__":
    asyncio.run(main())
```

---

## Security Best Practices

1. **Use HTTPS** in production for all API calls
2. **Store tokens securely** - Use HTTP-only cookies or secure storage
3. **Set appropriate durations** - Shorter tokens are more secure
4. **Use argon2** (default) - It's the most secure algorithm
5. **Add rate limiting** - Protect against brute force attacks
6. **Validate input** - Use Pydantic validators on email/password fields
7. **Use permissions** - Always set row-level security with `$auth`

---

## Troubleshooting

### "Namespace and database must be set"

Ensure connection is configured before auth operations:

```python
SurrealDBConnectionManager.set_connection(
    namespace="myns",
    database="mydb",
    # ...
)
```

### "User not found after signup"

The ACCESS definition might not be applied. Run migrations:

```bash
surreal-orm migrate
```

### "Invalid credentials"

Check that:

1. Email/identifier is correct
2. Password is correct (case-sensitive)
3. User exists and `is_active` is true (if using that check)

### Token expired

Tokens have a limited lifetime. Get a new token via signin:

```python
user, new_token = await User.signin(email=email, password=password)
```
