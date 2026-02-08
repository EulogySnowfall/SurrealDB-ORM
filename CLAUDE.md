# SurrealDB-ORM - Development Context

> Context document for Claude AI - Last updated: February 2026

## Project Vision

**Initial goal:** Django-style ORM for SurrealDB using the official Python SDK.

**Current direction:** Complete SDK + ORM solution that connects directly via WebSocket or HTTP to SurrealDB, with zero dependency on the official `surrealdb` package.

---

## Current Version: 0.8.0 (Alpha)

### What's New in 0.8.0

- **Auth Module: Ephemeral Connections** (Bug 1 - Critical) — `signup()`, `signin()`, and `authenticate_token()` no longer mutate the root singleton connection. They create isolated ephemeral `HTTPConnection` instances that are closed after each call, preventing concurrent ORM operation failures.

- **Auth Module: Configurable Access Name** (Bug 2 - High) — Access name is no longer hardcoded to `{table}_auth`. Configure it via `access_name` in `SurrealConfigDict`:

  ```python
  class User(AuthenticatedUserMixin, BaseSurrealModel):
      model_config = SurrealConfigDict(
          table_type=TableType.USER,
          access_name="account",  # Custom DEFINE ACCESS name
      )
  ```

- **Auth Module: `signup()` Returns Token** (Bug 3 - High) — `signup()` now returns `tuple[Self, str]` (user + JWT token), matching `signin()`:

  ```python
  # Before (v0.7.0)
  user = await User.signup(email="alice@example.com", password="secret", name="Alice")

  # After (v0.8.0)
  user, token = await User.signup(email="alice@example.com", password="secret", name="Alice")
  ```

- **Auth Module: `authenticate_token()` Fixed + `validate_token()`** (Bug 4 - Medium)

  - Added `authenticate()` RPC method to `BaseSurrealConnection` in the SDK
  - Fixed `authenticate_token()` — now returns `tuple[Self, str] | None` (user + record_id)
  - Added `validate_token()` — lightweight method returning `str | None` (just the record ID)

  ```python
  # Full validation (fetches user from DB)
  result = await User.authenticate_token(token)
  if result:
      user, record_id = result

  # Lightweight validation (just checks token + gets record ID)
  record_id = await User.validate_token(token)
  ```

### What's New in 0.7.0

- **`merge(refresh=False)`** — Skip the extra SELECT after UPDATE for fire-and-forget operations

  ```python
  # Only generates UPDATE, skips the SELECT round-trip
  await user.merge(last_seen=SurrealFunc("time::now()"), refresh=False)
  ```

- **`call_function()`** — Invoke custom SurrealDB stored functions from the ORM

  ```python
  # On connection manager
  result = await SurrealDBConnectionManager.call_function(
      "acquire_game_lock",
      params={"table_id": tid, "pod_id": pid, "ttl": 30},
  )

  # On any model class
  result = await GameTable.call_function("release_game_lock", params={...})
  ```

- **`extra_vars` on `save()` and `merge()`** — Bind additional query variables for SurrealFunc expressions that reference parameters

  ```python
  await user.save(
      server_values={"password_hash": SurrealFunc("crypto::argon2::generate($password)")},
      extra_vars={"password": raw_password},
  )
  ```

- **`fetch()` + FETCH clause** — Resolve record links inline to avoid N+1 queries

  ```python
  posts = await Post.objects().fetch("author", "tags").exec()
  # Generates: SELECT * FROM posts FETCH author, tags;

  # select_related() also maps to FETCH
  stats = await PlayerStats.objects().select_related("user").exec()
  ```

- **`remove_all_relations()` with list support** — Remove multiple relation types in one call

  ```python
  await table.remove_all_relations(
      ["has_player", "has_action", "has_state"], direction="out",
  )
  ```

### What's New in 0.6.0

- **Q Objects for Complex Queries** - Django-style composable query expressions

  - **`Q` class** - Combine conditions with `|` (OR), `&` (AND), and `~` (NOT) operators
  - **Use case**: Complex WHERE clauses that require OR logic or negation

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

- **Parameterized Filters (Security)** - All filter values are now bound as query variables

  - **Automatic parameterization** - Filter values use `$_fN` variables instead of string interpolation
  - **Prevents injection** - Values are never directly embedded in query strings
  - **Backwards compatible** - Existing `$variable` references still work via `.variables()`

    ```python
    # Before (v0.5.x): values were string-interpolated with repr()
    # SELECT * FROM users WHERE age > 18;

    # After (v0.6.0): values are parameterized
    # SELECT * FROM users WHERE age > $_f0;  with {"_f0": 18}

    # Explicit variable references still work
    users = await User.objects().filter(age__gte="$min_age").variables(min_age=18).exec()
    ```

- **SurrealFunc for Server-Side Functions** - Embed raw SurrealQL expressions in save/update

  - **`SurrealFunc(expression)`** - Marker for server-side function calls
  - **Use case**: Using `time::now()`, `crypto::argon2::generate()`, or other SurrealQL functions

    ```python
    from surreal_orm import SurrealFunc

    # In save() with server_values parameter
    player = Player(seat_position=1)
    await player.save(server_values={
        "joined_at": SurrealFunc("time::now()"),
    })
    # Generates: UPSERT players:... SET seat_position = $_sv_seat_position, joined_at = time::now()

    # In merge() - SurrealFunc values detected automatically
    await player.merge(last_ping=SurrealFunc("time::now()"))
    # Generates: UPDATE players:... SET last_ping = time::now()
    ```

