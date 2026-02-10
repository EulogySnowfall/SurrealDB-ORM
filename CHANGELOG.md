# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.9.0] - 2026-02-09

### Added

- **Live Models** - ORM-level real-time subscriptions via `QuerySet.live()`
  - `LiveModelStream[T]` async context manager + iterator yielding typed `ModelChangeEvent[T]`
  - Automatic `Model.from_db()` conversion from raw dicts to Pydantic model instances
  - Full QuerySet filter integration (WHERE clause + parameterized variables)
  - `auto_resubscribe=True` for seamless WebSocket reconnect recovery
  - `diff=True` for receiving only changed fields
  - `on_reconnect` callback support

- **Change Feed Integration** - ORM-level CDC via `QuerySet.changes()`
  - `ChangeModelStream[T]` async iterator for HTTP-based event streaming
  - Cursor tracking via `.cursor` property for resumable consumption
  - Configurable `poll_interval` and `batch_size`
  - Works over HTTP (no WebSocket required)

- **`ModelChangeEvent[T]`** - Typed dataclass for live change events
  - `action: LiveAction` (CREATE, UPDATE, DELETE)
  - `instance: T` - Full Pydantic model instance
  - `record_id: str` - Affected record ID
  - `changed_fields: list[str]` - Changed fields (DIFF mode)
  - `raw: dict` - Original raw data from the database

- **`post_live_change` signal** - Fires when live query events are received from the database (separate from local CRUD signals)

- **`SurrealDBConnectionManager.get_ws_client()`** - Lazy WebSocket connection management alongside existing HTTP connection

### Changed

- Bumped version to 0.9.0 (ORM, SDK, pyproject.toml)

---

## [0.8.0] - 2026-01-20

### Added

- **Computed Fields** - Server-side computed fields using `DEFINE FIELD ... VALUE <expression>`
  - `Computed[T] = Computed("expression")` dual-use API
  - Auto-excluded from writes via `get_server_fields()`
  - Migration introspector generates VALUE clauses

- **`validate_token()`** - Lightweight token validation returning just the record ID
- **SDK `authenticate()` method** on `BaseSurrealConnection`

### Fixed

- **Auth: Ephemeral Connections** (Critical) - `signup()`, `signin()`, `authenticate_token()` no longer corrupt the singleton connection
- **Auth: `signup()` Returns Token** (High) - Now returns `tuple[Self, str]` matching `signin()`
- **Auth: `authenticate_token()` Fixed** (Medium) - Now uses ephemeral connections, returns `tuple[Self, str] | None`
- **Auth: Configurable `access_name`** (High) - No longer hardcoded to `{table}_auth`

---

## [0.7.0] - 2026-01-10

### Added

- **`merge(refresh=False)`** - Skip the extra SELECT round-trip for fire-and-forget updates
- **`call_function()`** - Invoke custom SurrealDB stored functions from the ORM
- **`extra_vars` on `save()` / `merge()`** - Bind additional query variables for SurrealFunc expressions
- **`fetch()` + FETCH clause** - Resolve record links inline (N+1 prevention)
- **`remove_all_relations()` list support** - Remove multiple relation types in one call

---

## [0.6.0] - 2025-12-28

### Added

- **Q Objects** - Django-style composable query expressions with `|` (OR), `&` (AND), `~` (NOT)
- **Parameterized Filters** - All filter values bound as `$_fN` query variables (injection prevention)
- **SurrealFunc** - Embed raw SurrealQL expressions (`time::now()`, `crypto::argon2::generate()`) in save/update
- **`remove_all_relations()`** - Bulk edge deletion with direction support (`out`, `in`, `both`)
- **Django-style `-field` ordering** - `order_by("-created_at")` shorthand

### Fixed

- **`isnull` lookup** - Now generates `IS NULL` instead of `IS True`

---

## [0.5.9] - 2025-12-15

### Added

- **Atomic Array Operations** - `atomic_append()`, `atomic_remove()`, `atomic_set_add()`
- **Transaction Conflict Retry** - `retry_on_conflict()` decorator with exponential backoff + jitter
- **`TransactionConflictError`** - New exception subclass for conflict detection
- **Relation Direction Control** - `reverse` parameter on `relate()` and `remove_relation()`
- **New Query Lookups** - `not_contains`, `containsall`, `containsany`, `not_in`

---

## [0.5.8] - 2025-12-10

### Added

- **Around Signals** - Generator-based middleware pattern (`around_save`, `around_delete`, `around_update`)
  - Shared state between before/after phases via local variables
  - Guaranteed cleanup with `try/finally`
  - Execution order: `pre_* -> around(before) -> DB -> around(after) -> post_*`

---

## [0.5.7] - 2025-12-05

### Added

- **Model Signals** - Django-style event hooks for model lifecycle
  - `pre_save`, `post_save`, `pre_delete`, `post_delete`, `pre_update`, `post_update`

---

## [0.5.6] - 2025-11-28

### Fixed

- **Relation Query ID Escaping** - Fixed missing ID escaping in `get_related()`, `RelationQuerySet`, and graph traversal for IDs starting with digits

---

## [0.5.5.3] - 2025-11-25

### Fixed

- **RecordId objects in foreign key fields** - CBOR protocol now properly converts RecordId objects to strings in all fields

---

## [0.5.5.2] - 2025-11-22