- **`remove_all_relations()`** - Bulk relation deletion on model instances

  - **Direction support**: `"out"`, `"in"`, or `"both"`
  - **Transaction support** via `tx` parameter
  - **Use case**: Cleaning up all edges of a type before deletion or reset

    ```python
    # Remove all outgoing "has_player" edges from this table
    await table.remove_all_relations("has_player", direction="out")

    # Remove all incoming "follows" edges (who follows this user)
    await user.remove_all_relations("follows", direction="in")

    # Remove both directions
    await user.remove_all_relations("follows", direction="both")

    # Within a transaction
    async with SurrealDBConnectionManager.transaction() as tx:
        await table.remove_all_relations("has_player", direction="out", tx=tx)
    ```

- **Django-style `-field` Ordering** - Shorthand for descending order

  ```python
  # New shorthand (v0.6.0)
  users = await User.objects().order_by("-created_at").exec()

  # Equivalent to (still works)
  users = await User.objects().order_by("created_at", OrderBy.DESC).exec()
  ```

- **Bug Fix: `isnull` Lookup** - Fixed `filter(field__isnull=True)` generating `field IS True` instead of `field IS NULL`

  ```python
  # Before (broken): WHERE deleted_at IS True
  # After (fixed):   WHERE deleted_at IS NULL
  users = await User.objects().filter(deleted_at__isnull=True).exec()
  ```

### What's New in 0.5.9

- **Atomic Array Operations** - Server-side array mutations that avoid read-modify-write conflicts

  - **`atomic_append(record_id, field, value)`** - Append a value to an array using `array::append()` (allows duplicates)
  - **`atomic_remove(record_id, field, value)`** - Remove a value from an array using `-=` operator
  - **`atomic_set_add(record_id, field, value)`** - Add a value to an array only if not already present using `+=` operator
  - **Use case**: Multi-pod deployments where concurrent workers update the same array field

    ```python
    # Instead of read-modify-write (causes transaction conflicts):
    #   event = await Event.objects().get(event_id)
    #   event.processed_by.append(pod_id)
    #   await event.save()

    # Use atomic operations (no conflicts):
    await Event.atomic_append(event_id, "processed_by", pod_id)
    await Event.atomic_set_add(event_id, "processed_by", pod_id)  # no duplicates
    await Event.atomic_remove(event_id, "tags", "deprecated")
    ```

- **Transaction Conflict Retry** - Automatic retry decorator for conflict errors

  - **`retry_on_conflict(max_retries=3)`** - Decorator with exponential backoff + jitter
  - **`TransactionConflictError`** - New exception subclass for conflict detection

    ```python
    from surreal_orm import retry_on_conflict

    @retry_on_conflict(max_retries=5, base_delay=0.05)
    async def process_event(event_id: str, pod_id: str):
        await Event.atomic_set_add(event_id, "processed_by", pod_id)
    ```

- **Relation Direction Control** - `reverse` parameter for `relate()` and `remove_relation()`

  - **`reverse=True`** swaps the direction: `to -> relation -> self` instead of `self -> relation -> to`
  - **Use case**: When schema defines edges in opposite direction from the calling context

    ```python
    # Normal: game_tables:abc -> created -> users:xyz
    await table.relate("created", creator)

    # Reverse: users:xyz -> created -> game_tables:abc
    await table.relate("created", creator, reverse=True)

    # Also works with remove_relation
    await table.remove_relation("created", creator, reverse=True)
    ```

- **New Query Lookup Operators** - Server-side array filtering

  - **`not_contains`** - Generates `CONTAINSNOT` for excluding array values
  - **`containsall`** - Generates `CONTAINSALL` for matching all values
  - **`containsany`** - Generates `CONTAINSANY` for matching any value
  - **`not_in`** - Generates `NOT IN` for exclusion sets

    ```python
    # Server-side filter: events NOT containing this pod_id
    events = await Event.objects().filter(
        processed_by__not_contains=pod_id,
        status="pending",
    ).exec()

    # Events containing ALL required tags
    events = await Event.objects().filter(
        tags__containsall=["urgent", "production"],
    ).exec()
    ```

### What's New in 0.5.8

- **Around Signals** - Generator-based middleware pattern for wrapping DB operations

  - **Around signal types**: `around_save`, `around_delete`, `around_update`
  - **Use case**: Shared state between before/after logic, timing, guaranteed cleanup

  ```python
  from surreal_orm import around_save, around_delete

  @around_save.connect(Player)
  async def time_player_save(sender, instance, created, **kwargs):
      """Wrap save with timing measurement."""
      import time
      start = time.time()

      yield  # <-- The save() operation happens here

      duration = time.time() - start
      await log_audit(f"Saved {instance.id} in {duration:.3f}s")

  @around_delete.connect(Player)
  async def delete_with_lock(sender, instance, **kwargs):
      """Hold a lock during delete with guaranteed cleanup."""
      lock = await acquire_lock(f"player:{instance.id}")
      try:
          yield  # <-- delete happens while lock is held
      finally:
          await release_lock(lock)  # Always runs, even on error
  ```

  - **Key advantages over pre/post signals**:
    - Shared local variables between before/after code
    - Guaranteed cleanup with `try/finally`
    - Single handler for both phases
    - Cleaner timing and metrics collection

  - **Execution order**: `pre_* → around(before) → DB operation → around(after) → post_*`

### What's New in 0.5.7

- **Model Signals** - Django-style event hooks for model lifecycle operations

  - **Signal types**: `pre_save`, `post_save`, `pre_delete`, `post_delete`, `pre_update`, `post_update`
  - **Use case**: Push real-time updates to WebSocket clients immediately after database operations

  ```python
  from surreal_orm import post_save, post_delete

  @post_save.connect(Player)
  async def on_player_saved(sender, instance, created, **kwargs):
      """Called after any Player is saved."""
      if instance.is_ready:
          await ws_manager.broadcast_to_table(
              instance.table_id,
              {"type": "player_ready", "player_id": str(instance.id)}
          )

  @post_delete.connect(Player)
  async def on_player_deleted(sender, instance, **kwargs):
      """Called after any Player is deleted."""
      await ws_manager.broadcast({"type": "player_left", "id": instance.id})
  ```

  - **Signal arguments**:
    - `sender`: The model class
    - `instance`: The model instance
    - `created` (save only): True if new record, False if update
    - `update_fields` (update only): Dict of fields being updated
    - `tx`: Transaction context if within a transaction

### What's New in 0.5.6

- **Bug Fix: Issue #8 bis (Numeric ID escaping in relation queries)**

  - **Fixed missing ID escaping in graph traversal queries** - When using `get_related()`, `RelationQuerySet`, or `RelationInfo.get_traversal_query()` with IDs starting with digits, the queries would fail with parse errors.

    ```python
    # Before (v0.5.5.3) - Parse error for numeric IDs in relations
    table = GameTable(id="7qvdzsc14e5clo8sg064", ...)
    await table.save()
    players = await table.get_related("has_player", direction="out", model_class=Player)
    # Parse error: Failed to parse query - expected identifier, found '7qvdzsc'

    # After (v0.5.6) - Properly escaped
    players = await table.get_related("has_player", direction="out", model_class=Player)
    # Generates: SELECT VALUE out.* FROM has_player WHERE in = game_tables:`7qvdzsc14e5clo8sg064`;
    ```

  - **Files Fixed**:
    - `fields/relation.py` - `RelationInfo.get_traversal_query()` now uses `escape_record_id()`
    - `relations.py` - `RelationQuerySet._build_traversal_query()` now uses `escape_record_id()`

### What's New in 0.5.5.3

- **Bug Fix: Issue #10 (RecordId objects in non-id fields)**

  - **Fixed RecordId objects not converted to strings in foreign key fields** - When using CBOR protocol, fields like `user_id`, `table_id` returned `RecordId` objects instead of strings, causing Pydantic validation errors.

    ```python
    # Before (v0.5.5.2) - RecordId objects caused validation errors
    record = await Model.objects().get("id")
    print(record.user_id)  # RecordId(table='users', id='abc123')

    # After (v0.5.5.3) - Converted to proper strings
    print(record.user_id)  # "users:abc123"
    ```

  - **Fix**: `_preprocess_db_record()` now converts RecordId objects in ALL fields:
    - For `id` field: extracts just the id part (e.g., `"abc123"`)
    - For other fields: converts to "table:id" format (e.g., `"users:abc123"`)

### What's New in 0.5.5.2

- **Critical Bug Fix: Issue #9 (datetime_type regression)**

  - **Fixed Pydantic validation error for datetime fields** - In v0.5.5.1, records containing datetime fields failed Pydantic validation when loaded from the database, causing `from_db()` to silently return dict values instead of model instances.

  - **Fix**: Added `_preprocess_db_record()` method to handle datetime parsing and RecordId conversion before Pydantic validation.

### What's New in 0.5.5.1

- **Critical Bug Fixes** - Fixes for issues reported in production usage

  - **Issue #8 (CRITICAL): IDs starting with digits** - Fixed parse error when record IDs start with a digit (e.g., `7qvdzsc14e5clo8sg064`). IDs are now properly escaped with backticks when needed.

    ```python
    # Now works correctly
    table = await GameTable.objects().get("7qvdzsc14e5clo8sg064")

    # IDs starting with digits are automatically escaped
    # Generates: SELECT * FROM game_tables:`7qvdzsc14e5clo8sg064`
    ```

  - **Issue #3 (HIGH): data: prefix strings** - Fixed issue where strings starting with `data:` (like data URLs) were interpreted as record links. CBOR protocol is now the default for HTTP connections.

    ```python
    # Now works correctly
    player.avatar = "data:image/png;base64,iVBORw0KGgo..."
    await player.save()  # Saves as string, not record link
    ```

  - **Issue #1 (HIGH): Full record ID format in .get()** - `.get()` now properly handles both ID formats using the new `parse_record_id()` utility.

  - **Issue #2 (MEDIUM): remove_relation() string IDs** - Fixed to properly use parameterized queries when removing relations with string IDs.

  - **Issue #7 (MEDIUM): get_related() with direction=in** - Improved query syntax using `SELECT VALUE field.*` for more reliable record extraction.

  - **Bug fix: update() table name** - Fixed bug where `update()` used `self.__class__.__name__` instead of `self.get_table_name()`, causing failures with custom table names.