### Fixed

- **datetime_type regression** - Fixed Pydantic validation error for datetime fields introduced in v0.5.5.1
- **`_preprocess_db_record()` method** - Handles datetime parsing and RecordId conversion before validation

---

## [0.5.5.1] - 2025-11-18

### Fixed

- **Record ID escaping** (Critical) - IDs starting with digits properly escaped with backticks
- **CBOR for HTTP** (High) - HTTP connections default to CBOR, fixing `data:` prefix issues
- **`get()` full ID format** (High) - `QuerySet.get("table:id")` correctly parsed
- **`get_related()` direction="in"** (Medium) - Fixed to return actual related records
- **`update()` table name** (Medium) - Fixed ignoring custom `table_name`

---

## [0.5.5] - 2025-11-15

### Added

- **CBOR Protocol (Default)** - Binary protocol for WebSocket connections (required dependency)
- **Field Alias Support** - `Field(alias="db_column")` for DB column name mapping
- **`unset_connection_sync()`** - Synchronous connection cleanup for non-async contexts

---

## [0.5.4] - 2025-11-10

### Added

- **Record ID format handling** - `get()` accepts both `"abc123"` and `"table:abc123"`
- **`remove_relation()` accepts string IDs** - Pass string IDs instead of model instances
- **`raw_query()` class method** - Execute arbitrary SurrealQL from model class

---

## [0.5.3.3] - 2025-11-08

### Fixed

- **`from_db()` fields_set** - DB-loaded fields no longer incorrectly included in updates

---

## [0.5.3.2] - 2025-11-05

### Fixed

- **QuerySet table name** - Fixed using class name instead of configured `table_name`
- **`get()` signature** - Accepts both positional `id_item` and keyword `id=` argument

---

## [0.5.3.1] - 2025-11-02

### Fixed

- **Partial updates for persisted records** - `save()` uses `merge()` for already-persisted records
- **datetime parsing** - Auto-parses ISO 8601 strings from SurrealDB
- **`_db_persisted` flag** - Distinguishes new vs persisted records

---

## [0.5.3] - 2025-10-28

### Added

- **Upsert save behavior** - `save()` uses `upsert` for new records with ID
- **`server_fields` config** - Exclude server-generated fields from saves
- **`merge()` returns self** - Returns updated model instance

### Fixed

- **`save()` updates self** - Updates original instance in place
- **NULL values** - `exclude_unset=True` works correctly after loading from DB

---

## [0.5.2] - 2025-10-22

### Added

- **FieldType enum improvements** - `NUMBER`, `SET`, `REGEX` types, `generic()`, `from_python_type()`

### Fixed

- **datetime serialization** - Custom JSON encoder for RPC requests
- **Fluent API** - `connect()` returns `self` for chaining
- **Session cleanup** - WebSocket callback tasks properly cancelled
- **Optional fields** - `exclude_unset=True` prevents None overriding defaults

---

## [0.5.1] - 2025-10-15

### Added

- **Dependabot security workflows** - Auto-merge, patch tagging, auto-releases
- **SurrealDB security monitoring** - Daily checks, integration tests, auto-updates

---

## [0.5.0] - 2025-10-08

### Added

- **Live Select Stream** - `LiveSelectStream` async iterator for real-time changes
- **Auto-Resubscribe** - Automatic reconnection after WebSocket disconnect
- **Typed Function Calls** - `call()` with Pydantic/dataclass `return_type`
- **`LiveChange` dataclass** - `record_id`, `action`, `result`, `changed_fields`
- **`LiveAction` enum** - CREATE, UPDATE, DELETE
- **WHERE clause support** for live queries with parameterized queries

---

## [0.4.0] - 2025-09-28

### Added

- **Relations & Graph Traversal** - `ForeignKey`, `ManyToMany`, `Relation` field types
- **Relation operations** - `add()`, `remove()`, `set()`, `clear()`, `all()`, `filter()`, `count()`
- **Model methods** - `relate()`, `remove_relation()`, `get_related()`
- **QuerySet extensions** - `select_related()`, `prefetch_related()`, `traverse()`, `graph_query()`

---

## [0.3.1] - 2025-09-20

### Added

- **Bulk Operations** - `bulk_create()`, `bulk_update()`, `bulk_delete()` with atomic support

### Fixed

- ORDER BY positioned after LIMIT/START in generated SQL
- Typo "primirary_key" in error message

---

## [0.3.0] - 2025-09-12

### Added

- **ORM Transactions** - `tx` parameter on save/update/delete, `Model.transaction()` shortcut
- **Aggregations** - `Count`, `Sum`, `Avg`, `Min`, `Max` + GROUP BY with `values()`/`annotate()`

---

## [0.2.x] - 2025-08-xx

### Added

- **Custom SDK** (`surreal_sdk`) - HTTP + WebSocket connections
- **Connection pooling**
- **Atomic transactions** (SDK level)
- **Typed functions API**
- **Migration system** with CLI
- **JWT Authentication**
- **Live Queries** (SDK level)
- **Change Feeds** (SDK level)

---

## [0.1.x] - 2025-07-xx

### Added

- **Basic ORM** - `BaseSurrealModel`, `QuerySet`, CRUD operations
- **Pydantic validation** support
- **Django-style lookups** (filter, order_by, limit, offset)