- **SDK Enhancements**

  - **HTTP CBOR Protocol** - HTTP connections now support CBOR protocol (default) in addition to JSON. This fixes the data URL interpretation issue.

    ```python
    # CBOR is now the default for both HTTP and WebSocket
    SurrealDBConnectionManager.set_connection(
        url="http://localhost:8000",
        user="root",
        password="root",
        namespace="test",
        database="test",
        protocol="cbor",  # Default, can use "json" for debugging
    )
    ```

- **New Utility Functions** - Added to `surreal_orm.utils`:

  - `needs_id_escaping(record_id)` - Check if an ID needs escaping
  - `escape_record_id(record_id)` - Escape an ID with backticks if needed
  - `format_thing(table, record_id)` - Format a full thing reference with proper escaping
  - `parse_record_id(full_id)` - Parse a record ID into (table, id) tuple

### What's New in 0.5.5

- **CBOR Protocol (Default)** - Binary protocol for WebSocket connections

  - **CBOR is now required** - `cbor2>=5.6.0` is a required dependency (no longer optional)
  - **CBOR is the default protocol** - WebSocket connections use `protocol="cbor"` by default
  - **Aligns with official SurrealDB SDK** - Uses the same protocol as the official Python SDK

    ```python
    # CBOR is the default protocol (no need to specify)
    async with SurrealDB.ws("ws://localhost:8000", "ns", "db") as db:
        # data:xxx strings are handled correctly
        await db.create("files", {"content": "data:image/png;base64,..."})

    # Use JSON only for debugging/compatibility
    async with SurrealDB.ws("ws://localhost:8000", "ns", "db", protocol="json") as db:
        ...
    ```

  - **Fixes data: prefix issue** - JSON protocol incorrectly interprets `data:xxx` strings as record links. CBOR properly encodes them as strings.
  - **Custom CBOR tags** - Full support for SurrealDB's custom CBOR tags (RecordId, Table, Duration, DateTime, UUID, Decimal)

- **Field Alias Support** - Map Python field names to different database column names

  - **Pydantic Field aliases** - Use `Field(alias="db_column_name")` to map Python fields to DB columns:

    ```python
    class User(BaseSurrealModel):
        # Python 'password' maps to 'password_hash' in database
        password: str = Field(alias="password_hash")

    user = User(password="secret")  # Use Python name
    await user.save()  # Saves as password_hash in DB
    ```

  - **Automatic bidirectional mapping** - Saves use alias names, loads accept both names

- **Sync `unset_connection()`** - Synchronous version for non-async contexts

  - **`unset_connection_sync()`** - New method for use in atexit handlers or `__del__` methods:

    ```python
    # In non-async cleanup code
    SurrealDBConnectionManager.unset_connection_sync()
    ```

### What's New in 0.5.4

- **API Improvements** - Enhanced usability for common operations

  - **Record ID format handling** - `QuerySet.get()` now automatically handles both formats:
    - Just the ID: `get("abc123")`
    - Full SurrealDB format: `get("players:abc123")`
    This eliminates the need to manually strip table prefixes from IDs.

  - **`remove_relation()` accepts string IDs** - The method now accepts both model instances and string IDs:
    - Model instance: `await table.remove_relation("has_player", player_instance)`
    - String ID (full format): `await table.remove_relation("has_player", "players:abc123")`
    - String ID (just ID): `await table.remove_relation("has_player", "abc123")`

  - **`raw_query()` class method** - New method for executing arbitrary SurrealQL queries:

    ```python
    # Simple query
    results = await User.raw_query("SELECT * FROM users WHERE age > 21")

    # With variables (safe from injection)
    results = await User.raw_query(
        "SELECT * FROM users WHERE status = $status",
        variables={"status": "active"}
    )

    # Complex graph query
    results = await User.raw_query(
        "DELETE has_player WHERE in = $table_id AND out = $player_id",
        variables={"table_id": "tables:xyz", "player_id": "players:abc"}
    )
    ```

### What's New in 0.5.3.3

- **Bug Fix**
  - **`from_db()` fields_set** - Fixed bug where `from_db()` marked all fields as "user-set", causing `exclude_unset=True` to include DB-loaded fields (like `created_at`) in subsequent saves. Now `from_db()` clears `__pydantic_fields_set__` so only user-modified fields are sent during updates.

### What's New in 0.5.3.2

- **Critical Bug Fix**
  - **QuerySet table name** - Fixed critical bug where QuerySet used class name (`MyModel`) instead of configured `table_name` from `model_config`. This caused queries to fail silently when using custom table names.

- **API Improvements**
  - **`QuerySet.get()` signature** - Now accepts both `id=` keyword and positional `id_item` parameter for better usability. All these work: `get("id")`, `get(id="id")`, `get(id_item="id")`.

### What's New in 0.5.3.1

- **Bug Fixes** - Critical fixes for partial updates and datetime handling
  - **Partial updates for persisted records** - `save()` now uses `merge()` for already-persisted records, only sending explicitly modified fields. This prevents server-set fields (like `created_at`) from being overwritten with NONE.
  - **datetime parsing from DB** - `_update_from_db()` now automatically parses ISO 8601 datetime strings from SurrealDB into Python `datetime` objects. No more manual conversion needed.
  - **`_db_persisted` flag** - New internal tracking to distinguish between new records (use upsert) and already-persisted records (use merge for partial update).

### What's New in 0.5.3

- **ORM Improvements** - Better save/update behavior
  - **Upsert behavior** - `save()` now uses `upsert` for new records with ID (idempotent, Django-like)
  - **`server_fields` config** - Exclude server-generated fields (created_at, updated_at) from save/update
  - **`merge()` returns self** - Now returns the updated model instance instead of None
  - **`save()` updates self** - No longer returns new instance, updates original in place

- **Bug Fixes** - Critical fixes for ORM
  - **NULL values fix** - `_update_from_db()` preserves `__pydantic_fields_set__` so `exclude_unset=True` works after DB load
  - **datetime for UPDATE** - Server fields excluded via `server_fields` config option

- **SDK Enhancements**
  - **`upsert()` method** - Added to `BaseSurrealConnection` and transactions for create-or-update operations

### What's New in 0.5.2

- **FieldType Enum Improvements** - Enhanced type system for migrations
  - Added `NUMBER`, `SET`, `REGEX` types to `FieldType` enum
  - `generic(inner_type)` method for parameterized types (`array<string>`, `record<users>`)
  - `from_python_type(type)` class method for automatic Python → SurrealDB type mapping
  - Comprehensive docstrings with SurrealDB type documentation
  - `AddField` and `AlterField` now accept `FieldType | str` with validation

- **Bug Fixes** - Critical fixes for SDK and ORM
  - **datetime serialization** - Custom JSON encoder for datetime, date, time, Decimal, UUID in RPC requests
  - **Fluent API** - `connect()` now returns `Self` for method chaining (`await conn.connect().signin(...)`)
  - **Session cleanup** - WebSocket callback tasks properly tracked and cancelled on close
  - **Optional fields** - `exclude_unset=True` in `model_dump()` prevents None from overriding DB defaults
  - **Parameter alias** - `username` parameter alias for `user` in `SurrealDBConnectionManager.set_connection()`
  - **Patch version increment** - Fixed version calculation to use semantic base (x.y.z) instead of full version

### What's New in 0.5.1

- **Dependabot Security Workflows** - Automated security update management
  - Auto-merge for Dependabot PRs after tests pass
  - Patch version tagging (x.x.x.1, x.x.x.2, etc.) for security updates
  - Automatic GitHub releases for security patches

- **SurrealDB Security Monitoring** - Database vulnerability tracking
  - Daily checks for new SurrealDB releases
  - Automatic integration tests with new DB versions
  - Auto-update of DB requirements on security patches
  - Issue creation for compatibility failures

### What's New in 0.5.0

- **Live Select Stream** - Async iterator pattern for real-time change notifications
  - `LiveSelectStream` with `async with` context manager and `async for` iteration
  - `LiveChange` dataclass: `record_id`, `action`, `result`, `changed_fields`
  - `LiveAction` enum: `CREATE`, `UPDATE`, `DELETE`
  - WHERE clause support with parameterized queries
  - DIFF mode for receiving only changed fields

- **Auto-Resubscribe** - Automatic reconnection after WebSocket disconnect
  - `auto_resubscribe=True` parameter on `live_select()`
  - `on_reconnect(old_id, new_id)` callback for tracking ID changes
  - Seamless recovery from K8s pod restarts and network interruptions

- **Typed Function Calls** - Pydantic/dataclass return type support
  - `call(function, params, return_type)` for typed results
  - Automatic conversion to Pydantic models and dataclasses

### What's New in 0.4.0

- **Relations & Graph Traversal** - Declarative relation definitions
  - `ForeignKey`, `ManyToMany`, `Relation` field types
  - `RelationManager` for lazy loading and operations (add, remove, set, clear)
  - Graph traversal with `traverse()` and `graph_query()`
  - `select_related()` and `prefetch_related()` for N+1 prevention
  - Model methods: `relate()`, `remove_relation()`, `get_related()`

### What's New in 0.3.x

- **ORM Transactions** (v0.3.0) - `tx` parameter on save/update/delete, `Model.transaction()` shortcut
- **Aggregations** (v0.3.0) - `Count`, `Sum`, `Avg`, `Min`, `Max` + GROUP BY with `values()`/`annotate()`
- **Bulk Operations** (v0.3.1) - `bulk_create()`, `bulk_update()`, `bulk_delete()` with atomic support

### What's New in 0.2.x

- **Atomic Transactions** - Context manager for atomic operations with auto-commit/rollback
- **Typed Functions API** - Fluent API for SurrealDB built-in and custom functions
- **Django-style Migrations** - Full migration system with CLI
- **JWT Authentication** - SurrealDB native authentication support
- **Test Infrastructure** - Automatic container lifecycle management

---

## Architecture

```text
src/
├── surreal_orm/                 # Django-style ORM
│   ├── __init__.py              # Public API exports
│   ├── connection_manager.py    # Connection singleton (uses surreal_sdk)
│   ├── model_base.py            # BaseSurrealModel with CRUD + relation methods
│   ├── query_set.py             # Fluent query builder + graph traversal
│   ├── q.py                     # Q objects for complex OR/AND/NOT queries
│   ├── relations.py             # RelationManager, RelationQuerySet, RelationDescriptor
│   ├── aggregations.py          # Count, Sum, Avg, Min, Max
│   ├── auth.py                  # JWT authentication mixin
│   ├── fields/                  # Field types
│   │   ├── encrypted.py         # Encrypted field type
│   │   └── relation.py          # ForeignKey, ManyToMany, Relation
│   ├── types.py                 # TableType enum, SurrealConfigDict
│   └── migrations/              # Migration system
│       ├── operations.py        # CreateTable, AddField, etc.
│       ├── state.py             # SchemaState with diff algorithm
│       ├── generator.py         # Migration file generator
│       └── executor.py          # Migration executor
│
└── surreal_sdk/                 # Custom SDK (zero external dependencies)
    ├── __init__.py              # SurrealDB.http() / .ws() / .pool()
    ├── exceptions.py            # SurrealDBError, TransactionError, etc.
    ├── types.py                 # QueryResponse, RecordResponse, etc.
    ├── transaction.py           # HTTPTransaction, WebSocketTransaction
    ├── functions.py             # Typed functions API (db.fn.math.sqrt)
    ├── connection/
    │   ├── base.py              # BaseSurrealConnection ABC
    │   ├── http.py              # HTTPConnection (stateless, httpx)
    │   ├── websocket.py         # WebSocketConnection (stateful, aiohttp)
    │   └── pool.py              # ConnectionPool
    └── streaming/
        ├── change_feed.py       # ChangeFeedStream (CDC, HTTP)
        ├── live_query.py        # LiveQuery callback-based (WebSocket)
        └── live_select.py       # LiveSelectStream async iterator (WebSocket)
```

---

## Key Components

### 1. Custom SDK (`surreal_sdk`)

The SDK provides direct connection to SurrealDB without the official package.

**Connection Types:**

```python
from surreal_sdk import SurrealDB

# HTTP (stateless, microservices)
async with SurrealDB.http("http://localhost:8000", "ns", "db") as db:
    await db.signin("root", "root")
    result = await db.query("SELECT * FROM users")

# WebSocket (stateful, real-time)
async with SurrealDB.ws("ws://localhost:8000", "ns", "db") as db:
    await db.signin("root", "root")
    await db.live("orders", callback=on_change)

# Connection Pool
async with SurrealDB.pool("http://localhost:8000", "ns", "db", size=10) as pool:
    await pool.set_credentials("root", "root")
    async with pool.acquire() as conn:
        await conn.query("SELECT * FROM users")
```

**Transactions:**

```python
# Atomic operations with auto-commit/rollback
async with db.transaction() as tx:
    await tx.create("orders:1", {"total": 100})
    await tx.create("payments:1", {"amount": 100})
    # Auto-commit on success, auto-rollback on exception
```

**Typed Functions:**

```python
# Built-in functions
sqrt = await db.fn.math.sqrt(16)           # 4.0
now = await db.fn.time.now()               # datetime
sha = await db.fn.crypto.sha256("data")    # hash

# Custom user-defined functions
result = await db.fn.my_custom_function(arg1, arg2)
```

**Typed Function Calls (v0.5.0):**

```python
from pydantic import BaseModel

class VoteResult(BaseModel):
    success: bool
    new_count: int
    total_votes: int

# Call with typed return
result = await db.call(
    "cast_vote",
    params={"user_id": "alice", "table_id": "game:123"},
    return_type=VoteResult
)
# result is VoteResult instance, not dict
print(result.success, result.new_count)
```

**Live Select Stream (v0.5.0):**

```python
from surreal_sdk import LiveAction

# Async iterator pattern with WHERE clause and parameters
async with db.live_select(
    "players",
    where="table_id = $id",
    params={"id": "game_tables:xyz"},
    auto_resubscribe=True  # Auto-reconnect on WebSocket drop
) as stream:
    async for change in stream:
        match change.action:
            case LiveAction.CREATE:
                print(f"Player joined: {change.result['name']}")
            case LiveAction.UPDATE:
                if change.result.get("is_ready"):
                    print(f"Player ready: {change.record_id}")
            case LiveAction.DELETE:
                print(f"Player left: {change.record_id}")

# Callback-based multi-stream manager
from surreal_sdk import LiveSelectManager

manager = LiveSelectManager(db)
await manager.watch("players", on_player_change, where="table_id = $id", params={"id": table_id})
await manager.watch("game_tables", on_table_change, where="id = $id", params={"id": table_id})

# Later: cleanup
await manager.stop_all()
```

### 2. ORM (`surreal_orm`)

Django-style ORM built on top of the SDK.

**Model Definition:**

```python
from surreal_orm import BaseSurrealModel, SurrealConfigDict
from surreal_orm.types import TableType
from surreal_orm.fields import Encrypted

class User(BaseSurrealModel):
    model_config = SurrealConfigDict(
        table_type=TableType.USER,
        permissions={"select": "$auth.id = id"},
    )

    id: str | None = None
    email: str
    password: Encrypted  # Auto-hashed with argon2
    name: str
    age: int = 0
```

**QuerySet:**

```python
# Filter with Django-style lookups
users = await User.objects().filter(age__gte=18, name__startswith="A").exec()

# Supported lookups:
# exact, gt, gte, lt, lte, in, like, ilike, contains, icontains,
# startswith, istartswith, endswith, iendswith, match, regex, isnull
```

**JWT Authentication:**

```python
from surreal_orm.auth import AuthenticatedUserMixin

class User(AuthenticatedUserMixin, BaseSurrealModel):
    # ...

# Signup (returns user + token)
user, token = await User.signup(email="alice@example.com", password="secret", name="Alice")

# Signin (returns user + token)
user, token = await User.signin(email="alice@example.com", password="secret")

# Validate token (lightweight)
record_id = await User.validate_token(token)
```

### 3. Migration System

Django-style migrations with CLI support.

```bash
# Generate migration from model changes
surreal-orm makemigrations --name initial

# Apply migrations
surreal-orm migrate

# Check status
surreal-orm status

# Rollback
surreal-orm rollback 0001_initial
```

---

## Dependencies

**Runtime:**

- `pydantic >= 2.10.5` - Model validation
- `httpx >= 0.27.0` - HTTP client
- `aiohttp >= 3.9.0` - WebSocket client
- `cbor2 >= 5.6.0` - CBOR protocol (required, default for WebSocket)
- `click >= 8.1.0` - CLI (optional, with `[cli]`)

**No dependency on the official `surrealdb` package!**

**Dev:**

- pytest, pytest-asyncio, pytest-cov
- mypy, ruff
- docker (for integration tests)

---

## Test Structure

```text
tests/
├── conftest.py              # Container lifecycle management
├── test_unit.py             # ORM unit tests
├── test_manager.py          # ConnectionManager tests
├── test_e2e.py              # ORM integration tests
└── sdk/
    ├── test_connection.py   # HTTP/WS connection tests
    ├── test_transactions.py # Transaction tests
    ├── test_functions.py    # Typed functions tests
    └── test_streaming.py    # Live query/change feed tests
```

**Running tests:**

```bash
make test              # Unit tests only (no SurrealDB needed)
make test-sdk          # SDK tests only
make test-integration  # Integration tests (auto-starts container)
make test-all          # All tests
make ci-lint           # Run all linters (mypy, ruff)
```

---

## Useful Commands

```bash
# Installation
uv sync

# Tests
make test              # Unit tests (no SurrealDB)
make test-sdk          # SDK tests only
make test-integration  # Integration tests (auto-manages container)
make test-all          # All tests

# Docker/SurrealDB
make db-up             # Start test SurrealDB (port 8001)
make db-dev            # Start dev SurrealDB (port 8000, persistent)
make db-shell          # Interactive SQL shell

# Lint
make ci-lint           # Run all linters
uv run ruff check src/
uv run mypy src/

# Build
uv build
```

---

## TODO / Roadmap

See full roadmap: [docs/roadmap.md](docs/roadmap.md)

### Completed (0.2.x)

- [x] Custom SDK (HTTP + WebSocket)
- [x] Connection pooling
- [x] Atomic transactions (SDK level)
- [x] Typed functions API
- [x] Migration system
- [x] JWT Authentication
- [x] CLI commands
- [x] Live Queries
- [x] Change Feeds

### Completed (0.3.0) - ORM Transactions & Aggregations

- [x] Model-level transactions (`user.save(tx=tx)`)
- [x] QuerySet aggregations (`count()`, `sum()`, `avg()`, `min()`, `max()`)
- [x] GROUP BY with `values()` and `annotate()`

### Completed (0.3.1) - Bulk Operations

- [x] `bulk_create()` with atomic option
- [x] `bulk_update()` for filtered querysets
- [x] `bulk_delete()` with transaction support

### Completed (0.4.0) - Relations & Graph

- [x] `ForeignKey`, `ManyToMany` fields
- [x] `Relation` for graph edges (SurrealDB's `->edge->`)
- [x] Graph traversal queries (`traverse()`, `graph_query()`)
- [x] `select_related()` and `prefetch_related()` for eager loading
- [x] Model methods: `relate()`, `remove_relation()`, `get_related()`

### Completed (0.5.0) - Real-time SDK Enhancements

- [x] `LiveSelectStream` - Async iterator for Live Queries
- [x] `LiveSelectManager` - Multi-stream callback management
- [x] Auto-resubscribe on WebSocket reconnect
- [x] `LiveChange` dataclass with `record_id`, `action`, `changed_fields`
- [x] Typed function calls with Pydantic/dataclass support

### Completed (0.6.0) - Query Power & Security

- [x] Q objects for OR/AND/NOT query composition
- [x] Parameterized filters (security: `$_fN` variable binding)
- [x] `SurrealFunc` for server-side function expressions in save/update
- [x] `remove_all_relations()` for bulk edge deletion
- [x] Django-style `-field` descending ordering
- [x] Bug fix: `isnull` lookup now generates correct `IS NULL`

### Completed (0.7.0) - Performance & Developer Experience

- [x] `merge(refresh=False)` — skip extra SELECT for fire-and-forget ops
- [x] `call_function()` — invoke custom stored functions from ORM
- [x] `extra_vars` on `save()` / `merge()` — bound params in SurrealFunc
- [x] `fetch()` + FETCH clause — resolve record links inline (N+1 fix)
- [x] `remove_all_relations()` list support — multiple relation types at once

### Completed (0.8.0) - Auth Module Fixes

- [x] Ephemeral connections for auth ops (singleton no longer corrupted)
- [x] Configurable `access_name` in `SurrealConfigDict`
- [x] `signup()` returns `tuple[Self, str]` (user + JWT token)
- [x] SDK `authenticate()` method on `BaseSurrealConnection`
- [x] `authenticate_token()` fixed — returns `tuple[Self, str] | None`
- [x] `validate_token()` — lightweight token validation returning record ID

### v0.8.x - Computed Fields

- [ ] Computed fields with server-side SurrealDB expressions

### v0.9.x - ORM Real-time Integration

- [ ] Live Models (real-time sync at ORM level)
- [ ] Change Feed integration for ORM

---

## Real-World Use Case: Multiplayer Game Backend

The SDK's real-time features are designed for use cases like multiplayer game backends with 2-4 players per table.

### Scenario

A card/board game where players:

- Join tables and mark themselves "ready"
- Cast votes during gameplay
- Disconnect/reconnect during the game
- Need real-time synchronization

### Implementation Pattern

```python
from surreal_sdk import SurrealDB, LiveAction
from pydantic import BaseModel

# === Models ===
class VoteResult(BaseModel):
    success: bool
    new_count: int
    total_votes: int

# === Real-time Event Handling ===
async def on_player_change(change):
    """Handle player status changes."""
    match change.action:
        case LiveAction.CREATE:
            await notify_table(f"Player joined: {change.result['name']}")
        case LiveAction.UPDATE:
            if change.result.get("is_ready"):
                await check_all_ready(change.result["table_id"])
        case LiveAction.DELETE:
            await handle_player_disconnect(change.record_id)

async def on_table_change(change):
    """Handle game table status changes."""
    if change.action == LiveAction.UPDATE:
        status = change.result.get("status")
        if status == "voting":
            await start_vote_timer()
        elif status == "completed":
            await show_results()

# === Main Game Loop ===
async def run_game(table_id: str):
    async with SurrealDB.ws("ws://localhost:8000", "game", "prod") as db:
        await db.signin("root", "root")

        # Subscribe to players at this table (auto-reconnect enabled)
        async with db.live_select(
            "players",
            where="table_id = $table_id",
            params={"table_id": table_id},
            auto_resubscribe=True,
            on_reconnect=lambda old, new: print(f"Reconnected: {old} -> {new}")
        ) as player_stream:

            # Also watch the table itself
            async with db.live_select(
                "game_tables",
                where="id = $id",
                params={"id": table_id},
                auto_resubscribe=True
            ) as table_stream:

                # Process events from both streams
                async for change in player_stream:
                    await on_player_change(change)

# === Typed Function Calls ===
async def cast_vote(db, user_id: str, table_id: str) -> VoteResult:
    """Cast a vote with typed return."""
    return await db.call(
        "cast_vote",  # SurrealDB user-defined function
        params={"user_id": user_id, "table_id": table_id},
        return_type=VoteResult
    )
```

### Key Features for Game Backends

| Feature                     | Benefit                                     |
| --------------------------- | ------------------------------------------- |
| `live_select()` with WHERE  | Subscribe only to relevant players/tables   |
| `auto_resubscribe=True`     | Seamless recovery from K8s pod restarts     |
| `on_reconnect` callback     | Track subscription ID changes for debugging |
| `LiveChange.action`         | Distinguish CREATE/UPDATE/DELETE events     |
| `LiveChange.record_id`      | Quick access to affected record ID          |
| `LiveChange.changed_fields` | (DIFF mode) Know exactly what changed       |
| Typed `call()`              | Get Pydantic models instead of raw dicts    |

### SurrealDB Function Example

Define in SurrealDB for typed function calls:

```sql
DEFINE FUNCTION fn::cast_vote($user_id: string, $table_id: string) {
    LET $player = SELECT * FROM players WHERE user_id = $user_id AND table_id = $table_id;
    IF $player.has_voted {
        RETURN { success: false, new_count: 0, total_votes: 0 };
    };
    UPDATE players SET has_voted = true WHERE id = $player.id;
    LET $count = (SELECT count() FROM players WHERE table_id = $table_id AND has_voted = true GROUP ALL).count;
    LET $total = (SELECT count() FROM players WHERE table_id = $table_id GROUP ALL).count;
    RETURN { success: true, new_count: $count, total_votes: $total };
};
```

---

## Known Issues

_All previously documented issues have been fixed in v0.3.1:_

- ~~`refresh()` in model_base.py doesn't reassign data to instance~~ (Fixed)
- ~~ORDER BY positioned after LIMIT/START in generated SQL~~ (Fixed in v0.3.1)
- ~~Typo "primirary_key" in error message~~ (Fixed in v0.3.1)

---

## Code Conventions

- **Language:** English (code, comments, docs)
- **Style:** ruff format + ruff lint
- **Types:** mypy strict
- **Async:** All I/O must be async
- **Tests:** pytest-asyncio for async tests

---

## Contact

**Author:** Yannick Croteau
**GitHub:** EulogySnowfall
